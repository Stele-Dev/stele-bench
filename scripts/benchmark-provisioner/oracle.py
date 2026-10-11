#!/usr/bin/env python3
"""Maintainer CLI for repeatable benchmark-oracle runs."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import comb
from pathlib import Path
from typing import Any

import anthropic_reserve
import gateway_reserve
import model_provider
import prepare_external_dataset
from gateway_reserve import GatewayReserveError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# The aggregation maths is methodology, not orchestration, so it lives in the
# public benchmark package alongside the contracts it summarises.
sys.path.insert(0, os.fspath(REPOSITORY_ROOT / "eval" / "stele-bench" / "src"))

from stele_bench import harbor_pack, metrics, report, results, statistics  # noqa: E402

DEFAULT_SOURCE = REPOSITORY_ROOT.parent / "dotstate"
DEFAULT_REVISION = "a4a5986eafa496dee73a93141588cffaed559f80"
DEFAULT_CHALLENGE = "input-keypress-architecture"
DEFAULT_FIXTURE = (
    REPOSITORY_ROOT / "eval" / "stele-bench" / "fixtures" / "dotstate-v1" / "fixture.json"
)
BENCH_ROOT = REPOSITORY_ROOT / "eval" / "stele-bench"
CHALLENGES_ROOT = BENCH_ROOT / "challenges"
# Durable result records live in the repository, not in machine-local state.
# See `export_result`.
RESULTS_ROOT = BENCH_ROOT / "results"
RUNNER = Path(__file__).with_name("run_scored_smoke.py")
BORROWED_RUNNER = Path(__file__).with_name("run_borrowed_task.py")
COMPOUNDING_RUNNER = Path(__file__).with_name("run_compounding_chain.py")
INSTALL_ONLY_RUNNER = Path(__file__).with_name("run_install_only_smoke.py")
PROVISIONER = Path(__file__).with_name("provision.py")
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_COMPOUNDING_MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_COMPOUNDING_FIXTURE = BENCH_ROOT / "fixtures" / "compounding-fresh-v1" / "fixture.json"
DEFAULT_COMPOUNDING_PREREGISTRATION = (
    BENCH_ROOT / "preregistrations" / "profile-selection-compounding-v4.md"
)
COMPOUNDING_COHORT_ATTEMPTS = 10
COMPOUNDING_COHORT_CAP_USD = Decimal("400.00")
RETIRED_COMPOUNDING_PREREGISTRATIONS = {"profile-selection-compounding-v4.md"}
COMPOUNDING_CHALLENGES = (
    "profile-selection-intake",
    "profile-selection-live-path",
)
DEFAULT_K5_CHAIN_MANIFEST = BENCH_ROOT / "preregistrations" / "click-clirunner-k5-v1.json"
DEFAULT_K5_PREREGISTRATION = BENCH_ROOT / "preregistrations" / "click-clirunner-k5-v1.md"
DEFAULT_CONTINUITY_CHAIN_MANIFEST = (
    BENCH_ROOT / "preregistrations" / "fieldnote-continuity-v8.json"
)
DEFAULT_CONTINUITY_PREREGISTRATION = (
    BENCH_ROOT / "preregistrations" / "fieldnote-continuity-v8.md"
)
K5_COHORT_ATTEMPTS = 20
K5_SESSION_COUNT = 5
K5_SESSION_CAP_USD = Decimal("3.00")
K5_PAIR_CAP_USD = Decimal("30.00")
K5_COHORT_CAP_USD = Decimal("600.00")
K5_EXPERIMENT_CAP_USD = Decimal("630.00")
CONTINUITY_COHORT_ATTEMPTS = 20
CONTINUITY_SESSION_COUNT = 4
CONTINUITY_SESSION_CAP_USD = Decimal("3.00")
CONTINUITY_PAIR_CAP_USD = Decimal("30.00")
CONTINUITY_COHORT_CAP_USD = Decimal("600.00")
PAIR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
HARBOR_VERSION = re.compile(r"(?:^|\s)v?(0\.20\.\d+)(?:\s|$)")
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_UNTRACKED_BYTES = 64 * 1024 * 1024


class OracleError(RuntimeError):
    """A maintainer operation could not be completed safely."""


class OracleInterrupted(BaseException):
    """A controlled termination signal interrupted a maintainer operation."""

    def __init__(self, signum: int) -> None:
        super().__init__("runner interrupted by a termination signal")
        self.signum = signum


def _provisioner_executable() -> Path:
    """Resolve the graph adapter without coupling the oracle to Stele's schema."""

    configured = os.environ.get("STELE_BENCH_PROVISIONER")
    if not configured:
        return PROVISIONER
    path = Path(configured).expanduser().resolve(strict=False)
    if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        raise OracleError(
            "STELE_BENCH_PROVISIONER must name an executable regular file"
        )
    return path


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[..., CommandResult]

# Injected so tests exercise the fence without reaching the network. Production
# callers always get the real Gateway query.
ReserveChecker = Callable[..., dict[str, Any]]


def _provider_reserve_check(**kwargs: Any) -> dict[str, Any]:
    """The spending-fence preflight for the selected model provider."""

    if model_provider.current() == model_provider.ANTHROPIC:
        return anthropic_reserve.check_anthropic_reserve(**kwargs)
    return gateway_reserve.check_gateway_reserve(**kwargs)


def _provider_drain_check(**kwargs: Any) -> dict[str, Any]:
    """The between-arms drain check for the selected model provider."""

    if model_provider.current() == model_provider.ANTHROPIC:
        return anthropic_reserve.check_anthropic_drain(**kwargs)
    return gateway_reserve.check_gateway_drain(**kwargs)


def _default_reserve_config(state_root: Path) -> Path:
    if model_provider.current() == model_provider.ANTHROPIC:
        return anthropic_reserve.default_config_path(state_root)
    return gateway_reserve.default_config_path(state_root)


def _require_gateway_provider(run_shape: str) -> None:
    """Refuse a run shape whose analysis only the Gateway's billing can support.

    Compounding, K=5 and continuity runs score each session on an exclusive,
    settled cost delta read from the Gateway key's quota. The Anthropic usage
    and cost reports have no equivalent at that grain, so running them on the
    direct provider would produce receipts their own analysis rejects.
    """

    if model_provider.current() != model_provider.GATEWAY:
        raise OracleError(
            f"{run_shape} runs require the gateway model provider; their analysis needs "
            "exclusive per-session Gateway billing deltas"
        )


def _check_reserve(
    checker: ReserveChecker,
    *,
    state_root: Path,
    config_path: Path | None,
    pair_cap_usd: Decimal,
) -> dict[str, Any]:
    """Verify the selected provider's spending fence, mapping failures into OracleError."""

    try:
        return checker(
            acknowledged_pair_cap_usd=pair_cap_usd,
            config_path=config_path or _default_reserve_config(state_root),
        )
    except GatewayReserveError as error:
        raise OracleError(str(error)) from None


def _run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: float = 120.0,
) -> CommandResult:
    try:
        result = subprocess.run(
            tuple(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OracleError(f"command unavailable: {Path(argv[0]).name}") from error
    return CommandResult(result.returncode, result.stdout, result.stderr)


@contextmanager
def _controlled_termination_signals() -> object:
    """Turn interactive and supervisor termination into receipt-safe control flow."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    watched = tuple(
        candidate
        for candidate in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
        )
        if candidate is not None
    )
    previous = {candidate: signal.getsignal(candidate) for candidate in watched}

    def interrupt(signum: int, _frame: object) -> None:
        raise OracleInterrupted(signum)

    try:
        for candidate in watched:
            signal.signal(candidate, interrupt)
        yield
    finally:
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _state_root(value: Path | None = None) -> Path:
    if value is None:
        configured = os.environ.get("XDG_STATE_HOME")
        value = (
            Path(configured).expanduser() / "stele-bench"
            if configured
            else Path.home() / ".local" / "state" / "stele-bench"
        )
    root = value.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise OracleError("state root must be a real directory")
    root.chmod(0o700)
    return root


def _safe_id(value: str, name: str = "pair id") -> str:
    if not PAIR_ID.fullmatch(value):
        raise OracleError(f"{name} must be a portable identifier")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if len(payload) > MAX_JSON_BYTES:
        raise OracleError("receipt exceeds the private file limit")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OracleError(f"not a regular JSON file: {path}")
        if metadata.st_size > MAX_JSON_BYTES:
            raise OracleError(f"JSON file exceeds the private file limit: {path}")
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OracleError(f"could not read JSON file: {path}") from error
    if not isinstance(value, dict):
        raise OracleError(f"JSON root must be an object: {path}")
    return value


def _command_version(runner: CommandRunner, argv: Sequence[str], *, cwd: Path | None = None) -> str:
    result = runner(argv, cwd=cwd, timeout=30.0)
    if result.returncode:
        raise OracleError(f"{Path(argv[0]).name} check failed")
    return (result.stdout or result.stderr).strip().splitlines()[0][:200]


def _docker_arch(runner: CommandRunner) -> tuple[str, str]:
    result = runner(
        ("docker", "info", "--format", "{{.Architecture}} {{.ServerVersion}}"),
        timeout=30.0,
    )
    if result.returncode:
        raise OracleError("Docker daemon is unavailable")
    parts = result.stdout.strip().split()
    if not parts:
        raise OracleError("Docker did not report its architecture")
    return parts[0].lower(), parts[1] if len(parts) > 1 else "unknown"


def _target_for_arch(architecture: str) -> str:
    normalized = architecture.lower()
    if normalized in {"arm64", "aarch64"}:
        return "linux-arm64"
    if normalized in {"amd64", "x86_64", "x64"}:
        return "linux-x64"
    raise OracleError(f"unsupported Docker architecture: {architecture}")


def _supabase_configuration(runner: CommandRunner) -> tuple[str, str, str]:
    result = runner(("supabase", "status", "-o", "env"), cwd=REPOSITORY_ROOT, timeout=45.0)
    if result.returncode:
        raise OracleError("local Supabase stack is unavailable")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip().strip('"')
    url = values.get("API_URL")
    key = values.get("PUBLISHABLE_KEY") or values.get("ANON_KEY")
    local_url = r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::|/|$)"
    if not url or not key or not re.match(local_url, url):
        raise OracleError("local Supabase configuration is incomplete or non-local")
    version = _command_version(runner, ("supabase", "--version"))
    return url.rstrip("/"), key, version


def _docker_reachable_url(local_url: str) -> str:
    """Translate an exact loopback URL for a Docker container, and nothing else."""

    try:
        parsed = urllib.parse.urlsplit(local_url)
        port = parsed.port
    except ValueError as error:
        raise OracleError("local backend URL has an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise OracleError("Docker URL translation accepts only a credential-free loopback URL")
    authority = "host.docker.internal"
    if port is not None:
        authority = f"{authority}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def _validate_source(runner: CommandRunner, source: Path, revision: str) -> str:
    if not source.is_dir():
        raise OracleError(f"source repository is unavailable: {source}")
    result = runner(
        ("git", "rev-parse", "--verify", f"{revision}^{{commit}}"), cwd=source, timeout=30.0
    )
    commit = result.stdout.strip().lower()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise OracleError("pinned source commit is unavailable")
    if commit != revision.lower():
        raise OracleError("source revision does not resolve to the pinned commit exactly")
    return commit


def _validate_fixture_and_challenge(fixture: Path, challenge: str) -> dict[str, str]:
    fixture = fixture.resolve()
    fixture_data = _read_json(fixture)
    if fixture_data.get("fixture_id") is None or not isinstance(fixture_data.get("graphs"), dict):
        raise OracleError("fixture manifest is incomplete")
    faithful = fixture_data["graphs"].get("faithful")
    if not isinstance(faithful, dict):
        raise OracleError("fixture has no faithful graph descriptor")
    graph_relative = faithful.get("path")
    graph_sha = faithful.get("sha256")
    if not isinstance(graph_relative, str) or not isinstance(graph_sha, str):
        raise OracleError("faithful graph descriptor is incomplete")
    graph_path = (fixture.parent / graph_relative).resolve()
    try:
        graph_path.relative_to(fixture.parent)
    except ValueError as error:
        raise OracleError("faithful graph path escapes the fixture") from error
    graph_matches = (
        graph_path.is_file()
        and SHA256.fullmatch(graph_sha)
        and _sha256_file(graph_path) == graph_sha
    )
    if not graph_matches:
        raise OracleError("faithful graph digest mismatch")
    challenge_directory = CHALLENGES_ROOT / _safe_id(challenge, "challenge id")
    manifest = challenge_directory / "challenge.json"
    challenge_data = _read_json(manifest)
    if challenge_data.get("challenge_id") != challenge:
        raise OracleError("challenge manifest identity mismatch")
    verifier = challenge_data.get("verifier")
    asset = verifier.get("asset") if isinstance(verifier, dict) else None
    if not isinstance(asset, dict) or not isinstance(asset.get("path"), str):
        raise OracleError("challenge verifier descriptor is incomplete")
    verifier_path = challenge_directory / asset["path"]
    expected = asset.get("sha256")
    if not verifier_path.is_file() or expected != _sha256_file(verifier_path):
        raise OracleError("challenge verifier digest mismatch")
    return {
        "fixture_id": str(fixture_data["fixture_id"]),
        "faithful_graph_sha256": graph_sha,
        "challenge_manifest_sha256": _sha256_file(manifest),
        "verifier_sha256": str(expected),
    }


def _certify_packaged_inputs(
    runner: CommandRunner,
    *,
    source: Path,
    revision: str,
    fixture: Path,
    challenge: str,
    receipt_out: Path | None = None,
) -> dict[str, Any]:
    """Statically certify every packaged input before any state or spend.

    This is the zero-model leakage and benchmark-awareness gate: it probes the
    packaged corpus, the evidence corpus, every declared graph arm, and the
    selected challenge. A missing corpus or fixture artifact is a failure, not
    a skipped check — a gate that quietly covers nothing still reports success.
    """

    argv = [
        sys.executable,
        "-m",
        "stele_bench.cli",
        "certify",
        os.fspath(source.resolve()),
        revision,
        os.fspath(fixture.resolve()),
        "--challenge",
        _safe_id(challenge, "challenge id"),
    ]
    if receipt_out is not None:
        argv.extend(("--receipt-out", os.fspath(receipt_out)))
    result = runner(tuple(argv), cwd=BENCH_ROOT, timeout=300.0)
    # The certify CLI prints the receipt as indented (multi-line) JSON, so parse
    # the whole stdout as one object; _last_json_object only reads single-line
    # JSON and would miss it (it did — the FakeRunner returned compact JSON,
    # which hid this until a real run).
    try:
        receipt = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        receipt = _last_json_object(result.stdout)
    if result.returncode != 0 or not isinstance(receipt, dict):
        detail = (result.stderr or result.stdout or "").strip()[:800]
        raise OracleError(f"packaged inputs failed certification: {detail}")
    return receipt


def _validate_artifact_manifest(path: Path) -> dict[str, Any]:
    manifest = _read_json(path.resolve())
    artifact_value = manifest.get("artifact")
    if not isinstance(artifact_value, dict):
        raise OracleError("artifact manifest has no artifact descriptor")
    artifact_path = Path(str(artifact_value.get("path", ""))).expanduser().resolve()
    digest = artifact_value.get("sha256")
    artifact_matches = (
        artifact_path.is_file()
        and isinstance(digest, str)
        and digest == _sha256_file(artifact_path)
    )
    if not artifact_matches:
        raise OracleError("artifact manifest digest does not match the executable")
    if not os.access(artifact_path, os.X_OK):
        raise OracleError("artifact is not executable")
    return manifest


def doctor(
    *,
    source: Path = DEFAULT_SOURCE,
    revision: str = DEFAULT_REVISION,
    fixture: Path = DEFAULT_FIXTURE,
    challenge: str = DEFAULT_CHALLENGE,
    artifact_manifest: Path | None = None,
    gateway_reserve_config: Path | None = None,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
) -> dict[str, Any]:
    root = _state_root(state_root)
    checks: dict[str, Any] = {
        "python": platform.python_version(),
        "uv": _command_version(runner, ("uv", "--version")),
        "git": _command_version(runner, ("git", "--version")),
    }
    docker_arch, docker_version = _docker_arch(runner)
    checks["docker"] = {"architecture": docker_arch, "server_version": docker_version}
    harbor = _command_version(runner, ("harbor", "--version"))
    match = HARBOR_VERSION.search(harbor)
    if match is None:
        raise OracleError(f"Harbor 0.20.x is required; found {harbor or 'unknown'}")
    checks["harbor"] = match.group(1)
    backend_url, _, supabase_version = _supabase_configuration(runner)
    checks["supabase"] = {
        "version": supabase_version,
        "local_stack": True,
        "docker_reachable_url": _docker_reachable_url(backend_url),
    }
    checks["source_commit"] = _validate_source(runner, source.resolve(), revision)
    checks.update(_validate_fixture_and_challenge(fixture, challenge))
    certification = _certify_packaged_inputs(
        runner, source=source, revision=revision, fixture=fixture, challenge=challenge
    )
    checks["certification"] = {
        "certified": certification.get("certified"),
        "arms": certification.get("arms"),
        "files_probed": certification.get("files_probed"),
        "graph_values_probed": certification.get("graph_values_probed"),
        "redactions": len(certification.get("redactions") or ()),
    }
    if not os.environ.get("AI_GATEWAY_KEY_EVAL"):
        raise OracleError("AI_GATEWAY_KEY_EVAL is not exported")
    checks["gateway_key_present"] = True
    fence_name = "gateway_reserve"
    if model_provider.current() == model_provider.ANTHROPIC:
        # The Gateway key stays required: the semantic bridge embeds through it.
        for name in (model_provider.ANTHROPIC_KEY_ENV, model_provider.ANTHROPIC_ADMIN_KEY_ENV):
            if not os.environ.get(name):
                raise OracleError(f"{name} is not exported")
        checks["model_provider"] = model_provider.ANTHROPIC
        fence_name = "anthropic_reserve"
    # Zero-cost readiness: a cap of zero only reports the current fence state,
    # but it still fails if identity, balance, budget, or calibration cannot be
    # verified — which is exactly what "ready for a paid run" has to mean.
    checks[fence_name] = _check_reserve(
        reserve_checker,
        state_root=root,
        config_path=gateway_reserve_config,
        pair_cap_usd=Decimal(0),
    )
    selected_manifest = artifact_manifest
    current = root / "artifacts" / "current.json"
    if selected_manifest is None and current.is_file():
        selected_manifest = current
    if selected_manifest is not None:
        manifest = _validate_artifact_manifest(selected_manifest)
        artifact = manifest["artifact"]
        expected_target = _target_for_arch(docker_arch)
        if artifact.get("target") != expected_target:
            raise OracleError("artifact target does not match Docker architecture")
        checks["artifact"] = {
            "manifest": os.fspath(selected_manifest.resolve()),
            "sha256": artifact["sha256"],
            "target": artifact["target"],
        }
    return {"ok": True, "zero_cost": True, "state_root": os.fspath(root), "checks": checks}


def _git_build_provenance(runner: CommandRunner) -> dict[str, Any]:
    head = runner(("git", "rev-parse", "HEAD"), cwd=REPOSITORY_ROOT, timeout=30.0)
    status = runner(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        cwd=REPOSITORY_ROOT,
        timeout=30.0,
    )
    diff = runner(("git", "diff", "--binary", "HEAD"), cwd=REPOSITORY_ROOT, timeout=60.0)
    untracked = runner(
        ("git", "ls-files", "--others", "--exclude-standard", "-z"),
        cwd=REPOSITORY_ROOT,
        timeout=30.0,
    )
    if any(result.returncode for result in (head, status, diff, untracked)):
        raise OracleError("could not capture source provenance")
    untracked_paths = sorted(path for path in untracked.stdout.split("\0") if path)
    untracked_digest = hashlib.sha256()
    untracked_bytes = 0
    for relative_value in untracked_paths:
        relative = Path(relative_value)
        if relative.is_absolute() or ".." in relative.parts:
            raise OracleError("git reported an unsafe untracked path")
        candidate = REPOSITORY_ROOT / relative
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(REPOSITORY_ROOT.resolve())
        except (OSError, ValueError) as error:
            raise OracleError(
                "untracked source path is unavailable or escapes the repository"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OracleError("untracked source entries must be regular files")
        untracked_bytes += metadata.st_size
        if untracked_bytes > MAX_UNTRACKED_BYTES:
            raise OracleError("untracked source files exceed the provenance size limit")
        path_bytes = relative.as_posix().encode("utf-8")
        untracked_digest.update(len(path_bytes).to_bytes(8, "big"))
        untracked_digest.update(path_bytes)
        untracked_digest.update(metadata.st_size.to_bytes(8, "big"))
        with candidate.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                untracked_digest.update(chunk)
    return {
        "head": head.stdout.strip(),
        "dirty": bool(status.stdout),
        "status_sha256": hashlib.sha256(status.stdout.encode()).hexdigest(),
        "diff_sha256": hashlib.sha256(diff.stdout.encode()).hexdigest(),
        "status_entries": len(status.stdout.splitlines()),
        "untracked_content_sha256": untracked_digest.hexdigest(),
        "untracked_files": len(untracked_paths),
        "untracked_bytes": untracked_bytes,
    }


def _extract_built_artifact(archive: Path, expected_name: str, destination: Path) -> None:
    if archive.is_symlink() or not archive.is_file():
        raise OracleError("expected build archive is unavailable")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        executable_members = [member for member in members if member.name == expected_name]
        apple_double_name = f"._{expected_name}"
        apple_double_members = [member for member in members if member.name == apple_double_name]
        allowed_count = len(executable_members) + len(apple_double_members)
        if (
            len(executable_members) != 1
            or not executable_members[0].isfile()
            or len(apple_double_members) > 1
            or any(not member.isfile() for member in apple_double_members)
            or allowed_count != len(members)
        ):
            raise OracleError("build archive must contain exactly the expected executable")
        executable = executable_members[0]
        if executable.size <= 0 or executable.size > 256 * 1024 * 1024:
            raise OracleError("built executable has an invalid size")
        source = bundle.extractfile(executable)
        if source is None:
            raise OracleError("could not read built executable")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o755)
        os.replace(temporary, destination)
        destination.chmod(0o755)


def build_local_artifact(
    *, state_root: Path | None = None, runner: CommandRunner = _run_command
) -> dict[str, Any]:
    root = _state_root(state_root)
    architecture, docker_version = _docker_arch(runner)
    target = _target_for_arch(architecture)
    source = _git_build_provenance(runner)
    versions = {
        "python": platform.python_version(),
        "node": _command_version(runner, ("node", "--version")),
        "pnpm": _command_version(runner, ("pnpm", "--version")),
        "bun": _command_version(runner, ("bun", "--version")),
        "docker_server": docker_version,
        "docker_architecture": architecture,
    }
    environment = dict(os.environ)
    environment["STELE_BUILD_TARGETS"] = target
    built = runner(
        ("pnpm", "--filter", "./apps/cli", "run", "build:binaries"),
        cwd=REPOSITORY_ROOT,
        env=environment,
        timeout=1800.0,
    )
    if built.returncode:
        raise OracleError("local artifact build failed")
    executable_name = f"stele-{target}"
    archive = REPOSITORY_ROOT / "apps" / "cli" / "dist" / "bin" / f"{executable_name}.tar.gz"
    staging = root / "artifacts" / f".{executable_name}.building"
    staging.unlink(missing_ok=True)
    _extract_built_artifact(archive, executable_name, staging)
    digest = _sha256_file(staging)
    destination = root / "artifacts" / f"{executable_name}-{digest[:16]}"
    os.replace(staging, destination)
    destination.chmod(0o755)
    manifest = {
        "schema_version": 1,
        "created_at": _utc_now(),
        "source_kind": "local-dirty-worktree",
        "source": source,
        "runtime": versions,
        "artifact": {
            "path": os.fspath(destination.resolve()),
            "sha256": digest,
            "target": target,
            "bytes": destination.stat().st_size,
        },
    }
    manifest_path = root / "artifacts" / "current.json"
    _atomic_json(manifest_path, manifest)
    return {"ok": True, "manifest": os.fspath(manifest_path), "artifact": manifest["artifact"]}


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _condition_receipt(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    nested = value.get("receipt")
    return nested if isinstance(nested, dict) else value


# Receipt parsing lives in the package so the oracle and the durable result
# records read a run exactly the same way. Two parsers over one receipt drift,
# and the drift surfaces as a trend line that contradicts its own run.
_arm_metrics = metrics.arm_metrics


_arm_memory_evidence = metrics.memory_evidence


def _pair_deltas(control: Mapping[str, Any], faithful: Mapping[str, Any]) -> dict[str, Any] | None:
    identity_fields = ("model", "challenge_ids", "max_turns", "requested_budget_cap_usd_per_trial")
    if any(control.get(field) != faithful.get(field) for field in identity_fields):
        return None
    control_metrics = _arm_metrics(control)
    faithful_metrics = _arm_metrics(faithful)
    if control_metrics is None or faithful_metrics is None:
        return None
    shared = sorted(control_metrics.keys() & faithful_metrics.keys())
    absolute = {name: faithful_metrics[name] - control_metrics[name] for name in shared}
    reduction_percent = {
        name: (control_metrics[name] - faithful_metrics[name]) / control_metrics[name] * 100
        for name in shared
        if control_metrics[name] != 0 and name != "reward"
    }
    return {
        "control": control_metrics,
        "faithful": faithful_metrics,
        "faithful_minus_control": absolute,
        "faithful_reduction_percent": reduction_percent,
    }


def _runner_has_receipt_out() -> bool:
    try:
        return "--receipt-out" in RUNNER.read_text(encoding="utf-8")
    except OSError:
        return False


def _invoke_arm(
    *,
    condition: str,
    run_id: str,
    source: Path,
    revision: str,
    fixture: Path,
    challenge: str,
    artifact: Mapping[str, Any],
    backend_url: str,
    backend_key: str,
    model: str,
    max_turns: int,
    max_budget_usd: str,
    timeout_seconds: float,
    attempts: Path,
    work: Path,
    receipt_path: Path,
    runner: CommandRunner,
) -> dict[str, Any]:
    argv = [
        sys.executable,
        os.fspath(RUNNER),
        os.fspath(source),
        revision,
    ]
    # Every condition that installs Stele needs the graph, not just `faithful`.
    if condition in _GRAPH_CONDITIONS:
        argv.append(os.fspath(fixture))
    argv.extend(
        (
            "--condition",
            condition,
            "--challenge",
            challenge,
            "--attempt-root",
            os.fspath(attempts),
            "--work-root",
            os.fspath(work),
            "--run-id",
            run_id,
            "--model",
            model,
            "--max-turns",
            str(max_turns),
            "--max-budget-usd",
            max_budget_usd,
            "--timeout-seconds",
            str(timeout_seconds),
        )
    )
    if condition in _GRAPH_CONDITIONS:
        argv.extend(
            (
                "--provisioner",
                os.fspath(_provisioner_executable()),
                "--artifact",
                str(artifact["path"]),
                "--artifact-sha256",
                str(artifact["sha256"]),
                "--artifact-source-kind",
                "local-dirty-worktree",
                "--backend-url",
                backend_url,
                "--backend-anon-key",
                backend_key,
            )
        )
    receipt_supported = _runner_has_receipt_out()
    if receipt_supported:
        argv.extend(("--receipt-out", os.fspath(receipt_path)))
    result = runner(
        argv,
        cwd=REPOSITORY_ROOT / "eval" / "stele-bench",
        env=os.environ,
        timeout=timeout_seconds + 180.0,
    )
    parsed = (
        _read_json(receipt_path)
        if receipt_supported and receipt_path.is_file()
        else _last_json_object(result.stdout)
    )
    receipt = _condition_receipt(parsed)
    if receipt is not None and not receipt_path.is_file():
        _atomic_json(receipt_path, receipt)
    return {
        "run_id": run_id,
        "exit_code": result.returncode,
        "receipt": receipt,
        "receipt_path": os.fspath(receipt_path),
        "evidence_root": os.fspath(work),
        "error": None if result.returncode == 0 else "condition_runner_failed",
    }


def _invoke_borrowed_arm(
    *,
    condition: str,
    task: str,
    run_id: str,
    dataset: Path,
    artifact: Mapping[str, Any],
    backend_url: str,
    backend_key: str,
    model: str,
    max_turns: int,
    max_budget_usd: str,
    timeout_seconds: float,
    attempts: Path,
    work: Path,
    receipt_path: Path,
    runner: CommandRunner,
) -> dict[str, Any]:
    """Launch one arm of one borrowed task through the borrowed-task runner.

    The Stele-side inputs are passed only for the condition that installs it.
    Handing the control arm an artifact and a backend would make the two arms
    differ in more than the thing being measured, and the runner would have no
    way to tell that it had been mis-launched.
    """

    argv = [
        sys.executable,
        os.fspath(BORROWED_RUNNER),
        os.fspath(dataset),
        task,
        "--condition",
        condition,
        "--attempt-root",
        os.fspath(attempts),
        "--work-root",
        os.fspath(work),
        "--run-id",
        run_id,
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        max_budget_usd,
        "--timeout-seconds",
        str(timeout_seconds),
        "--receipt-out",
        os.fspath(receipt_path),
    ]
    if condition in _BORROWED_GRAPH_CONDITIONS:
        argv.extend(
            (
                "--artifact",
                str(artifact["path"]),
                "--artifact-sha256",
                str(artifact["sha256"]),
                "--artifact-source-kind",
                "local-dirty-worktree",
                "--backend-url",
                backend_url,
                "--backend-anon-key",
                backend_key,
            )
        )
    result = runner(
        argv,
        cwd=REPOSITORY_ROOT / "eval" / "stele-bench",
        env=os.environ,
        timeout=timeout_seconds + 180.0,
    )
    parsed = (
        _read_json(receipt_path) if receipt_path.is_file() else _last_json_object(result.stdout)
    )
    receipt = _condition_receipt(parsed)
    if receipt is not None and not receipt_path.is_file():
        _atomic_json(receipt_path, receipt)
    return {
        "run_id": run_id,
        "exit_code": result.returncode,
        "receipt": receipt,
        "receipt_path": os.fspath(receipt_path),
        "evidence_root": os.fspath(work),
        "error": None if result.returncode == 0 else "condition_runner_failed",
    }


def _invoke_compounding_chain(
    *,
    condition: str,
    run_id: str,
    source: Path,
    revision: str | None,
    chain_manifest: Path | None = None,
    session_count: int = 2,
    artifact: Mapping[str, Any],
    backend_url: str,
    backend_key: str,
    model: str,
    max_turns: int,
    max_budget_usd: str,
    timeout_seconds: float,
    attempts: Path,
    work: Path,
    receipt_path: Path,
    gateway_reserve_config: Path,
    runner: CommandRunner,
) -> dict[str, Any]:
    """Launch one chain; only the treatment receives persistent Stele state."""

    argv = [
        sys.executable,
        os.fspath(COMPOUNDING_RUNNER),
        os.fspath(source),
    ]
    if revision is not None:
        argv.append(revision)
    if chain_manifest is not None:
        argv.extend(("--chain-manifest", os.fspath(chain_manifest)))
    argv.extend(
        [
            "--condition",
            condition,
            "--attempt-root",
            os.fspath(attempts),
            "--work-root",
            os.fspath(work),
            "--chain-id",
            run_id,
            "--model",
            model,
            "--max-turns",
            str(max_turns),
            "--max-budget-usd",
            max_budget_usd,
            "--timeout-seconds",
            str(timeout_seconds),
            "--gateway-reserve-config",
            os.fspath(gateway_reserve_config),
            "--receipt-out",
            os.fspath(receipt_path),
        ]
    )
    if condition == "stele-fresh":
        argv.extend(
            (
                "--artifact",
                str(artifact["path"]),
                "--artifact-sha256",
                str(artifact["sha256"]),
                "--artifact-source-kind",
                "local-dirty-worktree",
                "--backend-url",
                backend_url,
                "--backend-anon-key",
                backend_key,
            )
        )
    result = runner(
        argv,
        cwd=BENCH_ROOT,
        env=os.environ,
        timeout=timeout_seconds * session_count + 300.0,
    )
    parsed = (
        _read_json(receipt_path) if receipt_path.is_file() else _last_json_object(result.stdout)
    )
    receipt = _condition_receipt(parsed)
    if receipt is not None and not receipt_path.is_file():
        _atomic_json(receipt_path, receipt)
    return {
        "run_id": run_id,
        "exit_code": result.returncode,
        "receipt": receipt,
        "receipt_path": os.fspath(receipt_path),
        "evidence_root": os.fspath(work),
        "error": None if result.returncode == 0 else "condition_runner_failed",
    }


def _validate_paid_budget(
    max_budget_usd: str, acknowledgement: str | None, arm_count: int
) -> tuple[Decimal, Decimal]:
    """Validate the per-arm budget and the exact-cap acknowledgement for N arms.

    Shared by run_pair (2 arms) and run_quad (4 arms) so the exact-cap
    discipline is identical: the operator must acknowledge the precise total
    the run can spend, which is what makes an accidental large run impossible.
    """

    try:
        budget = Decimal(max_budget_usd)
    except InvalidOperation as error:
        raise OracleError("max budget must be a decimal") from error
    if not budget.is_finite() or budget <= 0 or budget > 5:
        raise OracleError("max budget must be greater than zero and at most five")
    requested_cap = budget * arm_count
    try:
        acknowledged = Decimal(acknowledgement or "")
    except InvalidOperation as error:
        raise OracleError(
            f"paid run requires an acknowledgement matching the exact {arm_count}-arm requested cap"
        ) from error
    if not acknowledged.is_finite() or acknowledged != requested_cap:
        raise OracleError(
            f"paid run acknowledgement must match the exact {arm_count}-arm requested cap "
            f"of {requested_cap} USD and confirms {model_provider.key_env()} is a dedicated "
            "eval key with an external budget ceiling"
        )
    return budget, acknowledged


def _validate_compounding_budget(
    max_budget_usd: str, acknowledgement: str | None, attempts: int
) -> tuple[Decimal, Decimal]:
    """Validate the pre-registered four-session cap for each paired chain."""

    try:
        budget = Decimal(max_budget_usd)
    except InvalidOperation as error:
        raise OracleError("max budget must be a decimal") from error
    if not budget.is_finite() or budget != Decimal("10.00"):
        raise OracleError("the registered compounding run uses exactly $10.00 per session")
    requested_cap = budget * 4 * attempts
    try:
        acknowledged = Decimal(acknowledgement or "")
    except InvalidOperation as error:
        raise OracleError(
            "paid compounding run requires an acknowledgement matching the exact "
            "four-session-per-pair requested cap"
        ) from error
    if not acknowledged.is_finite() or acknowledged != requested_cap:
        raise OracleError(
            "paid compounding acknowledgement must match the exact requested cap of "
            f"{requested_cap} USD (budget x 4 sessions x {attempts} pairs)"
        )
    return budget, acknowledged


def _not_run_arm(run_id: str, receipt_path: Path, work: Path, error: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "exit_code": None,
        "receipt": None,
        "receipt_path": os.fspath(receipt_path),
        "evidence_root": os.fspath(work),
        "error": error,
    }


def _run_arm_sequence(
    arm_specs: Sequence[Mapping[str, Any]],
    *,
    run_id_prefix: str,
    run_root: Path,
    work: Path,
    receipt: dict[str, Any],
    receipt_path: Path,
    reserve: Mapping[str, Any],
    drain_checker: ReserveChecker,
    invoke: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], BaseException | None, str | None]:
    """Run each arm in order, re-checking shared Gateway drain between arms.

    Shared by every paid run shape so the money-fence behaviour is identical:
    the preflight proved the whole run fits above the reserve; this stops
    before the next arm if the balance has drained past the acknowledged cap.

    A ``spec`` always carries ``condition`` and ``label``; what else it needs
    is the run shape's business, so the whole spec is handed to ``invoke``
    rather than a fixed set of fields. A graph run puts a ``fixture`` in it; a
    borrowed-task run puts the task name in it instead.
    """

    arms: dict[str, Any] = {}
    drain_observations: list[dict[str, Any]] = []
    primary_error: str | None = None
    interrupted: BaseException | None = None
    for spec in arm_specs:
        label = str(spec["label"])
        run_id = f"{run_id_prefix}-{label}"
        arm_receipt_path = run_root / f"{label}.json"
        if arms:
            try:
                drain_observations.append(drain_checker(preflight=reserve))  # type: ignore[call-arg]
            except GatewayReserveError as error:
                observation = getattr(error, "observation", None)
                if isinstance(observation, dict):
                    drain_observations.append(observation)
                primary_error = primary_error or "gateway_drain_tripped"
                receipt["billing"]["gateway_drain_stop"] = str(error)
                arms[label] = _not_run_arm(run_id, arm_receipt_path, work, "gateway_drain_tripped")
                receipt["arms"] = arms
                receipt["billing"]["gateway_drain_checks"] = drain_observations
                _atomic_json(receipt_path, receipt)
                break
            receipt["billing"]["gateway_drain_checks"] = drain_observations
        try:
            arms[label] = invoke(
                spec=spec,
                run_id=run_id,
                receipt_path=arm_receipt_path,
            )
        except (OracleInterrupted, KeyboardInterrupt) as error:
            interrupted = error
            primary_error = "runner_interrupted"
            arms[label] = _not_run_arm(run_id, arm_receipt_path, work, "runner_interrupted")
            break
        except Exception as error:
            primary_error = primary_error or error.__class__.__name__
            arms[label] = _not_run_arm(run_id, arm_receipt_path, work, error.__class__.__name__)
        receipt["arms"] = arms
        _atomic_json(receipt_path, receipt)
    return arms, interrupted, primary_error


@dataclass(frozen=True)
class _PaidRunPreflight:
    """What a paid run proved before it created state or spent a token."""

    manifest_path: Path
    manifest: Mapping[str, Any]
    artifact: Mapping[str, Any]
    source_commit: str
    provenance: Mapping[str, Mapping[str, str]]
    certifications: Mapping[str, Mapping[str, Any]]
    backend_url: str
    backend_key: str
    supabase_version: str
    reserve: Mapping[str, Any]
    docker_architecture: str
    docker_server: str
    harbor: str
    dataset: Mapping[str, Any] | None = None

    def runtime(self) -> dict[str, str]:
        return {
            "python": platform.python_version(),
            "harbor": self.harbor,
            "supabase": self.supabase_version,
            "docker_architecture": self.docker_architecture,
            "docker_server": self.docker_server,
        }


def _prepare_paid_run(
    *,
    root: Path,
    artifact_manifest: Path | None,
    source: Path,
    revision: str,
    fixtures: Mapping[str, Path],
    challenge: str,
    acknowledged_cap: Decimal,
    gateway_reserve_config: Path | None,
    runner: CommandRunner,
    reserve_checker: ReserveChecker,
    dataset_provenance: Mapping[str, Any] | None = None,
) -> _PaidRunPreflight:
    """Prove a paid run is safe to start, before any state exists to clean up.

    Shared by run_pair, run_quad and run_repeat so all three refuse for the same
    reasons in the same order — an unverified artifact, a dirty or unpinned
    source, a fixture that fails certification, a shared Gateway balance that
    cannot absorb the acknowledged cap, or a host toolchain that does not match
    the artifact. Every check here runs BEFORE any directory is created and
    before a single token is spent, so a refusal leaves nothing behind.

    `fixtures` maps an arm label to the fixture to certify for it. Certification
    covers every distinct packaged graph: a leak in any one arm makes the whole
    comparison uninterpretable, so discovering it after the money is gone is too
    late.

    A borrowed-task run passes `dataset_provenance` instead: there is no graph
    to certify, and the equivalent proof — that the borrowed tasks still carry
    no reference solution and still hash to what was sanitised — is done by
    `_verify_borrowed_dataset` before this runs, because the task list it
    produces is what determines how many arms the run has and therefore what
    cap the reserve check below has to clear.
    """

    if not fixtures and dataset_provenance is None:
        raise OracleError("a paid run requires a fixture or a dataset to certify")
    manifest_path = artifact_manifest or root / "artifacts" / "current.json"
    manifest = _validate_artifact_manifest(manifest_path)
    artifact = manifest["artifact"]
    if dataset_provenance is None:
        source_commit = _validate_source(runner, source.resolve(), revision)
    else:
        # A borrowed run builds nothing from our corpus, so pinning a checkout
        # of it would be an irrelevant hurdle — and recording its commit as the
        # run's `source_commit` would imply a corpus that had no part in the
        # result. The dataset IS the corpus here, so its sanitisation digest is
        # what identifies it: stable across builds (unlike the artifact, which
        # is the variable under test) and exact about which tasks were run.
        source_commit = str(dataset_provenance["sanitisation_manifest_sha256"])

    provenance: dict[str, Mapping[str, str]] = {}
    certifications: dict[str, Mapping[str, Any]] = {}
    for label, fixture in fixtures.items():
        provenance[label] = _validate_fixture_and_challenge(fixture, challenge)
        certifications[label] = _certify_packaged_inputs(
            runner, source=source, revision=revision, fixture=fixture, challenge=challenge
        )

    backend_url, backend_key, supabase_version = _supabase_configuration(runner)
    # The acknowledged cap bounds what THIS run may spend; it says nothing about
    # what is left for production, which shares this Gateway balance. This is the
    # other half of the fence, and like certification it runs before any state
    # exists so a refusal leaves nothing to clean up.
    reserve = _check_reserve(
        reserve_checker,
        state_root=root,
        config_path=gateway_reserve_config,
        pair_cap_usd=acknowledged_cap,
    )
    docker_arch, docker_version = _docker_arch(runner)
    if artifact.get("target") != _target_for_arch(docker_arch):
        raise OracleError("artifact target does not match Docker architecture")
    harbor_version = _command_version(runner, ("harbor", "--version"))
    if HARBOR_VERSION.search(harbor_version) is None:
        raise OracleError(f"Harbor 0.20.x is required; found {harbor_version or 'unknown'}")

    return _PaidRunPreflight(
        manifest_path=manifest_path,
        manifest=manifest,
        artifact=artifact,
        source_commit=source_commit,
        provenance=provenance,
        certifications=certifications,
        backend_url=_docker_reachable_url(backend_url),
        backend_key=backend_key,
        supabase_version=supabase_version,
        reserve=reserve,
        docker_architecture=docker_arch,
        docker_server=docker_version,
        harbor=harbor_version,
        dataset=dataset_provenance,
    )


def run_pair(
    *,
    pair_id: str,
    source: Path = DEFAULT_SOURCE,
    revision: str = DEFAULT_REVISION,
    fixture: Path = DEFAULT_FIXTURE,
    challenge: str = DEFAULT_CHALLENGE,
    artifact_manifest: Path | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = 60,
    max_budget_usd: str = "5.00",
    acknowledge_paid_pair_cap_usd: str | None = None,
    gateway_reserve_config: Path | None = None,
    timeout_seconds: float = 900.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
) -> dict[str, Any]:
    pair_id = _safe_id(pair_id)
    budget, acknowledged_pair_cap = _validate_paid_budget(
        max_budget_usd, acknowledge_paid_pair_cap_usd, 2
    )
    if max_turns < 1 or max_turns > 100:
        raise OracleError("max turns must be between 1 and 100")
    root = _state_root(state_root)
    pair_root = root / "runs" / pair_id
    pair_receipt_path = pair_root / "pair.json"
    if pair_root.exists():
        raise OracleError("pair id already exists; inspect or clean it before retrying")
    prepared = _prepare_paid_run(
        root=root,
        artifact_manifest=artifact_manifest,
        source=source,
        revision=revision,
        fixtures={"faithful": fixture},
        challenge=challenge,
        acknowledged_cap=acknowledged_pair_cap,
        gateway_reserve_config=gateway_reserve_config,
        runner=runner,
        reserve_checker=reserve_checker,
    )
    manifest_path = prepared.manifest_path
    manifest = prepared.manifest
    artifact = prepared.artifact
    source_commit = prepared.source_commit
    challenge_provenance = prepared.provenance["faithful"]
    certification = prepared.certifications["faithful"]
    docker_backend_url = prepared.backend_url
    backend_key = prepared.backend_key
    reserve = prepared.reserve

    created_at = _utc_now()
    pair_root.mkdir(mode=0o700, parents=True)
    certification_path = pair_root / "certification.json"
    _atomic_json(certification_path, certification)
    attempts = root / "attempts"
    attempts.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = pair_root / "work"
    work.mkdir(mode=0o700)
    arms: dict[str, Any] = {}
    pair_receipt: dict[str, Any] = {
        "schema_version": 1,
        "pair_id": pair_id,
        "created_at": created_at,
        "completed_at": None,
        "status": "in_progress",
        "error": None,
        "configuration": {
            "task_image": harbor_pack.task_image(),
            "challenge_id": challenge,
            "source_commit": source_commit,
            "fixture_id": challenge_provenance["fixture_id"],
            "model": model,
            "max_turns": max_turns,
            "max_budget_usd_per_arm": str(budget),
            "acknowledged_paid_pair_cap_usd": str(acknowledged_pair_cap),
        },
        "arms": arms,
        "comparable": False,
        "deltas": None,
        "billing": {
            "gateway_total_usd": None,
            "source": None,
            "recorded_at": None,
            "claude_reported_usd_by_condition": {},
            "gateway_reserve_preflight": reserve,
            "gateway_drain_checks": [],
            "gateway_drain_stop": None,
        },
        "provenance": {
            "artifact_manifest": os.fspath(manifest_path.resolve()),
            "artifact_sha256": artifact["sha256"],
            "artifact_target": artifact["target"],
            "artifact_source": manifest.get("source"),
            "challenge_manifest_sha256": challenge_provenance["challenge_manifest_sha256"],
            "verifier_sha256": challenge_provenance["verifier_sha256"],
            "certification": {
                "path": os.fspath(certification_path),
                "sha256": _sha256_file(certification_path),
                "certified": certification.get("certified"),
                "arms": certification.get("arms"),
                "redacted_paths": [
                    str(record.get("path"))
                    for record in certification.get("redactions") or ()
                    if isinstance(record, Mapping)
                ],
            },
            "runtime": prepared.runtime(),
        },
    }
    _atomic_json(pair_receipt_path, pair_receipt)

    def _invoke_pair_arm(
        *, spec: Mapping[str, Any], run_id: str, receipt_path: Path
    ) -> dict[str, Any]:
        return _invoke_arm(
            condition=str(spec["condition"]),
            run_id=run_id,
            source=source.resolve(),
            revision=revision,
            fixture=Path(spec["fixture"]).resolve(),
            challenge=challenge,
            artifact=artifact,
            backend_url=docker_backend_url,
            backend_key=backend_key,
            model=model,
            max_turns=max_turns,
            max_budget_usd=str(budget),
            timeout_seconds=timeout_seconds,
            attempts=attempts,
            work=work,
            receipt_path=receipt_path,
            runner=runner,
        )

    arm_specs = (
        {"condition": "no-stele", "label": "control", "fixture": fixture},
        {"condition": "faithful", "label": "faithful", "fixture": fixture},
    )
    arms, interrupted, primary_error = _run_arm_sequence(
        arm_specs,
        run_id_prefix=pair_id,
        run_root=pair_root,
        work=work,
        receipt=pair_receipt,
        receipt_path=pair_receipt_path,
        reserve=reserve,
        drain_checker=drain_checker,
        invoke=_invoke_pair_arm,
    )

    for label in ("control", "faithful"):
        if label not in arms:
            arms[label] = _not_run_arm(
                f"{pair_id}-{label}", pair_root / f"{label}.json", work, "not_started"
            )

    control_receipt = arms["control"]["receipt"]
    faithful_receipt = arms["faithful"]["receipt"]
    arm_commands_succeeded = all(arms[label]["exit_code"] == 0 for label in ("control", "faithful"))
    deltas = (
        _pair_deltas(control_receipt, faithful_receipt)
        if arm_commands_succeeded
        and isinstance(control_receipt, Mapping)
        and isinstance(faithful_receipt, Mapping)
        else None
    )
    claude_costs: dict[str, float | None] = {}
    for label, receipt in (("control", control_receipt), ("faithful", faithful_receipt)):
        metrics = _arm_metrics(receipt) if isinstance(receipt, Mapping) else None
        claude_costs[label] = metrics.get("claude_reported_usd") if metrics else None
    successful = interrupted is None and arm_commands_succeeded
    pair_receipt.update(
        {
            "completed_at": _utc_now(),
            "status": (
                "interrupted"
                if interrupted is not None
                else "completed"
                if successful
                else "failed"
            ),
            "error": primary_error or (None if successful else "one_or_more_conditions_failed"),
            "arms": arms,
            "comparable": deltas is not None,
            "deltas": deltas,
        }
    )
    pair_receipt["billing"]["claude_reported_usd_by_condition"] = claude_costs
    _atomic_json(pair_receipt_path, pair_receipt)
    if interrupted is not None:
        raise interrupted
    return {"ok": successful, "pair_receipt": os.fspath(pair_receipt_path), "receipt": pair_receipt}


SESSION_BOUNDARY_VARIANTS = ("correct-anchor", "wrong-anchor", "no-anchor")


def _variant_fixture(cohort_root: Path, variant: str) -> Path:
    fixture = cohort_root / _safe_id(variant, "variant") / "fixture.json"
    if not fixture.is_file():
        raise OracleError(f"cohort is missing the {variant!r} variant fixture: {fixture}")
    return fixture


def run_quad(
    *,
    quad_id: str,
    cohort_root: Path,
    challenge: str,
    variants: Sequence[str] = SESSION_BOUNDARY_VARIANTS,
    source: Path = DEFAULT_SOURCE,
    revision: str = DEFAULT_REVISION,
    artifact_manifest: Path | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = 60,
    max_budget_usd: str = "5.00",
    acknowledge_paid_quad_cap_usd: str | None = None,
    gateway_reserve_config: Path | None = None,
    timeout_seconds: float = 900.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
) -> dict[str, Any]:
    """Run one shared no-stele control against N faithful graph-variant arms.

    A pair compares no-stele vs one faithful graph. A memory question like
    session-boundary-recall needs more: the SAME control against a
    correct-anchor graph (recall should fire), a wrong-anchor graph (a negative
    control that must not leak), and a no-anchor graph (what ordinary recall
    does with no structural hook). One control shared across all three keeps
    the comparison honest and cheaper than three separate pairs.
    """

    quad_id = _safe_id(quad_id)
    variant_list = tuple(dict.fromkeys(variants))
    if not variant_list:
        raise OracleError("a quad run requires at least one graph variant")
    arm_count = 1 + len(variant_list)
    budget, acknowledged_cap = _validate_paid_budget(
        max_budget_usd, acknowledge_paid_quad_cap_usd, arm_count
    )
    if max_turns < 1 or max_turns > 100:
        raise OracleError("max turns must be between 1 and 100")
    root = _state_root(state_root)
    quad_root = root / "runs" / quad_id
    quad_receipt_path = quad_root / "quad.json"
    if quad_root.exists():
        raise OracleError("quad id already exists; inspect or clean it before retrying")
    # Every variant is a distinct graph, so every variant is certified.
    # A missing variant fixture fails here, not mid-run.
    fixtures = {variant: _variant_fixture(cohort_root, variant) for variant in variant_list}
    prepared = _prepare_paid_run(
        root=root,
        artifact_manifest=artifact_manifest,
        source=source,
        revision=revision,
        fixtures=fixtures,
        challenge=challenge,
        acknowledged_cap=acknowledged_cap,
        gateway_reserve_config=gateway_reserve_config,
        runner=runner,
        reserve_checker=reserve_checker,
    )
    manifest_path = prepared.manifest_path
    manifest = prepared.manifest
    artifact = prepared.artifact
    source_commit = prepared.source_commit
    variant_provenance = prepared.provenance
    variant_certifications = prepared.certifications
    docker_backend_url = prepared.backend_url
    backend_key = prepared.backend_key
    reserve = prepared.reserve

    created_at = _utc_now()
    quad_root.mkdir(mode=0o700, parents=True)
    certification_path = quad_root / "certification.json"
    _atomic_json(certification_path, variant_certifications)
    attempts = root / "attempts"
    attempts.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = quad_root / "work"
    work.mkdir(mode=0o700)
    control_label = "no-stele"
    quad_receipt: dict[str, Any] = {
        "schema_version": 1,
        "quad_id": quad_id,
        "created_at": created_at,
        "completed_at": None,
        "status": "in_progress",
        "error": None,
        "configuration": {
            "task_image": harbor_pack.task_image(),
            "challenge_id": challenge,
            "source_commit": source_commit,
            "control_label": control_label,
            "variants": list(variant_list),
            "fixture_ids": {
                variant: variant_provenance[variant]["fixture_id"] for variant in variant_list
            },
            "model": model,
            "max_turns": max_turns,
            "max_budget_usd_per_arm": str(budget),
            "acknowledged_paid_quad_cap_usd": str(acknowledged_cap),
        },
        "arms": {},
        "comparable": False,
        "deltas": None,
        "billing": {
            "gateway_total_usd": None,
            "source": None,
            "recorded_at": None,
            "claude_reported_usd_by_condition": {},
            "gateway_reserve_preflight": reserve,
            "gateway_drain_checks": [],
            "gateway_drain_stop": None,
        },
        "provenance": {
            "artifact_manifest": os.fspath(manifest_path.resolve()),
            "artifact_sha256": artifact["sha256"],
            "artifact_target": artifact["target"],
            "artifact_source": manifest.get("source"),
            "certification": {
                "path": os.fspath(certification_path),
                "sha256": _sha256_file(certification_path),
                "certified": all(cert.get("certified") for cert in variant_certifications.values()),
                "by_variant": {
                    variant: {
                        "fixture_id": variant_provenance[variant]["fixture_id"],
                        "challenge_manifest_sha256": variant_provenance[variant][
                            "challenge_manifest_sha256"
                        ],
                        "verifier_sha256": variant_provenance[variant]["verifier_sha256"],
                        "certified": variant_certifications[variant].get("certified"),
                    }
                    for variant in variant_list
                },
            },
            "runtime": prepared.runtime(),
        },
        # Whether each faithful arm's prior-session lesson actually reached the
        # agent (vs. the agent rediscovering it) is decided post-run by the
        # runner, which scans the graded step's retained session transcript for
        # the fixture's memory marker. Populated from each arm receipt below.
        "memory_evidence": {variant: None for variant in variant_list},
    }
    _atomic_json(quad_receipt_path, quad_receipt)

    def _invoke_quad_arm(
        *, spec: Mapping[str, Any], run_id: str, receipt_path: Path
    ) -> dict[str, Any]:
        return _invoke_arm(
            condition=str(spec["condition"]),
            run_id=run_id,
            source=source.resolve(),
            revision=revision,
            fixture=Path(spec["fixture"]).resolve(),
            challenge=challenge,
            artifact=artifact,
            backend_url=docker_backend_url,
            backend_key=backend_key,
            model=model,
            max_turns=max_turns,
            max_budget_usd=str(budget),
            timeout_seconds=timeout_seconds,
            attempts=attempts,
            work=work,
            receipt_path=receipt_path,
            runner=runner,
        )

    # The control is fixture-independent (no-stele ignores the graph); it uses
    # the first variant's fixture path only to satisfy the runner's argument.
    control_fixture = fixtures[variant_list[0]]
    arm_specs = [{"condition": "no-stele", "label": control_label, "fixture": control_fixture}]
    arm_specs.extend(
        {"condition": "faithful", "label": variant, "fixture": fixtures[variant]}
        for variant in variant_list
    )
    arms, interrupted, primary_error = _run_arm_sequence(
        arm_specs,
        run_id_prefix=quad_id,
        run_root=quad_root,
        work=work,
        receipt=quad_receipt,
        receipt_path=quad_receipt_path,
        reserve=reserve,
        drain_checker=drain_checker,
        invoke=_invoke_quad_arm,
    )

    for label in (control_label, *variant_list):
        if label not in arms:
            arms[label] = _not_run_arm(
                f"{quad_id}-{label}", quad_root / f"{label}.json", work, "not_started"
            )

    control_receipt = arms[control_label]["receipt"]
    deltas: dict[str, Any] = {}
    claude_costs: dict[str, float | None] = {}
    control_metrics = (
        _arm_metrics(control_receipt) if isinstance(control_receipt, Mapping) else None
    )
    claude_costs[control_label] = (
        control_metrics.get("claude_reported_usd") if control_metrics else None
    )
    for variant in variant_list:
        variant_receipt = arms[variant]["receipt"]
        metrics = _arm_metrics(variant_receipt) if isinstance(variant_receipt, Mapping) else None
        claude_costs[variant] = metrics.get("claude_reported_usd") if metrics else None
        quad_receipt["memory_evidence"][variant] = _arm_memory_evidence(variant_receipt)
        if isinstance(control_receipt, Mapping) and isinstance(variant_receipt, Mapping):
            deltas[variant] = _pair_deltas(control_receipt, variant_receipt)
        else:
            deltas[variant] = None

    arm_commands_succeeded = all(
        arms[label]["exit_code"] == 0 for label in (control_label, *variant_list)
    )
    comparable = arm_commands_succeeded and all(delta is not None for delta in deltas.values())
    successful = interrupted is None and arm_commands_succeeded
    quad_receipt.update(
        {
            "completed_at": _utc_now(),
            "status": (
                "interrupted"
                if interrupted is not None
                else "completed"
                if successful
                else "failed"
            ),
            "error": primary_error or (None if successful else "one_or_more_conditions_failed"),
            "arms": arms,
            "comparable": comparable,
            "deltas": deltas,
        }
    )
    quad_receipt["billing"]["claude_reported_usd_by_condition"] = claude_costs
    _atomic_json(quad_receipt_path, quad_receipt)
    if interrupted is not None:
        raise interrupted
    return {"ok": successful, "quad_receipt": os.fspath(quad_receipt_path), "receipt": quad_receipt}


# Measures aggregated across a repeat. Binary ones get a rate, an interval and
# an exact test; continuous ones get a paired per-attempt difference and no
# test, because a continuous measure has no usable power at single-digit N.
_REPEAT_BINARY_MEASURES = ("cited_decision", "reward")
_REPEAT_PAIRED_MEASURES = (
    "investigative_tool_calls",
    "session_investigative_tool_calls",
    "tokens_input",
    "trial_wall_seconds",
)

# An arm that ran up against its own budget ceiling may have been cut off
# mid-answer, which scores like a failure to recall for a reason that has
# nothing to do with memory. Flag it rather than silently counting it.
_TRUNCATION_RISK_FRACTION = Decimal("0.9")
# The conditions a repeat may pair. `faithful-norag` is `faithful` with
# prompt-time injection stripped — the control for "is the graph worth having if
# nothing pushes it at you?" as opposed to "is the graph worth having at all?".
_REPEAT_CONDITIONS = ("no-stele", "faithful", "faithful-norag")
# Conditions that install Stele: these need a fixture restored per arm.
_GRAPH_CONDITIONS = ("faithful", "faithful-norag")


def _budget_truncation_risk(
    receipt: Mapping[str, Any] | None,
    metrics: Mapping[str, float] | None,
    fraction: Decimal,
) -> dict[str, Any] | None:
    """Flag an arm that may have been cut off at its own budget ceiling.

    Two signals, because they catch different things: the runner's own
    `claude_report_exceeded_requested_cap` is authoritative when the cap was
    actually hit, and the near-cap fraction is the early warning for an arm that
    came close enough that the next run could tip over.

    The ceiling is the per-trial cap times the number of billed steps, NOT the
    per-arm budget. A two-step challenge bills each step against the cap, so
    comparing an arm's whole cost against the single-step budget flags every
    multi-step arm that behaved perfectly normally.
    """

    reported = metrics.get("claude_reported_usd") if metrics else None
    exceeded = False
    effective_cap: Decimal | None = None
    if isinstance(receipt, Mapping):
        results = receipt.get("results")
        if isinstance(results, list) and len(results) == 1 and isinstance(results[0], Mapping):
            cost = results[0].get("cost")
            if isinstance(cost, Mapping):
                exceeded = cost.get("claude_report_exceeded_requested_cap") is True
                cap = cost.get("requested_claude_cli_cap_usd")
                steps = cost.get("billed_step_count")
                if isinstance(cap, (int, float)) and not isinstance(cap, bool):
                    step_count = steps if isinstance(steps, int) and steps > 0 else 1
                    effective_cap = Decimal(str(cap)) * step_count
    near_cap = (
        reported is not None
        and effective_cap is not None
        and Decimal(str(reported)) > effective_cap * fraction
    )
    if not exceeded and not near_cap:
        return None
    return {
        "claude_reported_usd": reported,
        "effective_cap_usd": str(effective_cap) if effective_cap is not None else None,
        "exceeded_requested_cap": exceeded,
        "above_warning_threshold": near_cap,
    }


def _measure_succeeded(value: Any) -> bool | None:
    """Treat a measure as a success only at a full 1.0.

    `bool(0.6)` is True, which would silently score a partial reward as a pass.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) >= 1.0


def _repeat_summary(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate paired attempts into rates with intervals and paired deltas.

    Only attempts where BOTH arms produced metrics are analysed; anything else
    is listed with its reason rather than quietly dropped, because a repeat that
    silently analyses 3 of 5 attempts reads exactly like one that ran 3.
    """

    analysable = [record for record in attempts if record.get("analysable")]
    excluded = [
        {
            "attempt": record.get("attempt"),
            "reason": record.get("excluded_reason") or "not_analysable",
        }
        for record in attempts
        if not record.get("analysable")
    ]

    binary: dict[str, Any] = {}
    for measure in _REPEAT_BINARY_MEASURES:
        control_values: list[bool] = []
        treatment_values: list[bool] = []
        for record in analysable:
            control = _measure_succeeded(record["metrics"]["control"].get(measure))
            treatment = _measure_succeeded(record["metrics"]["treatment"].get(measure))
            if control is None or treatment is None:
                continue
            control_values.append(control)
            treatment_values.append(treatment)
        binary[measure] = (
            statistics.binary_measure_summary(treatment=treatment_values, control=control_values)
            if control_values
            else None
        )

    paired: dict[str, Any] = {}
    for measure in _REPEAT_PAIRED_MEASURES:
        control_values_f: list[float] = []
        treatment_values_f: list[float] = []
        for record in analysable:
            control = record["metrics"]["control"].get(measure)
            treatment = record["metrics"]["treatment"].get(measure)
            if not isinstance(control, (int, float)) or not isinstance(treatment, (int, float)):
                continue
            control_values_f.append(float(control))
            treatment_values_f.append(float(treatment))
        paired[measure] = statistics.paired_measure_summary(
            treatment=treatment_values_f, control=control_values_f
        )

    return {
        "attempts_run": len(attempts),
        "attempts_analysed": len(analysable),
        "attempts_excluded": excluded,
        "binary_measures": binary,
        "paired_measures": paired,
    }


def run_repeat(
    *,
    repeat_id: str,
    attempts: int = 5,
    control_condition: str = "no-stele",
    treatment_condition: str = "faithful",
    source: Path = DEFAULT_SOURCE,
    revision: str = DEFAULT_REVISION,
    fixture: Path = DEFAULT_FIXTURE,
    challenge: str = DEFAULT_CHALLENGE,
    artifact_manifest: Path | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = 60,
    max_budget_usd: str = "5.00",
    acknowledge_paid_repeat_cap_usd: str | None = None,
    gateway_reserve_config: Path | None = None,
    timeout_seconds: float = 900.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
) -> dict[str, Any]:
    """Run N paired attempts of two conditions and report a rate, not a point.

    A single pair is one coin flip per arm. It can show that a memory mechanism
    fires; it cannot say how often, and a benchmark that reports 1.0 from one
    attempt has reported a coin flip. This runs the same pair N times under one
    certification and one money fence, keeps every attempt's evidence, and
    summarises the binary measures as rates with Wilson intervals and an exact
    one-sided test.

    The two conditions are arguments rather than fixtures of the design, because
    what counts as "the control" depends on the question being asked. Measuring
    what the graph is worth means comparing against no Stele at all; measuring
    what *proactive injection* is worth means comparing against an agent that has
    Stele and has to go and fetch (`faithful-norag`). Same pairing, same
    statistics, different control.

    Two things it does deliberately:

    - **Arm order alternates** (odd attempts control-first, even attempts
      treatment-first) so the arm is not confounded with position in the run —
      a later arm sees a later model-serving moment.
    - **Each attempt restores its own graph.** Every arm invocation carries its
      own run id, so the runner provisions and destroys a fresh project per
      attempt; no graph-condition attempt inherits another's state.
    """

    repeat_id = _safe_id(repeat_id, "repeat id")
    if attempts < 1 or attempts > 25:
        raise OracleError("a repeat must run between one and twenty-five attempts")
    for role, condition in (
        ("control", control_condition),
        ("treatment", treatment_condition),
    ):
        if condition not in _REPEAT_CONDITIONS:
            raise OracleError(f"{role} condition must be one of: {', '.join(_REPEAT_CONDITIONS)}")
    if control_condition == treatment_condition:
        # Two identical arms measure the model's run-to-run variance, not Stele.
        # That is a legitimate experiment, but it is not this one, and letting it
        # through here would produce a receipt that looks like a comparison.
        raise OracleError("control and treatment conditions must differ")
    arm_count = 2 * attempts
    budget, acknowledged_cap = _validate_paid_budget(
        max_budget_usd, acknowledge_paid_repeat_cap_usd, arm_count
    )
    if max_turns < 1 or max_turns > 100:
        raise OracleError("max turns must be between 1 and 100")
    root = _state_root(state_root)
    repeat_root = root / "runs" / repeat_id
    repeat_receipt_path = repeat_root / "repeat.json"
    if repeat_root.exists():
        raise OracleError("repeat id already exists; inspect or clean it before retrying")

    prepared = _prepare_paid_run(
        root=root,
        artifact_manifest=artifact_manifest,
        source=source,
        revision=revision,
        fixtures={"faithful": fixture},
        challenge=challenge,
        acknowledged_cap=acknowledged_cap,
        gateway_reserve_config=gateway_reserve_config,
        runner=runner,
        reserve_checker=reserve_checker,
    )
    challenge_provenance = prepared.provenance["faithful"]
    certification = prepared.certifications["faithful"]

    created_at = _utc_now()
    repeat_root.mkdir(mode=0o700, parents=True)
    certification_path = repeat_root / "certification.json"
    _atomic_json(certification_path, certification)
    attempt_root = root / "attempts"
    attempt_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = repeat_root / "work"
    work.mkdir(mode=0o700)

    # Odd attempts control-first, even attempts treatment-first.
    conditions = {"control": control_condition, "treatment": treatment_condition}
    arm_specs: list[dict[str, Any]] = []
    attempt_orders: dict[int, list[str]] = {}
    for index in range(1, attempts + 1):
        order = ["control", "treatment"] if index % 2 == 1 else ["treatment", "control"]
        attempt_orders[index] = order
        for role in order:
            arm_specs.append(
                {
                    "condition": conditions[role],
                    "label": f"attempt-{index:02d}-{role}",
                    "fixture": fixture,
                }
            )

    repeat_receipt: dict[str, Any] = {
        "schema_version": 1,
        "repeat_id": repeat_id,
        "created_at": created_at,
        "completed_at": None,
        "status": "in_progress",
        "error": None,
        "configuration": {
            "task_image": harbor_pack.task_image(),
            "challenge_id": challenge,
            "source_commit": prepared.source_commit,
            "fixture_id": challenge_provenance["fixture_id"],
            "model": model,
            "attempts": attempts,
            "arm_count": arm_count,
            "control_condition": control_condition,
            "treatment_condition": treatment_condition,
            "attempt_arm_order": {str(index): order for index, order in attempt_orders.items()},
            "max_turns": max_turns,
            "max_budget_usd_per_arm": str(budget),
            "acknowledged_paid_repeat_cap_usd": str(acknowledged_cap),
        },
        "arms": {},
        "attempts": [],
        "comparable": False,
        "summary": None,
        "billing": {
            "gateway_total_usd": None,
            "source": None,
            "recorded_at": None,
            "claude_reported_usd_by_condition": {},
            "gateway_reserve_preflight": prepared.reserve,
            "gateway_drain_checks": [],
            "gateway_drain_stop": None,
        },
        "provenance": {
            "artifact_manifest": os.fspath(prepared.manifest_path.resolve()),
            "artifact_sha256": prepared.artifact["sha256"],
            "artifact_target": prepared.artifact["target"],
            "artifact_source": prepared.manifest.get("source"),
            "challenge_manifest_sha256": challenge_provenance["challenge_manifest_sha256"],
            "verifier_sha256": challenge_provenance["verifier_sha256"],
            "certification": {
                "path": os.fspath(certification_path),
                "sha256": _sha256_file(certification_path),
                "certified": certification.get("certified"),
                "arms": certification.get("arms"),
                "redacted_paths": [
                    str(record.get("path"))
                    for record in certification.get("redactions") or ()
                    if isinstance(record, Mapping)
                ],
            },
            "runtime": prepared.runtime(),
        },
    }
    _atomic_json(repeat_receipt_path, repeat_receipt)

    def _invoke_repeat_arm(
        *, spec: Mapping[str, Any], run_id: str, receipt_path: Path
    ) -> dict[str, Any]:
        return _invoke_arm(
            condition=str(spec["condition"]),
            run_id=run_id,
            source=source.resolve(),
            revision=revision,
            fixture=Path(spec["fixture"]).resolve(),
            challenge=challenge,
            artifact=prepared.artifact,
            backend_url=prepared.backend_url,
            backend_key=prepared.backend_key,
            model=model,
            max_turns=max_turns,
            max_budget_usd=str(budget),
            timeout_seconds=timeout_seconds,
            attempts=attempt_root,
            work=work,
            receipt_path=receipt_path,
            runner=runner,
        )

    arms, interrupted, primary_error = _run_arm_sequence(
        arm_specs,
        run_id_prefix=repeat_id,
        run_root=repeat_root,
        work=work,
        receipt=repeat_receipt,
        receipt_path=repeat_receipt_path,
        reserve=prepared.reserve,
        drain_checker=drain_checker,
        invoke=_invoke_repeat_arm,
    )

    for spec in arm_specs:
        label = str(spec["label"])
        if label not in arms:
            arms[label] = _not_run_arm(
                f"{repeat_id}-{label}", repeat_root / f"{label}.json", work, "not_started"
            )

    truncation_fraction = _TRUNCATION_RISK_FRACTION
    attempt_records: list[dict[str, Any]] = []
    claude_costs: dict[str, float | None] = {}
    for index in range(1, attempts + 1):
        record: dict[str, Any] = {
            "attempt": index,
            "arm_order": attempt_orders[index],
            "labels": {},
            "metrics": {},
            "outcomes": {},
            "deltas": None,
            "memory_evidence": {},
            "budget_truncation_risk": [],
            "analysable": False,
            "excluded_reason": None,
        }
        receipts: dict[str, Any] = {}
        for role in ("control", "treatment"):
            arm_label = f"attempt-{index:02d}-{role}"
            record["labels"][role] = arm_label
            arm = arms[arm_label]
            receipt = arm["receipt"]
            metrics = _arm_metrics(receipt) if isinstance(receipt, Mapping) else None
            receipts[role] = receipt if arm["exit_code"] == 0 else None
            record["metrics"][role] = metrics or {}
            record["outcomes"][role] = (
                receipt.get("outcome") if isinstance(receipt, Mapping) else None
            )
            claude_costs[arm_label] = metrics.get("claude_reported_usd") if metrics else None
            risk = _budget_truncation_risk(receipt, metrics, truncation_fraction)
            if risk is not None:
                record["budget_truncation_risk"].append({"arm": role, **risk})
            # Per role, not just the treatment: when the control also has a graph
            # (`faithful-norag`), whether IT saw the memory is the measurement —
            # the question is what the agent does when nothing pushes context at
            # it, and that is unanswerable without the control's evidence.
            if conditions[role] in _GRAPH_CONDITIONS:
                record["memory_evidence"][role] = _arm_memory_evidence(receipt)
        invalid = sorted(
            role for role, outcome in record["outcomes"].items() if outcome == "invalid"
        )
        # An arm can exit cleanly and still report an infrastructure error, for
        # example when Claude Code aborts with OutputTokenExceededError. Its
        # verifier then scores an unfinished workspace, which is not a verdict.
        broken = sorted(
            role
            for role, outcome in record["outcomes"].items()
            if outcome == "infrastructure-error"
        )
        if receipts["control"] is None or receipts["treatment"] is None:
            record["excluded_reason"] = "arm_did_not_complete"
        elif broken:
            record["excluded_reason"] = f"arm_infrastructure_error:{','.join(broken)}"
        elif invalid:
            # An arm the runner itself marked invalid did not produce a
            # trustworthy verdict, so the pair is not a measurement. Excluding
            # it here is what the pre-registration promises; leaving it in
            # would quietly analyse an attempt the apparatus disowned.
            record["excluded_reason"] = f"arm_outcome_invalid:{','.join(invalid)}"
        else:
            record["deltas"] = _pair_deltas(receipts["control"], receipts["treatment"])
            if record["deltas"] is None:
                record["excluded_reason"] = "arms_are_not_comparable"
            else:
                record["analysable"] = True
        attempt_records.append(record)

    summary = _repeat_summary(attempt_records)
    arm_commands_succeeded = all(arm["exit_code"] == 0 for arm in arms.values())
    successful = interrupted is None and arm_commands_succeeded
    repeat_receipt.update(
        {
            "completed_at": _utc_now(),
            "status": (
                "interrupted"
                if interrupted is not None
                else "completed"
                if successful
                else "failed"
            ),
            "error": primary_error or (None if successful else "one_or_more_conditions_failed"),
            "arms": arms,
            "attempts": attempt_records,
            "comparable": summary["attempts_analysed"] == attempts,
            "summary": summary,
        }
    )
    repeat_receipt["billing"]["claude_reported_usd_by_condition"] = claude_costs
    _atomic_json(repeat_receipt_path, repeat_receipt)
    if interrupted is not None:
        raise interrupted
    return {
        "ok": successful,
        "repeat_receipt": os.fspath(repeat_receipt_path),
        "receipt": repeat_receipt,
    }


def _bootstrap_mean_interval(
    differences: Sequence[float], *, samples: int = 20_000
) -> list[float] | None:
    """Deterministic paired percentile bootstrap for the pre-registered N=10 estimate."""

    if not differences:
        return None
    rng = random.Random(2493)
    count = len(differences)
    means = sorted(
        sum(differences[rng.randrange(count)] for _ in range(count)) / count for _ in range(samples)
    )
    return [means[int(0.025 * (samples - 1))], means[int(0.975 * (samples - 1))]]


def _compounding_measure_summary(
    treatment: Sequence[float], control: Sequence[float]
) -> dict[str, Any] | None:
    if not treatment or len(treatment) != len(control):
        return None
    paired = statistics.paired_measure_summary(treatment=treatment, control=control)
    assert paired is not None
    differences = [t - c for t, c in zip(treatment, control, strict=True)]
    paired["mean_difference"] = sum(differences) / len(differences)
    paired["paired_bootstrap_mean_difference_95"] = _bootstrap_mean_interval(differences)
    paired["bootstrap_samples"] = 20_000
    paired["bootstrap_seed"] = 2493
    return paired


def _compounding_chain_metric(receipt: Mapping[str, Any], path: Sequence[str]) -> float | None:
    value: object = receipt
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _compounding_pair_valid(
    control: Mapping[str, Any] | None,
    treatment: Mapping[str, Any] | None,
    *,
    admission: bool,
) -> str | None:
    if control is None or treatment is None:
        return "chain_did_not_complete"
    for role, chain in (("control", control), ("treatment", treatment)):
        if chain.get("outcome") != "completed" or chain.get("cleanup_completed") is not True:
            return f"{role}_chain_invalid"
        boundary = chain.get("boundary")
        if (
            not isinstance(boundary, Mapping)
            or boundary.get("only_graph_crossed_boundary") is not True
        ):
            return f"{role}_boundary_unproven"
        for session_name in ("session_1", "session_2"):
            session = chain.get(session_name)
            challenge = session.get("challenge") if isinstance(session, Mapping) else None
            if not isinstance(challenge, Mapping) or challenge.get("outcome") in {
                "invalid",
                "infrastructure-error",
            }:
                return f"{role}_{session_name}_invalid"
            billing = session.get("gateway_billing") if isinstance(session, Mapping) else None
            delta = billing.get("delta_usd") if isinstance(billing, Mapping) else None
            try:
                observed_cost = Decimal(str(delta))
            except InvalidOperation:
                return f"{role}_{session_name}_gateway_cost_unobserved"
            if not observed_cost.is_finite() or observed_cost < 0:
                return f"{role}_{session_name}_gateway_cost_invalid"
        resolved = {
            session.get("resolved_model")
            for session_name in ("session_1", "session_2")
            if isinstance((session := chain.get(session_name)), Mapping)
        }
        if None in resolved or len(resolved) != 1:
            return f"{role}_model_unresolved_or_changed"
    all_models = {
        chain[session_name]["resolved_model"]
        for chain in (control, treatment)
        for session_name in ("session_1", "session_2")
    }
    if len(all_models) != 1:
        return "model_changed_between_arms"
    if admission:
        if control.get("session_1_correct") is not True:
            return "control_session_1_answer_incorrect"
        if treatment.get("session_1_correct") is not True:
            return "treatment_session_1_answer_incorrect"
        for role, chain in (("control", control), ("treatment", treatment)):
            if _compounding_chain_metric(chain, ("cumulative", "gateway_observed_usd")) in {
                None,
                0.0,
            }:
                return f"{role}_gateway_cost_did_not_settle"
    return None


def _compounding_summary(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    analysed = [attempt for attempt in attempts if attempt.get("analysable")]
    reward_control: list[bool] = []
    reward_treatment: list[bool] = []
    measures = {
        "cumulative_gateway_observed_usd": ("cumulative", "gateway_observed_usd"),
        "cumulative_claude_reported_usd": ("cumulative", "claude_reported_usd"),
        "cumulative_outer_harbor_wall_seconds": (
            "cumulative",
            "outer_harbor_wall_seconds",
        ),
        "session_2_first_live_edit_tool_call": ("first_live_edit", "tool_call_index"),
        "session_2_investigative_calls_before_live_edit": (
            "first_live_edit",
            "investigative_calls_before",
        ),
    }
    paired_values: dict[str, dict[str, list[float]]] = {
        name: {"control": [], "treatment": []} for name in measures
    }
    intake = {"written": 0, "relevant": 0, "deliverable": 0}
    for attempt in analysed:
        control = attempt["receipts"]["control"]
        treatment = attempt["receipts"]["treatment"]
        reward_control.append(control.get("session_2_reward") == 1.0)
        reward_treatment.append(treatment.get("session_2_reward") == 1.0)
        graph_intake = treatment.get("graph_intake")
        if isinstance(graph_intake, Mapping):
            for name in intake:
                intake[name] += int(graph_intake.get(name) is True)
        for name, path in measures.items():
            control_value = _compounding_chain_metric(control, path)
            treatment_value = _compounding_chain_metric(treatment, path)
            if control_value is not None and treatment_value is not None:
                paired_values[name]["control"].append(control_value)
                paired_values[name]["treatment"].append(treatment_value)

    reward = (
        statistics.binary_measure_summary(treatment=reward_treatment, control=reward_control)
        if analysed
        else None
    )
    if reward is not None:
        reverse = statistics.mcnemar_exact_greater(
            treatment=reward_control, control=reward_treatment
        )
        forward = float(reward["mcnemar_one_sided_p"])
        reward["mcnemar_two_sided_p"] = min(1.0, 2.0 * min(forward, reverse))
    paired = {
        name: _compounding_measure_summary(values["treatment"], values["control"])
        for name, values in paired_values.items()
    }
    gateway = paired["cumulative_gateway_observed_usd"]
    treatment_reward_not_lower = sum(reward_treatment) >= sum(reward_control) if analysed else None
    observed = bool(
        gateway is not None and treatment_reward_not_lower and gateway["mean_difference"] <= 0
    )
    interval = gateway.get("paired_bootstrap_mean_difference_95") if gateway is not None else None
    established = bool(
        observed and isinstance(interval, list) and len(interval) == 2 and interval[1] <= 0
    )
    return {
        "attempts_run": len(attempts),
        "attempts_analysed": len(analysed),
        "attempts_excluded": [
            {"attempt": value.get("attempt"), "reason": value.get("excluded_reason")}
            for value in attempts
            if not value.get("analysable")
        ],
        "session_2_reward": reward,
        "paired_measures": paired,
        "graph_intake": {
            name: {"successes": count, "trials": len(analysed)} for name, count in intake.items()
        },
        "payback_by_first_reuse": {
            "observed": observed,
            "established": established,
            "treatment_reward_not_lower": treatment_reward_not_lower,
            "verdict": (
                "established"
                if established
                else "observed_not_established"
                if observed
                else "not_established_by_first_reuse"
            ),
        },
    }


def _chain_session_vectors(
    chain: Mapping[str, Any],
    *,
    session_count: int,
    expected_model: str = DEFAULT_COMPOUNDING_MODEL,
) -> tuple[list[Decimal], list[int]] | None:
    if (
        chain.get("outcome") != "completed"
        or chain.get("cleanup_completed") is not True
        or not isinstance(chain.get("boundary"), Mapping)
        or chain["boundary"].get("only_graph_crossed_boundary") is not True
    ):
        return None
    sessions = chain.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != session_count:
        return None
    costs: list[Decimal] = []
    rewards: list[int] = []
    for session in sessions:
        if not isinstance(session, Mapping) or session.get("resolved_model") != expected_model:
            return None
        challenge = session.get("challenge")
        if not isinstance(challenge, Mapping) or challenge.get("outcome") in {
            "invalid",
            "infrastructure-error",
        }:
            return None
        reward = challenge.get("reward")
        if reward not in {0, 0.0, 1, 1.0}:
            return None
        billing = session.get("gateway_billing")
        if (
            not isinstance(billing, Mapping)
            or billing.get("exclusive_to_interval") is not True
            or billing.get("source") != "vercel-ai-gateway-quota"
        ):
            return None
        try:
            cost = Decimal(str(billing.get("delta_usd")))
        except InvalidOperation:
            return None
        if not cost.is_finite() or cost < 0:
            return None
        costs.append(cost)
        rewards.append(1 if float(reward) == 1.0 else 0)
    return costs, rewards


def _k5_session_vectors(
    chain: Mapping[str, Any], *, expected_model: str = DEFAULT_COMPOUNDING_MODEL
) -> tuple[list[Decimal], list[int]] | None:
    return _chain_session_vectors(
        chain,
        session_count=K5_SESSION_COUNT,
        expected_model=expected_model,
    )


def _continuity_session_vectors(
    chain: Mapping[str, Any], *, expected_model: str = DEFAULT_COMPOUNDING_MODEL
) -> tuple[list[Decimal], list[int]] | None:
    return _chain_session_vectors(
        chain,
        session_count=CONTINUITY_SESSION_COUNT,
        expected_model=expected_model,
    )


def _chain_input_tokens(chain: Mapping[str, Any], *, session_count: int) -> list[int] | None:
    sessions = chain.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != session_count:
        return None
    values: list[int] = []
    for session in sessions:
        challenge = session.get("challenge") if isinstance(session, Mapping) else None
        tokens = challenge.get("tokens") if isinstance(challenge, Mapping) else None
        value = tokens.get("input") if isinstance(tokens, Mapping) else None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        values.append(value)
    return values


def _k5_pair_valid(
    control: Mapping[str, Any] | None,
    treatment: Mapping[str, Any] | None,
    *,
    admission: bool,
) -> str | None:
    if control is None or treatment is None:
        return "chain_did_not_complete"
    control_vectors = _k5_session_vectors(control)
    treatment_vectors = _k5_session_vectors(treatment)
    if control_vectors is None:
        return "control_chain_or_exclusive_billing_invalid"
    if treatment_vectors is None:
        return "treatment_chain_or_exclusive_billing_invalid"
    if not admission:
        return None
    if sum(control_vectors[1]) < 4:
        return "control_passed_fewer_than_four_tasks"
    if sum(treatment_vectors[1]) < 4:
        return "treatment_passed_fewer_than_four_tasks"
    activity = treatment.get("graph_activity")
    if not isinstance(activity, list) or not any(
        isinstance(item, Mapping)
        and item.get("session_number") in {1, 2, 3, 4}
        and item.get("written") is True
        for item in activity
    ):
        return "treatment_created_no_pre_reuse_graph_write"
    delivery = treatment.get("memory_delivery")
    if not isinstance(delivery, list) or not any(
        isinstance(item, Mapping)
        and item.get("session_number") in {2, 3, 4, 5}
        and item.get("delivered_prior_node") is True
        for item in delivery
    ):
        return "treatment_delivered_no_exact_prior_node_version"
    treatment_after_two = sum(treatment_vectors[0][:2])
    control_after_two = sum(control_vectors[0][:2])
    deficit = treatment_after_two - control_after_two
    remaining_control = sum(control_vectors[0][2:])
    if deficit >= remaining_control:
        return "k5_crossover_infeasible_in_admission"
    return None


def _continuity_pair_valid(
    control: Mapping[str, Any] | None,
    treatment: Mapping[str, Any] | None,
    *,
    admission: bool,
) -> str | None:
    """Validate apparatus only; all behavioral outcomes remain analysable."""

    del admission
    if control is None or treatment is None:
        return "chain_did_not_complete"
    if _continuity_session_vectors(control) is None:
        return "control_chain_or_exclusive_billing_invalid"
    if _continuity_session_vectors(treatment) is None:
        return "treatment_chain_or_exclusive_billing_invalid"
    return None


def _exact_negative_sign_p(negative_count: int, total: int) -> float:
    if total < 1 or not 0 <= negative_count <= total:
        raise ValueError("invalid exact sign-test counts")
    return sum(comb(total, index) for index in range(negative_count, total + 1)) / 2**total


def _holm_adjust(raw_values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(raw_values), key=lambda item: (item[1], item[0]))
    adjusted = [1.0] * len(raw_values)
    running = 0.0
    total = len(raw_values)
    for rank, (index, value) in enumerate(ordered):
        running = max(running, min(1.0, (total - rank) * value))
        adjusted[index] = running
    return adjusted


def _sample_median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _bootstrap_median_interval(
    differences: Sequence[float], *, samples: int = 20_000
) -> list[float] | None:
    if not differences:
        return None
    rng = random.Random(2521)
    count = len(differences)
    medians = sorted(
        _sample_median([differences[rng.randrange(count)] for _ in range(count)])
        for _ in range(samples)
    )
    return [medians[int(0.025 * (samples - 1))], medians[int(0.975 * (samples - 1))]]


def _chain_crossover_summary(
    attempts: Sequence[Mapping[str, Any]],
    *,
    session_count: int,
    cohort_attempts: int,
    minimum_arm_pass_rate: float,
) -> dict[str, Any]:
    valid: list[tuple[list[Decimal], list[int], list[Decimal], list[int]]] = []
    valid_tokens: list[tuple[list[int], list[int]]] = []
    for attempt in attempts:
        receipts = attempt.get("receipts")
        if not isinstance(receipts, Mapping):
            continue
        control = receipts.get("control")
        treatment = receipts.get("treatment")
        if not isinstance(control, Mapping) or not isinstance(treatment, Mapping):
            continue
        control_vectors = _chain_session_vectors(control, session_count=session_count)
        treatment_vectors = _chain_session_vectors(treatment, session_count=session_count)
        if control_vectors is not None and treatment_vectors is not None:
            valid.append((*control_vectors, *treatment_vectors))
            control_tokens = _chain_input_tokens(control, session_count=session_count)
            treatment_tokens = _chain_input_tokens(treatment, session_count=session_count)
            if control_tokens is not None and treatment_tokens is not None:
                valid_tokens.append((control_tokens, treatment_tokens))

    complete = len(attempts) == cohort_attempts and len(valid) == cohort_attempts
    prefixes: list[dict[str, Any]] = []
    raw_p: list[float] = []
    for prefix in range(1, session_count + 1):
        differences = [
            float(sum(treatment_costs[:prefix]) - sum(control_costs[:prefix]))
            for control_costs, _control_rewards, treatment_costs, _treatment_rewards in valid
        ]
        token_differences = [
            sum(treatment_tokens[:prefix]) - sum(control_tokens[:prefix])
            for control_tokens, treatment_tokens in valid_tokens
        ]
        negative = sum(value < 0 for value in differences)
        p_value = _exact_negative_sign_p(negative, len(differences)) if differences else 1.0
        raw_p.append(p_value)
        denominator = len(valid) * prefix
        control_passes = sum(
            sum(control_rewards[:prefix])
            for _control_costs, control_rewards, _treatment_costs, _treatment_rewards in valid
        )
        treatment_passes = sum(
            sum(treatment_rewards[:prefix])
            for _control_costs, _control_rewards, _treatment_costs, treatment_rewards in valid
        )
        control_rate = control_passes / denominator if denominator else None
        treatment_rate = treatment_passes / denominator if denominator else None
        difference_rate = (
            treatment_rate - control_rate
            if treatment_rate is not None and control_rate is not None
            else None
        )
        prefixes.append(
            {
                "k": prefix,
                "paired_differences_usd": differences,
                "negative": negative,
                "zero": sum(value == 0 for value in differences),
                "positive": sum(value > 0 for value in differences),
                "median_difference_usd": _sample_median(differences) if differences else None,
                "paired_bootstrap_median_difference_95": _bootstrap_median_interval(
                    differences
                ),
                "bootstrap_samples": 20_000,
                "bootstrap_seed": 2521,
                "paired_input_token_differences": token_differences,
                "input_token_pairs": len(token_differences),
                "median_input_token_difference": (
                    _sample_median(token_differences) if token_differences else None
                ),
                "paired_bootstrap_median_input_token_difference_95": (
                    _bootstrap_median_interval(token_differences)
                ),
                "mean_difference_usd": (
                    sum(differences) / len(differences) if differences else None
                ),
                "raw_one_sided_sign_p": p_value,
                "control_pass_rate": control_rate,
                "treatment_pass_rate": treatment_rate,
                "treatment_minus_control_pass_rate": difference_rate,
                "correctness_guard_at_prefix": bool(
                    control_rate is not None
                    and treatment_rate is not None
                    and difference_rate is not None
                    and control_rate >= minimum_arm_pass_rate
                    and treatment_rate >= minimum_arm_pass_rate
                    and difference_rate >= -0.10
                ),
            }
        )
    adjusted = _holm_adjust(raw_p)
    guard_through = True
    earliest: int | None = None
    for record, adjusted_p in zip(prefixes, adjusted, strict=True):
        record["holm_adjusted_sign_p"] = adjusted_p
        guard_through = guard_through and record["correctness_guard_at_prefix"]
        record["correctness_guard_through_prefix"] = guard_through
        if (
            earliest is None
            and complete
            and record["median_difference_usd"] is not None
            and record["median_difference_usd"] < 0
            and adjusted_p < 0.05
            and guard_through
        ):
            earliest = int(record["k"])
    return {
        "attempts_registered": len(attempts),
        "pairs_with_complete_exclusive_receipts": len(valid),
        "crossover_inference_available": complete,
        "prefixes": prefixes,
        "earliest_established_crossover_session": earliest,
        "verdict": (
            f"crossover_established_at_session_{earliest}"
            if earliest is not None
            else f"crossover_not_established_by_session_{session_count}"
            if complete
            else "incomplete_no_crossover_claim"
        ),
    }


def _k5_summary(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return _chain_crossover_summary(
        attempts,
        session_count=K5_SESSION_COUNT,
        cohort_attempts=K5_COHORT_ATTEMPTS,
        minimum_arm_pass_rate=0.80,
    )


def _continuity_summary(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary = _chain_crossover_summary(
        attempts,
        session_count=CONTINUITY_SESSION_COUNT,
        cohort_attempts=CONTINUITY_COHORT_ATTEMPTS,
        minimum_arm_pass_rate=0.50,
    )
    valid = [attempt for attempt in attempts if attempt.get("analysable") is True]

    def chains(role: str) -> list[Mapping[str, Any]]:
        selected: list[Mapping[str, Any]] = []
        for attempt in valid:
            receipts = attempt.get("receipts")
            chain = receipts.get(role) if isinstance(receipts, Mapping) else None
            if isinstance(chain, Mapping):
                selected.append(chain)
        return selected

    control = chains("control")
    treatment = chains("treatment")

    def response_markers(chain: Mapping[str, Any], session_number: int) -> set[str]:
        sessions = chain.get("sessions")
        if not isinstance(sessions, list) or len(sessions) < session_number:
            return set()
        session = sessions[session_number - 1]
        challenge = session.get("challenge") if isinstance(session, Mapping) else None
        evidence = challenge.get("evidence") if isinstance(challenge, Mapping) else None
        response = evidence.get("response_evidence") if isinstance(evidence, Mapping) else None
        found = response.get("markers_found") if isinstance(response, Mapping) else None
        return {str(value) for value in found} if isinstance(found, list) else set()

    def wrote_in_session_one(chain: Mapping[str, Any]) -> bool:
        activity = chain.get("graph_activity")
        return bool(
            isinstance(activity, list)
            and any(
                isinstance(item, Mapping)
                and item.get("session_number") == 1
                and item.get("written") is True
                for item in activity
            )
        )

    def delivered_later(chain: Mapping[str, Any]) -> bool:
        delivery = chain.get("memory_delivery")
        return bool(
            isinstance(delivery, list)
            and any(
                isinstance(item, Mapping)
                and item.get("session_number") in {2, 3, 4}
                and item.get("delivered_prior_node") is True
                for item in delivery
            )
        )

    def arm_rate(values: Sequence[bool]) -> dict[str, Any]:
        successes = sum(values)
        low, high = statistics.wilson_interval(successes, len(values))
        return {
            "successes": successes,
            "trials": len(values),
            "rate": successes / len(values) if values else None,
            "wilson_95": [low, high],
        }

    release_markers = {
        "single-file Python 3.11",
        "standard library only",
        "no network access",
        "no watch mode",
        "include-events",
        "50 KiB",
    }
    summary["descriptive_memory_outcomes"] = {
        "treatment_session_1_graph_write": arm_rate(
            [wrote_in_session_one(chain) for chain in treatment]
        ),
        "treatment_later_exact_version_delivery": arm_rate(
            [delivered_later(chain) for chain in treatment]
        ),
        "session_2_milestone_recovery": statistics.binary_measure_summary(
            treatment=[bool(response_markers(chain, 2)) for chain in treatment],
            control=[bool(response_markers(chain, 2)) for chain in control],
        ),
        "session_3_conflict_surfacing": statistics.binary_measure_summary(
            treatment=[bool(response_markers(chain, 3)) for chain in treatment],
            control=[bool(response_markers(chain, 3)) for chain in control],
        ),
        "session_4_release_constraint_recovery": statistics.binary_measure_summary(
            treatment=[release_markers <= response_markers(chain, 4) for chain in treatment],
            control=[release_markers <= response_markers(chain, 4) for chain in control],
        ),
    }
    return summary


def _load_registered_chain_manifest(
    path: Path,
    *,
    experiment_id: str,
    session_count: int,
    label: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    resolved = path.resolve(strict=True)
    payload = _read_json(resolved)
    if payload.get("schema_version") != 1 or payload.get("experiment_id") != experiment_id:
        raise OracleError(f"the {label} chain manifest identity is invalid")
    runtime = payload.get("runtime")
    if (
        not isinstance(runtime, Mapping)
        or runtime.get("model") != DEFAULT_COMPOUNDING_MODEL
        or runtime.get("harness") != "claude-code"
    ):
        raise OracleError(f"the {label} chain manifest runtime is not the registered runtime")
    sessions = payload.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != session_count:
        raise OracleError(
            f"the {label} chain manifest must contain exactly {session_count} sessions"
        )
    normalized: list[dict[str, Any]] = []
    for index, session in enumerate(sessions, 1):
        if not isinstance(session, Mapping) or session.get("index") != index:
            raise OracleError(f"the {label} chain manifest session order is invalid")
        challenge_id = session.get("challenge_id")
        base = session.get("base_revision")
        target = session.get("canonical_target_revision")
        manifest_sha = session.get("challenge_manifest_sha256")
        verifier_sha = session.get("verifier_sha256")
        if not all(isinstance(value, str) for value in (challenge_id, base, target)):
            raise OracleError(f"the {label} chain manifest has a missing session identity")
        if not isinstance(manifest_sha, str) or not SHA256.fullmatch(manifest_sha):
            raise OracleError(f"the {label} challenge manifest digest is invalid")
        if not isinstance(verifier_sha, str) or not SHA256.fullmatch(verifier_sha):
            raise OracleError(f"the {label} verifier digest is invalid")
        challenge_path = CHALLENGES_ROOT / challenge_id / "challenge.json"
        challenge = _read_json(challenge_path)
        verifier = challenge.get("verifier")
        asset = verifier.get("asset") if isinstance(verifier, Mapping) else None
        if _sha256_file(challenge_path) != manifest_sha:
            raise OracleError(f"a registered {label} challenge manifest changed")
        if not isinstance(asset, Mapping) or asset.get("sha256") != verifier_sha:
            raise OracleError(f"a registered {label} verifier changed")
        normalized.append(dict(session))
    return payload, normalized


def _load_k5_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return _load_registered_chain_manifest(
        path,
        experiment_id="click-clirunner-k5-v1",
        session_count=K5_SESSION_COUNT,
        label="K=5",
    )


def _load_continuity_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload, sessions = _load_registered_chain_manifest(
        path,
        experiment_id="fieldnote-continuity-v8",
        session_count=CONTINUITY_SESSION_COUNT,
        label="Fieldnote continuity",
    )
    binding = payload.get("treatment_artifact")
    if (
        not isinstance(binding, Mapping)
        or not isinstance(binding.get("source_commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", str(binding["source_commit"]))
        or not isinstance(binding.get("sha256"), str)
        or not SHA256.fullmatch(str(binding["sha256"]))
        or binding.get("target") not in {"linux-arm64", "linux-x64"}
    ):
        raise OracleError("the Fieldnote continuity treatment artifact binding is invalid")
    return payload, sessions


def _validate_registered_artifact_binding(
    manifest_payload: Mapping[str, Any],
    *,
    artifact_manifest: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> None:
    binding = manifest_payload.get("treatment_artifact")
    if not isinstance(binding, Mapping):
        return
    source = artifact_manifest.get("source")
    if not isinstance(source, Mapping):
        raise OracleError("the registered treatment artifact has no source provenance")
    if (
        artifact.get("sha256") != binding.get("sha256")
        or artifact.get("target") != binding.get("target")
        or source.get("head") != binding.get("source_commit")
        or source.get("dirty") is not False
    ):
        raise OracleError("the treatment artifact does not match the registration")


def _validate_k5_budget(
    max_budget_usd: str, acknowledgement: str | None
) -> tuple[Decimal, Decimal]:
    try:
        budget = Decimal(max_budget_usd)
        acknowledged = Decimal(acknowledgement or "")
    except InvalidOperation as error:
        raise OracleError("K=5 budget and acknowledgement must be decimal values") from error
    if budget != K5_SESSION_CAP_USD:
        raise OracleError("the registered K=5 run uses exactly $3.00 per session")
    if acknowledged != K5_PAIR_CAP_USD:
        raise OracleError(
            "the K=5 paid acknowledgement must be exactly $30.00 ($3 x 5 sessions x 2 arms)"
        )
    return budget, acknowledged


def _validate_continuity_budget(
    max_budget_usd: str, acknowledgement: str | None
) -> tuple[Decimal, Decimal]:
    try:
        budget = Decimal(max_budget_usd)
        acknowledged = Decimal(acknowledgement or "")
    except InvalidOperation as error:
        raise OracleError(
            "Fieldnote continuity budget and acknowledgement must be decimal values"
        ) from error
    if budget != CONTINUITY_SESSION_CAP_USD:
        raise OracleError("the registered Fieldnote run uses exactly $3.00 per billed step")
    if acknowledged != CONTINUITY_PAIR_CAP_USD:
        raise OracleError(
            "the Fieldnote paid acknowledgement must be exactly $30.00 "
            "(ten billed steps across two arms)"
        )
    return budget, acknowledged


def _run_registered_chain_pair(
    *,
    registration_label: str,
    preregistration: Path,
    session_count: int,
    cohort_attempts: int,
    pair_cap_usd: Decimal,
    manifest_loader: Callable[[Path], tuple[dict[str, Any], list[dict[str, Any]]]],
    budget_validator: Callable[[str, str | None], tuple[Decimal, Decimal]],
    pair_validator: Callable[..., str | None],
    receipt_name: str,
    result_key: str,
    run_id: str,
    admission: bool,
    source: Path,
    chain_manifest: Path = DEFAULT_K5_CHAIN_MANIFEST,
    certification_fixture: Path = DEFAULT_COMPOUNDING_FIXTURE,
    artifact_manifest: Path | None = None,
    model: str = DEFAULT_COMPOUNDING_MODEL,
    max_turns: int = 45,
    max_budget_usd: str = "3.00",
    acknowledge_paid_cap_usd: str | None = None,
    gateway_reserve_config: Path | None = None,
    timeout_seconds: float = 900.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
    attempt_index: int = 1,
    arm_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run one discarded admission or one frozen registered chain pair."""

    _require_gateway_provider(registration_label)
    run_id = _safe_id(run_id, f"{registration_label} pair run id")
    if model != DEFAULT_COMPOUNDING_MODEL:
        raise OracleError(
            f"the registered {registration_label} model is exactly {DEFAULT_COMPOUNDING_MODEL}"
        )
    if max_turns != 45 or timeout_seconds != 900.0:
        raise OracleError(
            f"the registered {registration_label} limits are 45 turns and 900 seconds"
        )
    if not 1 <= attempt_index <= cohort_attempts:
        raise OracleError(
            f"{registration_label} attempt_index must be between 1 and {cohort_attempts}"
        )
    order = list(
        arm_order or (["control", "treatment"] if attempt_index % 2 else ["treatment", "control"])
    )
    if order not in (["control", "treatment"], ["treatment", "control"]):
        raise OracleError(
            f"{registration_label} arm order must contain control and treatment once"
        )
    budget, acknowledged = budget_validator(max_budget_usd, acknowledge_paid_cap_usd)
    if acknowledged != pair_cap_usd:
        raise OracleError(f"the {registration_label} pair cap does not match its registration")
    manifest_payload, sessions = manifest_loader(chain_manifest)
    first_base = str(sessions[0]["base_revision"])
    first_challenge = str(sessions[0]["challenge_id"])

    root = _state_root(state_root)
    run_root = root / "runs" / run_id
    receipt_path = run_root / receipt_name
    if run_root.exists():
        raise OracleError(f"{registration_label} pair run id already exists")
    prepared = _prepare_paid_run(
        root=root,
        artifact_manifest=artifact_manifest,
        source=source,
        revision=first_base,
        fixtures={"session-1": certification_fixture},
        challenge=first_challenge,
        acknowledged_cap=acknowledged,
        gateway_reserve_config=gateway_reserve_config,
        runner=runner,
        reserve_checker=reserve_checker,
    )
    _validate_registered_artifact_binding(
        manifest_payload,
        artifact_manifest=prepared.manifest,
        artifact=prepared.artifact,
    )
    resolved_reserve_config = (
        gateway_reserve_config.resolve()
        if gateway_reserve_config is not None
        else gateway_reserve.default_config_path(root)
    )
    reserve_config = gateway_reserve.load_reserve_config(resolved_reserve_config)
    if not reserve_config.exclusive_key_use_attested:
        raise OracleError(
            f"{registration_label} requires exclusive_key_use_attested=true "
            "for its dedicated eval key"
        )

    certifications: dict[str, dict[str, Any]] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for session in sessions:
        name = f"session-{session['index']}"
        challenge_id = str(session["challenge_id"])
        base = str(session["base_revision"])
        provenance[name] = _validate_fixture_and_challenge(certification_fixture, challenge_id)
        certifications[name] = _certify_packaged_inputs(
            runner,
            source=source,
            revision=base,
            fixture=certification_fixture,
            challenge=challenge_id,
        )

    run_root.mkdir(mode=0o700, parents=True)
    attempt_root = root / "attempts"
    attempt_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = run_root / "work"
    work.mkdir(mode=0o700)
    certification_paths: dict[str, str] = {}
    for name, certification in certifications.items():
        path = run_root / f"{name}-certification.json"
        _atomic_json(path, certification)
        certification_paths[name] = os.fspath(path)

    arm_specs = [
        {
            "condition": "no-stele" if role == "control" else "stele-fresh",
            "label": role,
        }
        for role in order
    ]
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "classification": "discarded-admission" if admission else "registered-cohort-pair",
        "created_at": _utc_now(),
        "completed_at": None,
        "status": "in_progress",
        "configuration": {
            "attempt_index": attempt_index,
            "arm_order": order,
            "sessions_per_chain": session_count,
            "model": model,
            "max_turns_per_session": max_turns,
            "max_budget_usd_per_session": str(budget),
            "acknowledged_paid_cap_usd": str(acknowledged),
        },
        "arms": {},
        "attempt": None,
        "billing": {"gateway_reserve_preflight": prepared.reserve, "gateway_drain_checks": []},
        "provenance": {
            "preregistration": os.fspath(preregistration.resolve()),
            "preregistration_sha256": _sha256_file(preregistration),
            "chain_manifest": os.fspath(chain_manifest.resolve()),
            "chain_manifest_sha256": _sha256_file(chain_manifest),
            "chain_manifest_payload_sha256": hashlib.sha256(
                json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "artifact_manifest": os.fspath(prepared.manifest_path.resolve()),
            "artifact_sha256": prepared.artifact["sha256"],
            "challenges": provenance,
            "certifications": certification_paths,
            "runtime": prepared.runtime(),
        },
    }
    _atomic_json(receipt_path, receipt)

    def invoke_chain(*, spec: Mapping[str, Any], run_id: str, receipt_path: Path) -> dict[str, Any]:
        return _invoke_compounding_chain(
            condition=str(spec["condition"]),
            run_id=run_id,
            source=source.resolve(),
            revision=None,
            chain_manifest=chain_manifest.resolve(),
            session_count=session_count,
            artifact=prepared.artifact,
            backend_url=prepared.backend_url,
            backend_key=prepared.backend_key,
            model=model,
            max_turns=max_turns,
            max_budget_usd=str(budget),
            timeout_seconds=timeout_seconds,
            attempts=attempt_root,
            work=work,
            receipt_path=receipt_path,
            gateway_reserve_config=resolved_reserve_config,
            runner=runner,
        )

    arms, interrupted, primary_error = _run_arm_sequence(
        arm_specs,
        run_id_prefix=run_id,
        run_root=run_root,
        work=work,
        receipt=receipt,
        receipt_path=receipt_path,
        reserve=prepared.reserve,
        drain_checker=drain_checker,
        invoke=invoke_chain,
    )
    for role in ("control", "treatment"):
        if role not in arms:
            arms[role] = _not_run_arm(
                f"{run_id}-{role}", run_root / f"{role}.json", work, "not_started"
            )
    receipts = {
        role: (
            arms[role].get("receipt")
            if arms[role].get("exit_code") == 0 and isinstance(arms[role].get("receipt"), Mapping)
            else None
        )
        for role in ("control", "treatment")
    }
    excluded = pair_validator(
        receipts["control"], receipts["treatment"], admission=admission
    )
    attempt = {
        "attempt": attempt_index,
        "arm_order": order,
        "receipts": receipts,
        "analysable": excluded is None,
        "excluded_reason": excluded,
    }
    successful = interrupted is None and all(arm.get("exit_code") == 0 for arm in arms.values())
    receipt.update(
        {
            "completed_at": _utc_now(),
            "status": "interrupted"
            if interrupted is not None
            else "completed"
            if successful
            else "failed",
            "error": primary_error or (None if successful else "one_or_more_chains_failed"),
            "arms": arms,
            "attempt": attempt,
            "admission_passed": excluded is None if admission else None,
            "pair_valid": excluded is None,
        }
    )
    _atomic_json(receipt_path, receipt)
    if interrupted is not None:
        raise interrupted
    return {
        "ok": successful and excluded is None,
        result_key: os.fspath(receipt_path),
        "receipt": receipt,
    }


def run_k5_pair(
    *,
    run_id: str,
    admission: bool,
    source: Path,
    chain_manifest: Path = DEFAULT_K5_CHAIN_MANIFEST,
    certification_fixture: Path = DEFAULT_COMPOUNDING_FIXTURE,
    artifact_manifest: Path | None = None,
    model: str = DEFAULT_COMPOUNDING_MODEL,
    max_turns: int = 45,
    max_budget_usd: str = "3.00",
    acknowledge_paid_cap_usd: str | None = None,
    gateway_reserve_config: Path | None = None,
    timeout_seconds: float = 900.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
    attempt_index: int = 1,
    arm_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    return _run_registered_chain_pair(
        registration_label="K=5",
        preregistration=DEFAULT_K5_PREREGISTRATION,
        session_count=K5_SESSION_COUNT,
        cohort_attempts=K5_COHORT_ATTEMPTS,
        pair_cap_usd=K5_PAIR_CAP_USD,
        manifest_loader=_load_k5_manifest,
        budget_validator=_validate_k5_budget,
        pair_validator=_k5_pair_valid,
        receipt_name="k5-pair.json",
        result_key="k5_pair_receipt",
        run_id=run_id,
        admission=admission,
        source=source,
        chain_manifest=chain_manifest,
        certification_fixture=certification_fixture,
        artifact_manifest=artifact_manifest,
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        acknowledge_paid_cap_usd=acknowledge_paid_cap_usd,
        gateway_reserve_config=gateway_reserve_config,
        timeout_seconds=timeout_seconds,
        state_root=state_root,
        runner=runner,
        reserve_checker=reserve_checker,
        drain_checker=drain_checker,
        attempt_index=attempt_index,
        arm_order=arm_order,
    )


def run_continuity_pair(
    *,
    run_id: str,
    admission: bool,
    source: Path,
    chain_manifest: Path = DEFAULT_CONTINUITY_CHAIN_MANIFEST,
    certification_fixture: Path = DEFAULT_COMPOUNDING_FIXTURE,
    artifact_manifest: Path | None = None,
    model: str = DEFAULT_COMPOUNDING_MODEL,
    max_turns: int = 45,
    max_budget_usd: str = "3.00",
    acknowledge_paid_cap_usd: str | None = None,
    gateway_reserve_config: Path | None = None,
    timeout_seconds: float = 900.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
    attempt_index: int = 1,
    arm_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    return _run_registered_chain_pair(
        registration_label="Fieldnote continuity",
        preregistration=DEFAULT_CONTINUITY_PREREGISTRATION,
        session_count=CONTINUITY_SESSION_COUNT,
        cohort_attempts=CONTINUITY_COHORT_ATTEMPTS,
        pair_cap_usd=CONTINUITY_PAIR_CAP_USD,
        manifest_loader=_load_continuity_manifest,
        budget_validator=_validate_continuity_budget,
        pair_validator=_continuity_pair_valid,
        receipt_name="continuity-pair.json",
        result_key="continuity_pair_receipt",
        run_id=run_id,
        admission=admission,
        source=source,
        chain_manifest=chain_manifest,
        certification_fixture=certification_fixture,
        artifact_manifest=artifact_manifest,
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        acknowledge_paid_cap_usd=acknowledge_paid_cap_usd,
        gateway_reserve_config=gateway_reserve_config,
        timeout_seconds=timeout_seconds,
        state_root=state_root,
        runner=runner,
        reserve_checker=reserve_checker,
        drain_checker=drain_checker,
        attempt_index=attempt_index,
        arm_order=arm_order,
    )


def _chain_attempt_spend(
    attempt: Mapping[str, Any], *, session_count: int, label: str
) -> Decimal:
    receipts = attempt.get("receipts")
    if not isinstance(receipts, Mapping):
        raise OracleError(f"a {label} cohort attempt lost its paired receipts")
    total = Decimal(0)
    for role in ("control", "treatment"):
        chain = receipts.get(role)
        if not isinstance(chain, Mapping):
            raise OracleError(f"a {label} cohort attempt has an incomplete arm")
        sessions = chain.get("sessions")
        if not isinstance(sessions, list) or len(sessions) != session_count:
            raise OracleError(
                f"a {label} cohort arm lacks {session_count} session receipts"
            )
        for session in sessions:
            billing = session.get("gateway_billing") if isinstance(session, Mapping) else None
            if (
                not isinstance(billing, Mapping)
                or billing.get("exclusive_to_interval") is not True
                or billing.get("source") != "vercel-ai-gateway-quota"
            ):
                raise OracleError(
                    f"a {label} cohort attempt lacks exclusive settled session costs"
                )
            try:
                cost = Decimal(str(billing.get("delta_usd")))
            except InvalidOperation as error:
                raise OracleError(f"a {label} authoritative session cost is invalid") from error
            if not cost.is_finite() or cost < 0:
                raise OracleError(f"a {label} authoritative session cost is invalid")
            total += cost
    return total


def _k5_attempt_spend(attempt: Mapping[str, Any]) -> Decimal:
    return _chain_attempt_spend(attempt, session_count=K5_SESSION_COUNT, label="K=5")


def _continuity_attempt_spend(attempt: Mapping[str, Any]) -> Decimal:
    return _chain_attempt_spend(
        attempt,
        session_count=CONTINUITY_SESSION_COUNT,
        label="Fieldnote continuity",
    )


def _run_registered_chain_batch(
    *,
    registration_label: str,
    preregistration: Path,
    cohort_attempts: int,
    pair_cap_usd: Decimal,
    cohort_cap_usd: Decimal,
    budget_validator: Callable[[str, str | None], tuple[Decimal, Decimal]],
    pair_runner: Callable[..., dict[str, Any]],
    pair_receipt_key: str,
    attempt_spend: Callable[[Mapping[str, Any]], Decimal],
    summarizer: Callable[[Sequence[Mapping[str, Any]]], dict[str, Any]],
    run_suffix: str,
    ledger_name: str,
    lock_name: str,
    cohort_id: str,
    attempt_index: int,
    source: Path,
    acknowledge_paid_cap_usd: str | None,
    acknowledge_cohort_cap_usd: str | None,
    chain_manifest: Path = DEFAULT_K5_CHAIN_MANIFEST,
    certification_fixture: Path = DEFAULT_COMPOUNDING_FIXTURE,
    artifact_manifest: Path | None = None,
    gateway_reserve_config: Path | None = None,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
) -> dict[str, Any]:
    """Append exactly the next immutable pair to a registered chain cohort."""

    _require_gateway_provider(registration_label)
    cohort_id = _safe_id(cohort_id, f"{registration_label} cohort id")
    if not 1 <= attempt_index <= cohort_attempts:
        raise OracleError(
            f"{registration_label} cohort attempt index must be between 1 and "
            f"{cohort_attempts}"
        )
    budget_validator("3.00", acknowledge_paid_cap_usd)
    try:
        cohort_ack = Decimal(acknowledge_cohort_cap_usd or "")
    except InvalidOperation as error:
        raise OracleError(
            f"{registration_label} cohort acknowledgement must be a decimal"
        ) from error
    if cohort_ack != cohort_cap_usd:
        raise OracleError(
            f"{registration_label} cohort acknowledgement must be exactly "
            f"${cohort_cap_usd}"
        )

    root = _state_root(state_root)
    cohort_root = root / "runs" / cohort_id
    cohort_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = cohort_root / lock_name
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OracleError(
                f"another {registration_label} cohort batch is already running"
            ) from error

        ledger_path = cohort_root / ledger_name
        prereg_sha = _sha256_file(preregistration)
        manifest_sha = _sha256_file(chain_manifest)
        planned = [
            {
                "attempt": index,
                "arm_order": ["control", "treatment"] if index % 2 else ["treatment", "control"],
                "batch_run_id": f"{cohort_id}-{run_suffix}-{index:02d}",
            }
            for index in range(1, cohort_attempts + 1)
        ]
        plan_sha = hashlib.sha256(
            json.dumps(planned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if ledger_path.exists():
            ledger = _read_json(ledger_path)
            if (
                ledger.get("preregistration_sha256") != prereg_sha
                or ledger.get("chain_manifest_sha256") != manifest_sha
                or ledger.get("planned_attempts_sha256") != plan_sha
                or ledger.get("acknowledged_cohort_cap_usd") != str(cohort_ack)
            ):
                raise OracleError(
                    f"{registration_label} cohort resume settings differ from the frozen ledger"
                )
        else:
            if attempt_index != 1:
                raise OracleError(f"a {registration_label} cohort must start with attempt 1")
            ledger = {
                "schema_version": 1,
                "cohort_id": cohort_id,
                "status": "in_progress",
                "created_at": _utc_now(),
                "completed_at": None,
                "preregistration": os.fspath(preregistration.resolve()),
                "preregistration_sha256": prereg_sha,
                "chain_manifest": os.fspath(chain_manifest.resolve()),
                "chain_manifest_sha256": manifest_sha,
                "acknowledged_cohort_cap_usd": str(cohort_ack),
                "planned_attempts": planned,
                "planned_attempts_sha256": plan_sha,
                "batches": [],
                "authoritative_spend_usd": "0",
                "summary": None,
            }
            _atomic_json(ledger_path, ledger)

        batches = ledger.get("batches")
        if not isinstance(batches, list):
            raise OracleError(f"{registration_label} cohort ledger lost its batch list")
        for batch in batches:
            if not isinstance(batch, Mapping):
                raise OracleError(
                    f"{registration_label} cohort ledger contains a malformed batch"
                )
            path = Path(str(batch.get("receipt_path")))
            if not path.is_file() or _sha256_file(path) != batch.get("receipt_sha256"):
                raise OracleError(
                    f"a {registration_label} batch receipt changed after entering the cohort"
                )
        if attempt_index != len(batches) + 1:
            raise OracleError(
                f"{registration_label} cohort batches must run once in frozen order"
            )
        try:
            actual_spend = sum(
                (Decimal(str(batch["authoritative_spend_usd"])) for batch in batches),
                Decimal(0),
            )
        except (InvalidOperation, KeyError) as error:
            raise OracleError(
                f"a prior {registration_label} batch has no settled authoritative spend"
            ) from error
        if actual_spend + pair_cap_usd > cohort_cap_usd:
            raise OracleError(
                f"{registration_label} cohort spend plus the next pair cap exceeds "
                f"${cohort_cap_usd}"
            )

        plan = planned[attempt_index - 1]
        result = pair_runner(
            run_id=plan["batch_run_id"],
            admission=False,
            source=source,
            chain_manifest=chain_manifest,
            certification_fixture=certification_fixture,
            artifact_manifest=artifact_manifest,
            max_budget_usd="3.00",
            acknowledge_paid_cap_usd=acknowledge_paid_cap_usd,
            gateway_reserve_config=gateway_reserve_config,
            state_root=root,
            runner=runner,
            reserve_checker=reserve_checker,
            drain_checker=drain_checker,
            attempt_index=attempt_index,
            arm_order=plan["arm_order"],
        )
        pair_receipt = result.get("receipt")
        pair_path_value = result.get(pair_receipt_key)
        if not isinstance(pair_receipt, Mapping) or not isinstance(pair_path_value, str):
            raise OracleError(
                f"{registration_label} pair runner returned no immutable receipt"
            )
        pair_path = Path(pair_path_value).resolve(strict=True)
        attempt_record = pair_receipt.get("attempt")
        if not isinstance(attempt_record, Mapping):
            raise OracleError(f"{registration_label} pair receipt lost its attempt record")
        batch_record = {
            "attempt": attempt_index,
            "arm_order": plan["arm_order"],
            "batch_run_id": plan["batch_run_id"],
            "receipt_path": os.fspath(pair_path),
            "receipt_sha256": _sha256_file(pair_path),
            "attempt_record": dict(attempt_record),
            "authoritative_spend_usd": None,
        }
        batches.append(batch_record)
        try:
            pair_spend = attempt_spend(attempt_record)
        except OracleError as error:
            ledger["status"] = "blocked_missing_authoritative_cost"
            ledger["error"] = str(error)
            _atomic_json(ledger_path, ledger)
            return {
                "ok": False,
                "cohort_ledger": os.fspath(ledger_path),
                "receipt": ledger,
            }
        batch_record["authoritative_spend_usd"] = format(pair_spend, "f")
        actual_spend += pair_spend
        ledger["authoritative_spend_usd"] = format(actual_spend, "f")
        if len(batches) == cohort_attempts:
            ledger["summary"] = summarizer([batch["attempt_record"] for batch in batches])
            ledger["status"] = "completed"
            ledger["completed_at"] = _utc_now()
        _atomic_json(ledger_path, ledger)
        return {
            "ok": result.get("ok") is True,
            "cohort_ledger": os.fspath(ledger_path),
            "receipt": ledger,
        }


def run_k5_batch(
    *,
    cohort_id: str,
    attempt_index: int,
    source: Path,
    acknowledge_paid_cap_usd: str | None,
    acknowledge_cohort_cap_usd: str | None,
    chain_manifest: Path = DEFAULT_K5_CHAIN_MANIFEST,
    certification_fixture: Path = DEFAULT_COMPOUNDING_FIXTURE,
    artifact_manifest: Path | None = None,
    gateway_reserve_config: Path | None = None,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
    pair_runner: Callable[..., dict[str, Any]] = run_k5_pair,
) -> dict[str, Any]:
    return _run_registered_chain_batch(
        registration_label="K=5",
        preregistration=DEFAULT_K5_PREREGISTRATION,
        cohort_attempts=K5_COHORT_ATTEMPTS,
        pair_cap_usd=K5_PAIR_CAP_USD,
        cohort_cap_usd=K5_COHORT_CAP_USD,
        budget_validator=_validate_k5_budget,
        pair_runner=pair_runner,
        pair_receipt_key="k5_pair_receipt",
        attempt_spend=_k5_attempt_spend,
        summarizer=_k5_summary,
        run_suffix="k5",
        ledger_name="k5-cohort.json",
        lock_name="k5-cohort.lock",
        cohort_id=cohort_id,
        attempt_index=attempt_index,
        source=source,
        acknowledge_paid_cap_usd=acknowledge_paid_cap_usd,
        acknowledge_cohort_cap_usd=acknowledge_cohort_cap_usd,
        chain_manifest=chain_manifest,
        certification_fixture=certification_fixture,
        artifact_manifest=artifact_manifest,
        gateway_reserve_config=gateway_reserve_config,
        state_root=state_root,
        runner=runner,
        reserve_checker=reserve_checker,
        drain_checker=drain_checker,
    )


def run_continuity_batch(
    *,
    cohort_id: str,
    attempt_index: int,
    source: Path,
    acknowledge_paid_cap_usd: str | None,
    acknowledge_cohort_cap_usd: str | None,
    chain_manifest: Path = DEFAULT_CONTINUITY_CHAIN_MANIFEST,
    certification_fixture: Path = DEFAULT_COMPOUNDING_FIXTURE,
    artifact_manifest: Path | None = None,
    gateway_reserve_config: Path | None = None,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
    pair_runner: Callable[..., dict[str, Any]] = run_continuity_pair,
) -> dict[str, Any]:
    return _run_registered_chain_batch(
        registration_label="Fieldnote continuity",
        preregistration=DEFAULT_CONTINUITY_PREREGISTRATION,
        cohort_attempts=CONTINUITY_COHORT_ATTEMPTS,
        pair_cap_usd=CONTINUITY_PAIR_CAP_USD,
        cohort_cap_usd=CONTINUITY_COHORT_CAP_USD,
        budget_validator=_validate_continuity_budget,
        pair_runner=pair_runner,
        pair_receipt_key="continuity_pair_receipt",
        attempt_spend=_continuity_attempt_spend,
        summarizer=_continuity_summary,
        run_suffix="continuity",
        ledger_name="continuity-cohort.json",
        lock_name="continuity-cohort.lock",
        cohort_id=cohort_id,
        attempt_index=attempt_index,
        source=source,
        acknowledge_paid_cap_usd=acknowledge_paid_cap_usd,
        acknowledge_cohort_cap_usd=acknowledge_cohort_cap_usd,
        chain_manifest=chain_manifest,
        certification_fixture=certification_fixture,
        artifact_manifest=artifact_manifest,
        gateway_reserve_config=gateway_reserve_config,
        state_root=state_root,
        runner=runner,
        reserve_checker=reserve_checker,
        drain_checker=drain_checker,
    )


def run_compounding(
    *,
    run_id: str,
    admission: bool,
    attempts: int,
    source: Path = DEFAULT_SOURCE,
    revision: str = DEFAULT_REVISION,
    certification_fixture: Path = DEFAULT_COMPOUNDING_FIXTURE,
    artifact_manifest: Path | None = None,
    model: str = DEFAULT_COMPOUNDING_MODEL,
    max_turns: int = 45,
    max_budget_usd: str = "10.00",
    acknowledge_paid_cap_usd: str | None = None,
    gateway_reserve_config: Path | None = None,
    timeout_seconds: float = 900.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
    cohort_attempt_index: int | None = None,
    cohort_arm_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run the locked live two-session experiment, either admission or N=10."""

    _require_gateway_provider("compounding")
    run_id = _safe_id(run_id, "compounding run id")
    if admission and cohort_attempt_index is not None:
        raise OracleError("an admission run cannot be a cohort batch")
    if cohort_attempt_index is None and cohort_arm_order is not None:
        raise OracleError("a frozen arm order is valid only for a cohort batch")
    if admission and attempts != 1:
        raise OracleError("a compounding admission run must contain exactly one pair")
    if not admission and cohort_attempt_index is None and attempts != 10:
        raise OracleError("the registered compounding cohort must contain exactly ten pairs")
    if not admission and cohort_attempt_index is not None:
        if attempts != 1:
            raise OracleError("a compounding cohort batch must contain exactly one pair")
        if not 1 <= cohort_attempt_index <= COMPOUNDING_COHORT_ATTEMPTS:
            raise OracleError("a compounding cohort attempt index must be between 1 and 10")
        if list(cohort_arm_order or ()) not in (
            ["control", "treatment"],
            ["treatment", "control"],
        ):
            raise OracleError("a compounding cohort batch needs its frozen arm order")
    if model != DEFAULT_COMPOUNDING_MODEL:
        raise OracleError(
            f"the registered compounding run uses exactly {DEFAULT_COMPOUNDING_MODEL}"
        )
    if max_turns != 45:
        raise OracleError("the registered compounding run uses exactly 45 turns per session")
    if timeout_seconds != 900.0:
        raise OracleError("the registered compounding run uses exactly 900 seconds per session")
    budget, acknowledged_cap = _validate_compounding_budget(
        max_budget_usd, acknowledge_paid_cap_usd, attempts
    )
    root = _state_root(state_root)
    run_root = root / "runs" / run_id
    receipt_path = run_root / "compounding.json"
    if run_root.exists():
        raise OracleError("compounding run id already exists; inspect or clean it before retrying")

    prepared = _prepare_paid_run(
        root=root,
        artifact_manifest=artifact_manifest,
        source=source,
        revision=revision,
        fixtures={"session-1": certification_fixture},
        challenge=COMPOUNDING_CHALLENGES[0],
        acknowledged_cap=acknowledged_cap,
        gateway_reserve_config=gateway_reserve_config,
        runner=runner,
        reserve_checker=reserve_checker,
    )
    second_provenance = _validate_fixture_and_challenge(
        certification_fixture, COMPOUNDING_CHALLENGES[1]
    )
    second_certification = _certify_packaged_inputs(
        runner,
        source=source,
        revision=revision,
        fixture=certification_fixture,
        challenge=COMPOUNDING_CHALLENGES[1],
    )
    resolved_reserve_config = (
        gateway_reserve_config.resolve()
        if gateway_reserve_config is not None
        else gateway_reserve.default_config_path(root)
    )

    run_root.mkdir(mode=0o700, parents=True)
    attempt_root = root / "attempts"
    attempt_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = run_root / "work"
    work.mkdir(mode=0o700)
    certifications = {
        "session_1": prepared.certifications["session-1"],
        "session_2": second_certification,
    }
    certification_paths: dict[str, str] = {}
    for name, certification in certifications.items():
        path = run_root / f"{name}-certification.json"
        _atomic_json(path, certification)
        certification_paths[name] = os.fspath(path)

    attempt_indices = (
        [cohort_attempt_index] if cohort_attempt_index is not None else list(range(1, attempts + 1))
    )
    arm_specs: list[dict[str, Any]] = []
    orders: dict[int, list[str]] = {}
    for index in attempt_indices:
        order = (
            list(cohort_arm_order)
            if cohort_attempt_index is not None and cohort_arm_order is not None
            else ["control", "treatment"]
            if index % 2
            else ["treatment", "control"]
        )
        orders[index] = order
        for role in order:
            arm_specs.append(
                {
                    "condition": "no-stele" if role == "control" else "stele-fresh",
                    "label": f"attempt-{index:02d}-{role}",
                }
            )

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "classification": (
            "discarded-admission"
            if admission
            else "registered-cohort-batch"
            if cohort_attempt_index is not None
            else "registered-cohort"
        ),
        "created_at": _utc_now(),
        "completed_at": None,
        "status": "in_progress",
        "error": None,
        "configuration": {
            "attempts": attempts,
            "cohort_total_attempts": COMPOUNDING_COHORT_ATTEMPTS if not admission else None,
            "cohort_attempt_index": cohort_attempt_index,
            "sessions_per_chain": 2,
            "conditions": {"control": "no-stele", "treatment": "stele-fresh"},
            "arm_order": {str(index): value for index, value in orders.items()},
            "source_commit": prepared.source_commit,
            "challenges": list(COMPOUNDING_CHALLENGES),
            "model": model,
            "max_turns_per_session": max_turns,
            "max_budget_usd_per_session": str(budget),
            "acknowledged_paid_cap_usd": str(acknowledged_cap),
        },
        "arms": {},
        "attempts": [],
        "summary": None,
        "billing": {
            "gateway_reserve_preflight": prepared.reserve,
            "gateway_drain_checks": [],
            "gateway_drain_stop": None,
        },
        "provenance": {
            "preregistration": os.fspath(DEFAULT_COMPOUNDING_PREREGISTRATION.resolve()),
            "preregistration_sha256": _sha256_file(DEFAULT_COMPOUNDING_PREREGISTRATION),
            "artifact_manifest": os.fspath(prepared.manifest_path.resolve()),
            "artifact_sha256": prepared.artifact["sha256"],
            "artifact_target": prepared.artifact["target"],
            "certification_fixture_only": os.fspath(certification_fixture.resolve()),
            "challenges": {
                COMPOUNDING_CHALLENGES[0]: prepared.provenance["session-1"],
                COMPOUNDING_CHALLENGES[1]: second_provenance,
            },
            "certifications": certification_paths,
            "runtime": prepared.runtime(),
        },
    }
    _atomic_json(receipt_path, receipt)

    def invoke_chain(*, spec: Mapping[str, Any], run_id: str, receipt_path: Path) -> dict[str, Any]:
        return _invoke_compounding_chain(
            condition=str(spec["condition"]),
            run_id=run_id,
            source=source.resolve(),
            revision=revision,
            artifact=prepared.artifact,
            backend_url=prepared.backend_url,
            backend_key=prepared.backend_key,
            model=model,
            max_turns=max_turns,
            max_budget_usd=str(budget),
            timeout_seconds=timeout_seconds,
            attempts=attempt_root,
            work=work,
            receipt_path=receipt_path,
            gateway_reserve_config=resolved_reserve_config,
            runner=runner,
        )

    arms, interrupted, primary_error = _run_arm_sequence(
        arm_specs,
        run_id_prefix=run_id,
        run_root=run_root,
        work=work,
        receipt=receipt,
        receipt_path=receipt_path,
        reserve=prepared.reserve,
        drain_checker=drain_checker,
        invoke=invoke_chain,
    )
    for spec in arm_specs:
        label = str(spec["label"])
        if label not in arms:
            arms[label] = _not_run_arm(
                f"{run_id}-{label}", run_root / f"{label}.json", work, "not_started"
            )

    attempt_records: list[dict[str, Any]] = []
    for index in attempt_indices:
        receipts: dict[str, Mapping[str, Any] | None] = {}
        for role in ("control", "treatment"):
            arm = arms[f"attempt-{index:02d}-{role}"]
            value = arm.get("receipt")
            receipts[role] = (
                value if arm.get("exit_code") == 0 and isinstance(value, Mapping) else None
            )
        excluded = _compounding_pair_valid(
            receipts["control"], receipts["treatment"], admission=admission
        )
        attempt_records.append(
            {
                "attempt": index,
                "arm_order": orders[index],
                "receipts": receipts,
                "analysable": excluded is None,
                "excluded_reason": excluded,
            }
        )

    summary = _compounding_summary(attempt_records)
    successful = interrupted is None and all(arm.get("exit_code") == 0 for arm in arms.values())
    admission_passed = admission and summary["attempts_analysed"] == 1
    cohort_valid = (
        not admission and cohort_attempt_index is None and summary["attempts_analysed"] >= 8
    )
    receipt.update(
        {
            "completed_at": _utc_now(),
            "status": (
                "interrupted"
                if interrupted is not None
                else "completed"
                if successful
                else "failed"
            ),
            "error": primary_error or (None if successful else "one_or_more_chains_failed"),
            "arms": arms,
            "attempts": attempt_records,
            "summary": summary,
            "admission_passed": admission_passed if admission else None,
            "cohort_valid": (
                cohort_valid if not admission and cohort_attempt_index is None else None
            ),
        }
    )
    _atomic_json(receipt_path, receipt)
    if interrupted is not None:
        raise interrupted
    return {
        "ok": successful
        and (
            admission_passed
            if admission
            else cohort_valid
            if cohort_attempt_index is None
            else True
        ),
        "compounding_receipt": os.fspath(receipt_path),
        "receipt": receipt,
    }


def _compounding_batch_order(index: int) -> list[str]:
    return ["control", "treatment"] if index % 2 else ["treatment", "control"]


def _compounding_plan_sha256(planned_attempts: Sequence[Mapping[str, Any]]) -> str:
    frozen = [
        {
            "attempt": item.get("attempt"),
            "arm_order": item.get("arm_order"),
            "batch_run_id": item.get("batch_run_id"),
        }
        for item in planned_attempts
    ]
    payload = json.dumps(
        frozen,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _compounding_batch_entry(receipt_path: Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    configuration = receipt.get("configuration")
    attempts = receipt.get("attempts")
    if not isinstance(configuration, Mapping) or not isinstance(attempts, list):
        raise OracleError("compounding batch receipt is missing configuration or attempts")
    index = configuration.get("cohort_attempt_index")
    if not isinstance(index, int) or len(attempts) != 1:
        raise OracleError("compounding batch receipt does not contain exactly one indexed pair")
    attempt = attempts[0]
    arms = receipt.get("arms")
    if not isinstance(attempt, Mapping) or not isinstance(arms, Mapping):
        raise OracleError("compounding batch receipt is missing paired evidence")
    gateway_cost = Decimal("0")
    costs_settled = True
    for role in ("control", "treatment"):
        arm = arms.get(f"attempt-{index:02d}-{role}")
        chain = arm.get("receipt") if isinstance(arm, Mapping) else None
        cumulative = chain.get("cumulative") if isinstance(chain, Mapping) else None
        value = cumulative.get("gateway_observed_usd") if isinstance(cumulative, Mapping) else None
        try:
            gateway_cost += Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            costs_settled = False
    return {
        "attempt": index,
        "batch_run_id": receipt.get("run_id"),
        "receipt_path": os.fspath(receipt_path.resolve()),
        "receipt_sha256": _sha256_file(receipt_path),
        "status": receipt.get("status"),
        "analysable": attempt.get("analysable") is True,
        "excluded_reason": attempt.get("excluded_reason"),
        "gateway_observed_usd": str(gateway_cost) if costs_settled else None,
    }


def _compounding_authoritative_spend(ledger: Mapping[str, Any]) -> Decimal:
    total = Decimal("0")
    for batch in ledger.get("batches", []):
        value = batch.get("gateway_observed_usd")
        if value is None:
            raise OracleError("a prior compounding batch has no settled authoritative Gateway cost")
        try:
            total += Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise OracleError(
                "a prior compounding batch has an invalid authoritative Gateway cost"
            ) from error
    return total


def _finalize_compounding_cohort(ledger: dict[str, Any]) -> None:
    attempts: list[Mapping[str, Any]] = []
    for batch in ledger["batches"]:
        receipt_path = Path(batch["receipt_path"])
        if _sha256_file(receipt_path) != batch["receipt_sha256"]:
            raise OracleError("a compounding batch receipt changed after it entered the cohort")
        receipt = _read_json(receipt_path)
        records = receipt.get("attempts")
        if not isinstance(records, list) or len(records) != 1:
            raise OracleError("a compounding batch receipt lost its paired attempt")
        attempts.append(records[0])
    if [record.get("attempt") for record in attempts] != list(
        range(1, COMPOUNDING_COHORT_ATTEMPTS + 1)
    ):
        raise OracleError("compounding cohort receipts are not the frozen attempts 1 through 10")
    summary = _compounding_summary(attempts)
    ledger.update(
        {
            "status": "completed",
            "completed_at": _utc_now(),
            "summary": summary,
            "cohort_valid": summary["attempts_analysed"] >= 8,
            "next_attempt_index": None,
        }
    )


def _with_compounding_cohort_lock(function: Callable[..., dict[str, Any]]):
    def locked(*, cohort_id: str, state_root: Path | None = None, **kwargs: Any) -> dict[str, Any]:
        safe_cohort_id = _safe_id(cohort_id, "compounding cohort id")
        root = _state_root(state_root)
        cohort_root = root / "runs" / safe_cohort_id
        cohort_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = cohort_root / ".batch.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OracleError("another compounding cohort batch is already running") from error
            return function(
                cohort_id=safe_cohort_id,
                state_root=root,
                **kwargs,
            )

    return locked


@_with_compounding_cohort_lock
def run_compounding_batch(
    *,
    cohort_id: str,
    attempt_index: int,
    source: Path = DEFAULT_SOURCE,
    revision: str = DEFAULT_REVISION,
    certification_fixture: Path = DEFAULT_COMPOUNDING_FIXTURE,
    artifact_manifest: Path | None = None,
    model: str = DEFAULT_COMPOUNDING_MODEL,
    max_turns: int = 45,
    max_budget_usd: str = "10.00",
    acknowledge_paid_cap_usd: str | None = None,
    gateway_reserve_config: Path | None = None,
    timeout_seconds: float = 900.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
) -> dict[str, Any]:
    """Append exactly the next frozen pair to a ten-pair compounding cohort."""

    _require_gateway_provider("compounding")
    cohort_id = _safe_id(cohort_id, "compounding cohort id")
    if DEFAULT_COMPOUNDING_PREREGISTRATION.name in RETIRED_COMPOUNDING_PREREGISTRATIONS:
        raise OracleError("profile-selection compounding v4 was retired after admission 003 audit")
    if not 1 <= attempt_index <= COMPOUNDING_COHORT_ATTEMPTS:
        raise OracleError("compounding batch attempt index must be between 1 and 10")
    if model != DEFAULT_COMPOUNDING_MODEL:
        raise OracleError(
            f"the registered compounding run uses exactly {DEFAULT_COMPOUNDING_MODEL}"
        )
    if max_turns != 45:
        raise OracleError("the registered compounding run uses exactly 45 turns per session")
    if timeout_seconds != 900.0:
        raise OracleError("the registered compounding run uses exactly 900 seconds per session")
    budget, acknowledged_cap = _validate_compounding_budget(
        max_budget_usd, acknowledge_paid_cap_usd, 1
    )
    root = _state_root(state_root)
    cohort_root = root / "runs" / cohort_id
    ledger_path = cohort_root / "compounding-cohort.json"
    batch_run_id = f"{cohort_id}-attempt-{attempt_index:02d}"
    batch_receipt_path = root / "runs" / batch_run_id / "compounding.json"
    resolved_artifact_manifest = (
        artifact_manifest.resolve()
        if artifact_manifest is not None
        else (root / "artifacts" / "current.json").resolve()
    )
    preregistration_sha = _sha256_file(DEFAULT_COMPOUNDING_PREREGISTRATION)
    frozen_configuration = {
        "attempts": COMPOUNDING_COHORT_ATTEMPTS,
        "batch_size": 1,
        "sessions_per_chain": 2,
        "model": model,
        "max_turns_per_session": max_turns,
        "max_budget_usd_per_session": str(budget),
        "paid_cap_usd_per_batch": str(acknowledged_cap),
        "paid_cap_usd_per_cohort": str(COMPOUNDING_COHORT_CAP_USD),
        "source_revision": revision,
        "certification_fixture": os.fspath(certification_fixture.resolve()),
        "certification_fixture_sha256": _sha256_file(certification_fixture),
        "artifact_manifest": os.fspath(resolved_artifact_manifest),
        "artifact_manifest_sha256": _sha256_file(resolved_artifact_manifest),
    }
    if ledger_path.exists():
        ledger = _read_json(ledger_path)
        if ledger.get("configuration") != frozen_configuration:
            raise OracleError("compounding cohort resume settings differ from the frozen ledger")
        if ledger.get("preregistration_sha256") != preregistration_sha:
            raise OracleError("compounding cohort preregistration changed after the first batch")
    else:
        if attempt_index != 1:
            raise OracleError("a compounding cohort must start with attempt 1")
        cohort_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        planned_attempts = [
            {
                "attempt": index,
                "arm_order": _compounding_batch_order(index),
                "batch_run_id": f"{cohort_id}-attempt-{index:02d}",
                "execution_state": "pending",
                "started_at": None,
                "consumed_at": None,
            }
            for index in range(1, COMPOUNDING_COHORT_ATTEMPTS + 1)
        ]
        ledger = {
            "schema_version": 1,
            "cohort_id": cohort_id,
            "classification": "registered-cohort-batched",
            "created_at": _utc_now(),
            "completed_at": None,
            "status": "in_progress",
            "configuration": frozen_configuration,
            "preregistration": os.fspath(DEFAULT_COMPOUNDING_PREREGISTRATION.resolve()),
            "preregistration_sha256": preregistration_sha,
            "planned_attempts": planned_attempts,
            "planned_attempts_sha256": _compounding_plan_sha256(planned_attempts),
            "batches": [],
            "cumulative_gateway_observed_usd": "0",
            "next_attempt_index": 1,
            "summary": None,
            "cohort_valid": None,
        }
        _atomic_json(ledger_path, ledger)

    completed = [batch.get("attempt") for batch in ledger.get("batches", [])]
    planned_attempts = ledger.get("planned_attempts")
    if not isinstance(planned_attempts, list) or len(planned_attempts) != 10:
        raise OracleError("compounding cohort ledger lost its frozen pair assignments")
    if ledger.get("planned_attempts_sha256") != _compounding_plan_sha256(planned_attempts):
        raise OracleError("compounding cohort frozen pair assignments changed")
    planned = next(
        (item for item in planned_attempts if item.get("attempt") == attempt_index),
        None,
    )
    if not isinstance(planned, Mapping):
        raise OracleError("compounding cohort ledger lost the requested pair assignment")
    frozen_arm_order = planned.get("arm_order")
    if frozen_arm_order not in (
        ["control", "treatment"],
        ["treatment", "control"],
    ):
        raise OracleError("compounding cohort ledger has an invalid frozen arm order")
    execution_state = planned.get("execution_state")
    if execution_state == "consumed":
        raise OracleError("compounding cohort attempt already has an immutable receipt")
    if execution_state not in ("pending", "started_no_receipt"):
        raise OracleError("compounding cohort ledger has an invalid pair execution state")
    for batch in ledger.get("batches", []):
        prior_path = Path(batch["receipt_path"])
        if not prior_path.is_file() or _sha256_file(prior_path) != batch["receipt_sha256"]:
            raise OracleError("a compounding batch receipt changed after it entered the cohort")
    if attempt_index in completed:
        raise OracleError("compounding cohort attempt already has an immutable receipt")
    if ledger.get("next_attempt_index") != attempt_index:
        raise OracleError("compounding cohort batches must run in frozen attempt order")
    authoritative_spend = _compounding_authoritative_spend(ledger)
    if authoritative_spend + acknowledged_cap > COMPOUNDING_COHORT_CAP_USD:
        raise OracleError(
            "compounding cohort authoritative Gateway spend plus the next batch cap "
            "would exceed the registered $400.00 cohort ceiling"
        )
    if ledger.get("cumulative_gateway_observed_usd") != str(authoritative_spend):
        raise OracleError("compounding cohort cumulative Gateway spend is inconsistent")

    def append_existing_batch() -> dict[str, Any]:
        batch_receipt = _read_json(batch_receipt_path)
        entry = _compounding_batch_entry(batch_receipt_path, batch_receipt)
        if entry["attempt"] != attempt_index or entry["batch_run_id"] != batch_run_id:
            raise OracleError("compounding batch receipt identity does not match the cohort")
        ledger["batches"].append(entry)
        planned["execution_state"] = "consumed"
        planned["consumed_at"] = _utc_now()
        authoritative_total = _compounding_authoritative_spend(ledger)
        ledger["cumulative_gateway_observed_usd"] = str(authoritative_total)
        ledger["next_attempt_index"] = attempt_index + 1
        if attempt_index == COMPOUNDING_COHORT_ATTEMPTS:
            _finalize_compounding_cohort(ledger)
        _atomic_json(ledger_path, ledger)
        return entry

    if batch_receipt_path.exists():
        entry = append_existing_batch()
        return {
            "ok": entry["status"] == "completed",
            "recovered": True,
            "cohort_ledger": os.fspath(ledger_path),
            "batch": entry,
            "progress": f"{len(ledger['batches'])}/{COMPOUNDING_COHORT_ATTEMPTS}",
            "cohort_complete": ledger["status"] == "completed",
        }

    planned["execution_state"] = "started_no_receipt"
    if planned.get("started_at") is None:
        planned["started_at"] = _utc_now()
    _atomic_json(ledger_path, ledger)

    try:
        batch_result = run_compounding(
            run_id=batch_run_id,
            admission=False,
            attempts=1,
            source=source,
            revision=revision,
            certification_fixture=certification_fixture,
            artifact_manifest=resolved_artifact_manifest,
            model=model,
            max_turns=max_turns,
            max_budget_usd=str(budget),
            acknowledge_paid_cap_usd=str(acknowledged_cap),
            gateway_reserve_config=gateway_reserve_config,
            timeout_seconds=timeout_seconds,
            state_root=root,
            runner=runner,
            reserve_checker=reserve_checker,
            drain_checker=drain_checker,
            cohort_attempt_index=attempt_index,
            cohort_arm_order=frozen_arm_order,
        )
    except BaseException:
        if batch_receipt_path.exists():
            append_existing_batch()
        raise
    entry = append_existing_batch()
    return {
        "ok": batch_result["ok"],
        "recovered": False,
        "cohort_ledger": os.fspath(ledger_path),
        "batch": entry,
        "progress": f"{len(ledger['batches'])}/{COMPOUNDING_COHORT_ATTEMPTS}",
        "cohort_complete": ledger["status"] == "completed",
    }


_BORROWED_CONDITIONS = ("no-stele", "stele-fresh")
_BORROWED_GRAPH_CONDITIONS = ("stele-fresh",)
# What "no worse" has to mean before the run starts. A margin chosen after
# seeing the number is not a margin.
DEFAULT_NON_INFERIORITY_MARGIN = 0.10


def _verify_borrowed_dataset(dataset: Path) -> dict[str, Any]:
    """Re-prove a borrowed dataset carries no answers, and pin what it contains.

    Sanitisation happened once, possibly long ago, to a directory on a laptop.
    This runs the same audits again against the tree as it stands now, because
    the tree is what the arms will actually be built from — and a run that
    reaches the reference solution is worse than no run at all here, since the
    arm that explores more would benefit more.
    """

    try:
        return dict(prepare_external_dataset.verify_dataset(dataset))
    except (prepare_external_dataset.DatasetPreparationError, OSError, ValueError) as error:
        raise OracleError(f"borrowed dataset is not safe to run: {error}") from error


def _suite_summary(attempts: Sequence[Mapping[str, Any]], margin: float) -> dict[str, Any]:
    """Aggregate paired tasks, and answer the question a suite actually asks.

    The per-measure part is deliberately identical to a repeat's, because the
    arithmetic of pairing does not change when the pairs are different tasks
    instead of repetitions of one. What DOES change is the claim: a repeat asks
    whether memory helped, a suite asks whether installing it hurt. So this adds
    an interval on the paired difference and a verdict against a margin that was
    fixed in advance, and reports `reward` as the headline because a borrowed
    benchmark's own verifier is the only thing here we did not write.
    """

    summary = _repeat_summary(attempts)
    analysable = [record for record in attempts if record.get("analysable")]
    control: list[bool] = []
    treatment: list[bool] = []
    for record in analysable:
        control_value = _measure_succeeded(record["metrics"]["control"].get("reward"))
        treatment_value = _measure_succeeded(record["metrics"]["treatment"].get("reward"))
        if control_value is None or treatment_value is None:
            continue
        control.append(control_value)
        treatment.append(treatment_value)
    difference = statistics.paired_binary_difference(treatment=treatment, control=control)
    summary["sample_unit"] = "task"
    summary["solve_rate"] = difference
    summary["non_inferiority"] = statistics.non_inferiority_verdict(difference, margin)
    summary["external_source_access"] = _external_access_rates(analysable)
    summary["memory_uptake"] = results._memory_uptake(attempts)
    return summary


def _external_access_rates(attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-arm count of trials that reached the network, for the operator to judge.

    These tasks allow internet and have published solutions, so this is the same
    differential-cheat shape as the bundled answer payload. It is not auto-
    excluded, because a network call is just as likely to be a package manager
    and dropping those would bias against whichever arm installs more
    dependencies. Reported per arm so a pre-declared decision rule can fire on a
    gap between them, rather than a filter silently deciding for us.
    """

    counts = {"control": 0, "treatment": 0}
    for record in attempts:
        reached = record.get("external_source_access")
        if not isinstance(reached, Mapping):
            continue
        for role in counts:
            if reached.get(role) is True:
                counts[role] += 1
    return {"trials": len(attempts), **counts}


def _arm_reached_the_network(receipt: Mapping[str, Any] | None) -> bool | None:
    if not isinstance(receipt, Mapping):
        return None
    results = receipt.get("results")
    if not isinstance(results, list) or len(results) != 1:
        return None
    evidence = results[0].get("evidence") if isinstance(results[0], Mapping) else None
    if not isinstance(evidence, Mapping):
        return None
    value = evidence.get("external_source_access_detected")
    return value if isinstance(value, bool) else None


def _suite_graph_uptake(arm: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What the treatment arm actually left in its project, per task."""

    if not isinstance(arm, Mapping):
        return None
    receipt = arm.get("receipt")
    if not isinstance(receipt, Mapping):
        return None
    uptake = receipt.get("graph_uptake")
    if not isinstance(uptake, Mapping):
        return None
    observed = uptake.get("observed")
    if not isinstance(observed, Mapping):
        return {"observed": False, "project_count": uptake.get("project_count")}
    nodes = observed.get("agent_nodes_by_type")
    tasks = observed.get("agent_tasks")
    return {
        "observed": True,
        "slug": observed.get("slug"),
        "agent_nodes_by_type": dict(nodes) if isinstance(nodes, Mapping) else {},
        "agent_nodes_total": (
            sum(int(value) for value in nodes.values()) if isinstance(nodes, Mapping) else 0
        ),
        "agent_tasks": dict(tasks) if isinstance(tasks, Mapping) else {},
        # The pre-registered failure mode: the seeded "fill this graph" task was
        # picked up instead of the exercise.
        "seeded_tasks_touched": observed.get("seeded_tasks_touched"),
        "project_count": uptake.get("project_count"),
    }


def run_suite(
    *,
    suite_id: str,
    dataset: Path,
    tasks: Sequence[str] | None = None,
    control_condition: str = "no-stele",
    treatment_condition: str = "stele-fresh",
    non_inferiority_margin: float = DEFAULT_NON_INFERIORITY_MARGIN,
    source: Path = DEFAULT_SOURCE,
    revision: str = DEFAULT_REVISION,
    artifact_manifest: Path | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = 60,
    max_budget_usd: str = "0.50",
    acknowledge_paid_suite_cap_usd: str | None = None,
    gateway_reserve_config: Path | None = None,
    timeout_seconds: float = 900.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
    reserve_checker: ReserveChecker = _provider_reserve_check,
    drain_checker: ReserveChecker = _provider_drain_check,
) -> dict[str, Any]:
    """Pair two arms across tasks borrowed from someone else's benchmark.

    This is the regression guard the rest of the apparatus cannot be: every
    other run shape asks a question we wrote, on a graph we froze, graded by a
    verifier we wrote. All three are places a favourable result can be authored
    without anyone intending to. Here the task, the workspace and the verifier
    are someone else's, none of them reward memory, and the only thing we supply
    is the agent configuration — so a difference in the score is a difference
    the install caused.

    Which makes the claim a negative one, and negative claims have their own
    discipline:

    - **The margin is an argument, not a result.** How much regression would
      count as harm has to be fixed before the run; `non_inferiority_margin`
      is recorded in the receipt for exactly that reason.
    - **Failing to detect harm is not evidence of safety.** The summary reports
      `inconclusive` distinctly from `non_inferior`, because at these sample
      sizes the honest answer is usually the former.
    - **The pairing is across tasks.** Each task is run by both arms, and the
      sample unit is the task; the arm order alternates so being second in the
      queue is not confounded with being the treatment.

    Every treatment task gets its own fresh project, created in-container by the
    same command a new user runs, and destroyed with the attempt afterwards.
    """

    suite_id = _safe_id(suite_id, "suite id")
    for role, condition in (
        ("control", control_condition),
        ("treatment", treatment_condition),
    ):
        if condition not in _BORROWED_CONDITIONS:
            raise OracleError(f"{role} condition must be one of: {', '.join(_BORROWED_CONDITIONS)}")
    if control_condition == treatment_condition:
        raise OracleError("control and treatment conditions must differ")
    if not isinstance(non_inferiority_margin, float) or not (0.0 < non_inferiority_margin < 1.0):
        raise OracleError("the non-inferiority margin must be a rate between zero and one")

    dataset = dataset.resolve()
    if not dataset.is_dir():
        raise OracleError("dataset must be a directory produced by prepare_external_dataset")

    root = _state_root(state_root)
    suite_root = root / "runs" / suite_id
    suite_receipt_path = suite_root / "suite.json"
    if suite_root.exists():
        raise OracleError("suite id already exists; inspect or clean it before retrying")

    # The borrowed inputs are certified first because their task list is what
    # determines how many arms the run has, and the acknowledged cap — the whole
    # basis of the money fence — cannot be validated until that number is known.
    dataset_provenance = _verify_borrowed_dataset(dataset)
    available = tuple(str(name) for name in dataset_provenance["tasks"])
    selected = tuple(tasks) if tasks else available
    if not selected:
        raise OracleError("a suite must run at least one task")
    if len(selected) != len(set(selected)):
        raise OracleError("task selection must not contain duplicates")
    unknown = sorted(set(selected) - set(available))
    if unknown:
        raise OracleError(f"dataset has no such tasks: {', '.join(unknown)}")

    arm_count = 2 * len(selected)
    budget, acknowledged_cap = _validate_paid_budget(
        max_budget_usd, acknowledge_paid_suite_cap_usd, arm_count
    )
    if max_turns < 1 or max_turns > 200:
        raise OracleError("max turns must be between 1 and 200")

    prepared = _prepare_paid_run(
        root=root,
        artifact_manifest=artifact_manifest,
        source=source,
        revision=revision,
        fixtures={},
        challenge="",
        acknowledged_cap=acknowledged_cap,
        gateway_reserve_config=gateway_reserve_config,
        runner=runner,
        reserve_checker=reserve_checker,
        dataset_provenance=dataset_provenance,
    )
    reserve = prepared.reserve

    created_at = _utc_now()
    suite_root.mkdir(mode=0o700, parents=True)
    attempt_root = root / "attempts"
    attempt_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = suite_root / "work"
    work.mkdir(mode=0o700)

    conditions = {"control": control_condition, "treatment": treatment_condition}
    arm_specs: list[dict[str, Any]] = []
    task_orders: dict[int, list[str]] = {}
    for index, task in enumerate(selected, start=1):
        order = ["control", "treatment"] if index % 2 == 1 else ["treatment", "control"]
        task_orders[index] = order
        for role in order:
            arm_specs.append(
                {
                    "condition": conditions[role],
                    "label": f"task-{index:02d}-{role}",
                    "task": task,
                }
            )

    suite_receipt: dict[str, Any] = {
        "schema_version": 1,
        "suite_id": suite_id,
        "created_at": created_at,
        "completed_at": None,
        "status": "in_progress",
        "error": None,
        "configuration": {
            "task_image": harbor_pack.task_image(),
            # The dataset stands where a challenge id stands in every other run
            # shape: it is the question being asked.
            "challenge_id": dataset.name,
            "source_commit": prepared.source_commit,
            # There is no fixture. Naming the provisioning mode keeps the field
            # honest instead of borrowing a fixture id that does not exist.
            "fixture_id": "created-per-task",
            "model": model,
            "attempts": len(selected),
            "arm_count": arm_count,
            "control_condition": control_condition,
            "treatment_condition": treatment_condition,
            "tasks": list(selected),
            "task_arm_order": {str(index): order for index, order in task_orders.items()},
            "non_inferiority_margin": non_inferiority_margin,
            "max_turns": max_turns,
            "max_budget_usd_per_arm": str(budget),
            "acknowledged_paid_suite_cap_usd": str(acknowledged_cap),
        },
        "arms": {},
        "attempts": [],
        "comparable": False,
        "summary": None,
        "billing": {
            "gateway_total_usd": None,
            "source": None,
            "recorded_at": None,
            "claude_reported_usd_by_condition": {},
            "gateway_reserve_preflight": reserve,
            "gateway_drain_checks": [],
            "gateway_drain_stop": None,
        },
        "provenance": {
            "artifact_manifest": os.fspath(prepared.manifest_path.resolve()),
            "artifact_sha256": prepared.artifact["sha256"],
            "artifact_target": prepared.artifact["target"],
            "artifact_source": prepared.manifest.get("source"),
            # The dataset's two digests take the places the challenge and
            # verifier digests take elsewhere, and mean the same thing: what was
            # asked, and how it was graded. An upstream change to either has to
            # break comparability rather than move the score.
            "challenge_manifest_sha256": dataset_provenance["workspaces_sha256"],
            "verifier_sha256": dataset_provenance["tests_sha256"],
            "dataset": dict(dataset_provenance),
            "runtime": prepared.runtime(),
        },
    }
    _atomic_json(suite_receipt_path, suite_receipt)

    def _invoke_suite_arm(
        *, spec: Mapping[str, Any], run_id: str, receipt_path: Path
    ) -> dict[str, Any]:
        return _invoke_borrowed_arm(
            condition=str(spec["condition"]),
            task=str(spec["task"]),
            run_id=run_id,
            dataset=dataset,
            artifact=prepared.artifact,
            backend_url=prepared.backend_url,
            backend_key=prepared.backend_key,
            model=model,
            max_turns=max_turns,
            max_budget_usd=str(budget),
            timeout_seconds=timeout_seconds,
            attempts=attempt_root,
            work=work,
            receipt_path=receipt_path,
            runner=runner,
        )

    arms, interrupted, primary_error = _run_arm_sequence(
        arm_specs,
        run_id_prefix=suite_id,
        run_root=suite_root,
        work=work,
        receipt=suite_receipt,
        receipt_path=suite_receipt_path,
        reserve=reserve,
        drain_checker=drain_checker,
        invoke=_invoke_suite_arm,
    )

    for spec in arm_specs:
        label = str(spec["label"])
        if label not in arms:
            arms[label] = _not_run_arm(
                f"{suite_id}-{label}", suite_root / f"{label}.json", work, "not_started"
            )

    attempt_records: list[dict[str, Any]] = []
    claude_costs: dict[str, float | None] = {}
    for index, task in enumerate(selected, start=1):
        record: dict[str, Any] = {
            "attempt": index,
            "task": task,
            "arm_order": task_orders[index],
            "labels": {},
            "metrics": {},
            "outcomes": {},
            "deltas": None,
            "memory_evidence": {},
            "graph_uptake": None,
            "external_source_access": {},
            "budget_truncation_risk": [],
            "analysable": False,
            "excluded_reason": None,
        }
        receipts: dict[str, Any] = {}
        for role in ("control", "treatment"):
            arm_label = f"task-{index:02d}-{role}"
            record["labels"][role] = arm_label
            arm = arms[arm_label]
            receipt = arm["receipt"]
            arm_metrics = _arm_metrics(receipt) if isinstance(receipt, Mapping) else None
            receipts[role] = receipt if arm["exit_code"] == 0 else None
            record["metrics"][role] = arm_metrics or {}
            record["outcomes"][role] = (
                receipt.get("outcome") if isinstance(receipt, Mapping) else None
            )
            claude_costs[arm_label] = (
                arm_metrics.get("claude_reported_usd") if arm_metrics else None
            )
            risk = _budget_truncation_risk(receipt, arm_metrics, _TRUNCATION_RISK_FRACTION)
            if risk is not None:
                record["budget_truncation_risk"].append({"arm": role, **risk})
            record["external_source_access"][role] = _arm_reached_the_network(receipt)
            if conditions[role] in _BORROWED_GRAPH_CONDITIONS:
                record["graph_uptake"] = _suite_graph_uptake(arm)
                record["memory_evidence"][role] = _arm_memory_evidence(receipt)
        invalid = sorted(
            role for role, outcome in record["outcomes"].items() if outcome == "invalid"
        )
        # An arm can exit cleanly and still report an infrastructure error, for
        # example when Claude Code aborts with OutputTokenExceededError. Its
        # verifier then scores an unfinished workspace, which is not a verdict.
        broken = sorted(
            role
            for role, outcome in record["outcomes"].items()
            if outcome == "infrastructure-error"
        )
        if receipts["control"] is None or receipts["treatment"] is None:
            record["excluded_reason"] = "arm_did_not_complete"
        elif broken:
            record["excluded_reason"] = f"arm_infrastructure_error:{','.join(broken)}"
        elif invalid:
            # This is where a trial that reached the reference solution leaves
            # the analysis: the runner marked it invalid, so the whole paired
            # task is dropped rather than scored on one honest arm.
            record["excluded_reason"] = f"arm_outcome_invalid:{','.join(invalid)}"
        else:
            record["deltas"] = _pair_deltas(receipts["control"], receipts["treatment"])
            if record["deltas"] is None:
                record["excluded_reason"] = "arms_are_not_comparable"
            else:
                record["analysable"] = True
        attempt_records.append(record)

    summary = _suite_summary(attempt_records, non_inferiority_margin)
    arm_commands_succeeded = all(arm["exit_code"] == 0 for arm in arms.values())
    successful = interrupted is None and arm_commands_succeeded
    suite_receipt.update(
        {
            "completed_at": _utc_now(),
            "status": (
                "interrupted"
                if interrupted is not None
                else "completed"
                if successful
                else "failed"
            ),
            "error": primary_error or (None if successful else "one_or_more_conditions_failed"),
            "arms": arms,
            "attempts": attempt_records,
            "comparable": summary["attempts_analysed"] == len(selected),
            "summary": summary,
        }
    )
    suite_receipt["billing"]["claude_reported_usd_by_condition"] = claude_costs
    _atomic_json(suite_receipt_path, suite_receipt)
    if interrupted is not None:
        raise interrupted
    return {
        "ok": successful,
        "suite_receipt": os.fspath(suite_receipt_path),
        "receipt": suite_receipt,
    }


def preflight(
    *,
    run_id: str,
    source: Path = DEFAULT_SOURCE,
    revision: str = DEFAULT_REVISION,
    fixture: Path = DEFAULT_FIXTURE,
    challenge: str = DEFAULT_CHALLENGE,
    artifact_manifest: Path | None = None,
    timeout_seconds: float = 600.0,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    """Run the matched install-only pair without invoking an agent model."""

    run_id = _safe_id(run_id, "run id")
    root = _state_root(state_root)
    preflight_root = root / "preflights" / run_id
    if preflight_root.exists():
        raise OracleError("preflight run id already exists; inspect or clean retained work")
    manifest_path = artifact_manifest or root / "artifacts" / "current.json"
    manifest = _validate_artifact_manifest(manifest_path)
    artifact = manifest["artifact"]
    _validate_source(runner, source.resolve(), revision)
    _validate_fixture_and_challenge(fixture, challenge)
    _certify_packaged_inputs(
        runner, source=source, revision=revision, fixture=fixture, challenge=challenge
    )
    backend_url, backend_key, _ = _supabase_configuration(runner)
    docker_backend_url = _docker_reachable_url(backend_url)
    docker_arch, _ = _docker_arch(runner)
    if artifact.get("target") != _target_for_arch(docker_arch):
        raise OracleError("artifact target does not match Docker architecture")
    harbor_version = _command_version(runner, ("harbor", "--version"))
    if HARBOR_VERSION.search(harbor_version) is None:
        raise OracleError(f"Harbor 0.20.x is required; found {harbor_version or 'unknown'}")

    preflight_root.mkdir(mode=0o700, parents=True)
    attempts = root / "attempts"
    attempts.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = preflight_root / "work"
    work.mkdir(mode=0o700)
    receipt_path = preflight_root / "receipt.json"
    argv = (
        sys.executable,
        os.fspath(INSTALL_ONLY_RUNNER),
        os.fspath(source.resolve()),
        revision,
        os.fspath(fixture.resolve()),
        "--attempt-root",
        os.fspath(attempts),
        "--work-root",
        os.fspath(work),
        "--run-id",
        run_id,
        "--artifact",
        str(artifact["path"]),
        "--artifact-sha256",
        str(artifact["sha256"]),
        "--artifact-source-kind",
        "local-dirty-worktree",
        "--backend-url",
        docker_backend_url,
        "--backend-anon-key",
        backend_key,
        "--timeout-seconds",
        str(timeout_seconds),
        "--condition",
        "no-stele",
        "--condition",
        "faithful",
        "--challenge",
        challenge,
        "--receipt-out",
        os.fspath(receipt_path),
    )
    environment = dict(os.environ)
    for name in (
        "AI_GATEWAY_KEY_EVAL",
        model_provider.ANTHROPIC_KEY_ENV,
        model_provider.ANTHROPIC_ADMIN_KEY_ENV,
    ):
        environment.pop(name, None)
    try:
        result = runner(
            argv,
            cwd=REPOSITORY_ROOT / "eval" / "stele-bench",
            env=environment,
            timeout=timeout_seconds + 180.0,
        )
    except Exception as error:
        envelope = {
            "ok": False,
            "failure": {
                "code": "install_only_runner_unavailable",
                "message": str(error)[:500] or error.__class__.__name__,
            },
            "receipt": None,
        }
        _atomic_json(receipt_path, envelope)
        return {
            "ok": False,
            "mode": "install-only",
            "zero_model": True,
            "run_id": run_id,
            "exit_code": None,
            "failure": envelope["failure"],
            "receipt_path": os.fspath(receipt_path),
            "evidence_root": os.fspath(work),
        }
    if receipt_path.is_file():
        envelope = _read_json(receipt_path)
    else:
        envelope = _last_json_object(result.stdout) or {
            "ok": False,
            "failure": {
                "code": "install_only_runner_failed_to_emit_receipt",
                "message": "The install-only runner exited without a receipt.",
            },
            "receipt": None,
        }
        _atomic_json(receipt_path, envelope)
    successful = result.returncode == 0 and envelope.get("ok") is True
    failure = envelope.get("failure")
    return {
        "ok": successful,
        "mode": "install-only",
        "zero_model": True,
        "run_id": run_id,
        "exit_code": result.returncode,
        "failure": failure if isinstance(failure, dict) else None,
        "receipt_path": os.fspath(receipt_path),
        "evidence_root": os.fspath(work),
    }


# A run's receipt is named for its shape. Readers resolve whichever exists so
# inspect/record-billing/clean work for every shape, not just a pair.
_RUN_RECEIPT_NAMES = ("pair.json", "quad.json", "repeat.json", "suite.json")


def _run_receipt_path(root: Path, run_id: str) -> Path:
    run_root = root / "runs" / run_id
    for name in _RUN_RECEIPT_NAMES:
        candidate = run_root / name
        if candidate.is_file():
            return candidate
    return run_root / "pair.json"


def _receipt_arm_labels(receipt: Mapping[str, Any]) -> tuple[str, ...]:
    """The arm labels a run actually used, for cleanup after a crash.

    Read from the receipt rather than assumed, because a quad's labels are its
    variants and a repeat's are per-attempt — assuming control/faithful would
    silently leave every other arm's project behind.
    """

    arms = receipt.get("arms")
    if isinstance(arms, Mapping) and arms:
        return tuple(str(label) for label in arms)
    return ("control", "faithful")


def export_result(
    run_id: str,
    *,
    state_root: Path | None = None,
    results_root: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Copy a run's durable record out of machine-local state into the repo.

    The receipt lives in `~/.local/state/…` next to gigabytes of corpus trees and
    trajectories that `clean --delete-work` is expected to remove, on one
    machine, with no backup. The record is the part that has to outlive all of
    that, so it goes in git: versioned, diffable, machine-independent, and it
    travels with the code that produced it.
    """

    root = _state_root(state_root)
    run_id = _safe_id(run_id, "run id")
    receipt_path = _run_receipt_path(root, run_id)
    if not receipt_path.is_file():
        raise OracleError("no run receipt for that id; nothing to export")

    try:
        records = results.build_run_records(_read_json(receipt_path))
    except results.ResultsError as error:
        raise OracleError(f"run cannot be recorded: {error}") from error
    if not records:
        raise OracleError("run produced no recordable comparison")

    destination_root = (results_root or RESULTS_ROOT).resolve()
    written: list[dict[str, Any]] = []
    for record in records:
        destination = (
            destination_root / record.comparability.challenge_id / results.record_filename(record)
        )
        if destination.exists() and not overwrite:
            raise OracleError(f"record already exists: {destination} (pass --overwrite)")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Not `_atomic_json`: these are committed artefacts, not private state,
        # and 0600 would make them unreadable to anyone else checking out the
        # repository.
        destination.write_text(results.serialise_record(record), encoding="utf-8")
        written.append(
            {
                "run_id": record.run_id,
                "record": os.fspath(destination),
                "comparability_digest": record.comparability_digest,
                "attempts": len(record.attempts),
                "aggregates": len(record.aggregates),
            }
        )
    return {"ok": True, "run_id": run_id, "records": written}


def load_result_records(results_root: Path | None = None) -> list[results.RunRecord]:
    root = (results_root or RESULTS_ROOT).resolve()
    if not root.is_dir():
        return []
    records: list[results.RunRecord] = []
    for path in sorted(root.rglob("*.json")):
        try:
            records.append(results.load_record(path.read_text(encoding="utf-8")))
        except (OSError, results.ResultsError, ValueError) as error:
            # Loud, not silent: a record that cannot be read is a hole in the
            # history, and a viewer that quietly skips it reports a shorter
            # trend as if it were complete.
            raise OracleError(f"unreadable result record {path}: {error}") from error
    return records


def report_results(
    *,
    results_root: Path | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    """Render every durable record into one self-contained historical view."""

    records = load_result_records(results_root)
    destination = (output or (results_root or RESULTS_ROOT) / "history.html").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report.render_report(records, generated_at=_utc_now()), encoding="utf-8")
    return {
        "ok": True,
        "report": os.fspath(destination),
        "summary": report.render_summary_text(records),
        **report.comparability_meta(records),
    }


def inspect_pair(pair_id: str, *, state_root: Path | None = None) -> dict[str, Any]:
    root = _state_root(state_root)
    pair_id = _safe_id(pair_id)
    path = _run_receipt_path(root, pair_id)
    preflight_path = root / "preflights" / pair_id / "receipt.json"
    if not path.is_file() and preflight_path.is_file():
        envelope = _read_json(preflight_path)
        receipt = envelope.get("receipt")
        failure = envelope.get("failure")
        return {
            "ok": True,
            "mode": "install-only",
            "run_id": pair_id,
            "status": "completed" if envelope.get("ok") is True else "failed",
            "failure": failure if isinstance(failure, dict) else None,
            "receipt": receipt if isinstance(receipt, dict) else None,
            "receipt_path": os.fspath(preflight_path),
            "evidence_root": os.fspath(preflight_path.parent / "work"),
        }
    receipt = _read_json(path)
    arms = receipt.get("arms") if isinstance(receipt.get("arms"), dict) else {}
    inspected = {
        "ok": True,
        "pair_id": pair_id,
        "status": receipt.get("status"),
        "comparable": receipt.get("comparable"),
        "configuration": receipt.get("configuration"),
        "billing": receipt.get("billing"),
        "deltas": receipt.get("deltas"),
        "evidence": {
            label: {
                "run_id": arm.get("run_id"),
                "receipt_path": arm.get("receipt_path"),
                "evidence_root": arm.get("evidence_root"),
            }
            for label, arm in arms.items()
            if isinstance(arm, dict)
        },
        "pair_receipt": os.fspath(path),
    }
    # A repeat's headline is its aggregate, not any single arm's numbers.
    if receipt.get("summary") is not None or "attempts" in receipt:
        inspected["summary"] = receipt.get("summary")
        inspected["attempts"] = receipt.get("attempts")
    return inspected


def record_billing(
    pair_id: str, gateway_total_usd: str, *, state_root: Path | None = None
) -> dict[str, Any]:
    try:
        amount = Decimal(gateway_total_usd)
    except InvalidOperation as error:
        raise OracleError("gateway total must be a decimal") from error
    if not amount.is_finite() or amount < 0:
        raise OracleError("gateway total must be a finite non-negative decimal")
    root = _state_root(state_root)
    path = _run_receipt_path(root, _safe_id(pair_id))
    receipt = _read_json(path)
    billing = receipt.get("billing")
    if not isinstance(billing, dict):
        raise OracleError("pair receipt has no billing record")
    billing["gateway_total_usd"] = str(amount)
    billing["source"] = "manual-vercel-dashboard"
    billing["recorded_at"] = _utc_now()
    receipt["billing"] = billing
    _atomic_json(path, receipt)
    return {"ok": True, "pair_id": pair_id, "billing": billing, "pair_receipt": os.fspath(path)}


def _cleanup_attempt(run_id: str, attempts: Path, *, runner: CommandRunner) -> bool:
    environment = dict(os.environ)
    environment["STELE_BENCH_ATTEMPT_ROOT"] = os.fspath(attempts.resolve())
    provisioner = _provisioner_executable()
    command = (
        (sys.executable, os.fspath(provisioner), "cleanup")
        if provisioner == PROVISIONER
        else (os.fspath(provisioner), "cleanup")
    )
    result = runner(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        input_text=json.dumps({"run_id": run_id}, separators=(",", ":")),
        timeout=120.0,
    )
    response = _last_json_object(result.stdout)
    return result.returncode == 0 and response == {"ok": True}


def clean_pair(
    pair_id: str,
    *,
    delete_work: bool = False,
    state_root: Path | None = None,
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    root = _state_root(state_root)
    pair_id = _safe_id(pair_id)
    attempts = root / "attempts"
    attempts.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt_path = _run_receipt_path(root, pair_id)
    labels = (
        _receipt_arm_labels(_read_json(receipt_path))
        if receipt_path.is_file()
        else ("control", "faithful")
    )
    cleaned = {
        label: _cleanup_attempt(f"{pair_id}-{label}", attempts, runner=runner) for label in labels
    }
    cleaned["preflight"] = _cleanup_attempt(pair_id, attempts, runner=runner)
    pair_root = root / "runs" / pair_id
    deleted = False
    if delete_work:
        for work in (
            pair_root / "work",
            root / "preflights" / pair_id / "work",
        ):
            if not work.exists():
                continue
            if work.is_symlink() or not work.is_dir():
                raise OracleError("retained work path is unsafe")
            shutil.rmtree(work)
            deleted = True
    return {
        "ok": all(cleaned.values()),
        "pair_id": pair_id,
        "attempts": cleaned,
        "work_deleted": deleted,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument(
        "--model-provider",
        choices=model_provider.PROVIDERS,
        default=os.environ.get(model_provider.PROVIDER_ENV) or model_provider.GATEWAY,
        help=(
            "where the agent's model calls are billed: the Vercel AI Gateway "
            f"({model_provider.GATEWAY_KEY_ENV}) or the first-party Anthropic API "
            f"({model_provider.ANTHROPIC_KEY_ENV}, fenced by "
            f"<state-root>/{anthropic_reserve.DEFAULT_CONFIG_NAME}; "
            "--gateway-reserve-config overrides that path too)"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    doctor_parser.add_argument("--revision", default=DEFAULT_REVISION)
    doctor_parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    doctor_parser.add_argument("--challenge", default=DEFAULT_CHALLENGE)
    doctor_parser.add_argument("--artifact-manifest", type=Path)
    doctor_parser.add_argument("--gateway-reserve-config", type=Path)

    subparsers.add_parser("build-local-artifact")

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("run_id")
    preflight_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    preflight_parser.add_argument("--revision", default=DEFAULT_REVISION)
    preflight_parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    preflight_parser.add_argument("--challenge", default=DEFAULT_CHALLENGE)
    preflight_parser.add_argument("--artifact-manifest", type=Path)
    preflight_parser.add_argument("--timeout-seconds", type=float, default=600.0)

    run_parser = subparsers.add_parser("run-pair")
    run_parser.add_argument("pair_id")
    run_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    run_parser.add_argument("--revision", default=DEFAULT_REVISION)
    run_parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    run_parser.add_argument("--challenge", default=DEFAULT_CHALLENGE)
    run_parser.add_argument("--artifact-manifest", type=Path)
    run_parser.add_argument("--model", default=DEFAULT_MODEL)
    run_parser.add_argument("--max-turns", type=int, default=60)
    run_parser.add_argument("--max-budget-usd", default="5.00")
    run_parser.add_argument(
        "--acknowledge-paid-pair-cap-usd",
        required=True,
        help=(
            "confirm the exact two-arm requested cap and that AI_GATEWAY_KEY_EVAL "
            "is a dedicated eval key protected by an external budget ceiling"
        ),
    )
    run_parser.add_argument("--timeout-seconds", type=float, default=900.0)
    run_parser.add_argument(
        "--gateway-reserve-config",
        type=Path,
        help=(
            "path to the shared-Gateway spending fence configuration "
            f"(defaults to <state-root>/{gateway_reserve.DEFAULT_CONFIG_NAME})"
        ),
    )

    quad_parser = subparsers.add_parser("run-quad")
    quad_parser.add_argument("quad_id")
    quad_parser.add_argument(
        "--cohort-root",
        type=Path,
        required=True,
        help="fixture cohort directory containing one subdir per graph variant",
    )
    quad_parser.add_argument("--challenge", required=True)
    quad_parser.add_argument(
        "--variant",
        action="append",
        dest="variants",
        help=(
            "graph variant subdir to run as a faithful arm (repeatable); "
            f"defaults to {', '.join(SESSION_BOUNDARY_VARIANTS)}"
        ),
    )
    quad_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    quad_parser.add_argument("--revision", default=DEFAULT_REVISION)
    quad_parser.add_argument("--artifact-manifest", type=Path)
    quad_parser.add_argument("--model", default=DEFAULT_MODEL)
    quad_parser.add_argument("--max-turns", type=int, default=60)
    quad_parser.add_argument("--max-budget-usd", default="5.00")
    quad_parser.add_argument(
        "--acknowledge-paid-quad-cap-usd",
        required=True,
        help=(
            "confirm the exact N-arm requested cap (per-arm budget x [1 control + "
            "variants]) and that AI_GATEWAY_KEY_EVAL is a dedicated eval key protected "
            "by an external budget ceiling"
        ),
    )
    quad_parser.add_argument("--timeout-seconds", type=float, default=900.0)
    quad_parser.add_argument(
        "--gateway-reserve-config",
        type=Path,
        help=(
            "path to the shared-Gateway spending fence configuration "
            f"(defaults to <state-root>/{gateway_reserve.DEFAULT_CONFIG_NAME})"
        ),
    )

    repeat_parser = subparsers.add_parser("run-repeat")
    repeat_parser.add_argument("repeat_id")
    repeat_parser.add_argument(
        "--attempts",
        type=int,
        default=5,
        help="paired attempts to run (default 5)",
    )
    repeat_parser.add_argument(
        "--control-condition",
        choices=_REPEAT_CONDITIONS,
        default="no-stele",
        help=(
            "what the treatment is compared against: no-stele measures what the "
            "graph is worth at all; faithful-norag measures what proactive "
            "injection is worth, holding the graph constant (default no-stele)"
        ),
    )
    repeat_parser.add_argument(
        "--treatment-condition",
        choices=_REPEAT_CONDITIONS,
        default="faithful",
        help="the arm under test (default faithful)",
    )
    repeat_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    repeat_parser.add_argument("--revision", default=DEFAULT_REVISION)
    repeat_parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    repeat_parser.add_argument("--challenge", default=DEFAULT_CHALLENGE)
    repeat_parser.add_argument("--artifact-manifest", type=Path)
    repeat_parser.add_argument("--model", default=DEFAULT_MODEL)
    repeat_parser.add_argument("--max-turns", type=int, default=60)
    repeat_parser.add_argument("--max-budget-usd", default="5.00")
    repeat_parser.add_argument(
        "--acknowledge-paid-repeat-cap-usd",
        required=True,
        help=(
            "confirm the exact whole-repeat requested cap (per-arm budget x 2 x attempts) "
            "and that AI_GATEWAY_KEY_EVAL is a dedicated eval key protected by an "
            "external budget ceiling"
        ),
    )
    repeat_parser.add_argument("--timeout-seconds", type=float, default=900.0)
    repeat_parser.add_argument(
        "--gateway-reserve-config",
        type=Path,
        help=(
            "path to the shared-Gateway spending fence configuration "
            f"(defaults to <state-root>/{gateway_reserve.DEFAULT_CONFIG_NAME})"
        ),
    )

    compounding_parser = subparsers.add_parser(
        "run-compounding",
        help="run the locked two-session intake-to-reuse experiment",
    )
    compounding_parser.add_argument("run_id")
    compounding_parser.add_argument(
        "--admission",
        action="store_true",
        help="run the single discarded admission pair instead of the fixed N=10 cohort",
    )
    compounding_parser.add_argument(
        "--attempts",
        type=int,
        help="fixed automatically to 1 with --admission and 10 otherwise",
    )
    compounding_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    compounding_parser.add_argument("--revision", default=DEFAULT_REVISION)
    compounding_parser.add_argument(
        "--certification-fixture", type=Path, default=DEFAULT_COMPOUNDING_FIXTURE
    )
    compounding_parser.add_argument("--artifact-manifest", type=Path)
    compounding_parser.add_argument("--model", default=DEFAULT_COMPOUNDING_MODEL)
    compounding_parser.add_argument("--max-turns", type=int, default=45)
    compounding_parser.add_argument("--max-budget-usd", default="10.00")
    compounding_parser.add_argument(
        "--acknowledge-paid-cap-usd",
        required=True,
        help=("confirm the exact cap: per-session budget x 4 sessions x paired attempts"),
    )
    compounding_parser.add_argument("--timeout-seconds", type=float, default=900.0)
    compounding_parser.add_argument(
        "--gateway-reserve-config",
        type=Path,
        help=(
            "path to the shared-Gateway spending fence configuration "
            f"(defaults to <state-root>/{gateway_reserve.DEFAULT_CONFIG_NAME})"
        ),
    )

    compounding_batch_parser = subparsers.add_parser(
        "run-compounding-batch",
        help="append exactly one frozen pair to the registered N=10 cohort",
    )
    compounding_batch_parser.add_argument("cohort_id")
    compounding_batch_parser.add_argument("--attempt-index", type=int, required=True)
    compounding_batch_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    compounding_batch_parser.add_argument("--revision", default=DEFAULT_REVISION)
    compounding_batch_parser.add_argument(
        "--certification-fixture", type=Path, default=DEFAULT_COMPOUNDING_FIXTURE
    )
    compounding_batch_parser.add_argument("--artifact-manifest", type=Path)
    compounding_batch_parser.add_argument("--model", default=DEFAULT_COMPOUNDING_MODEL)
    compounding_batch_parser.add_argument("--max-turns", type=int, default=45)
    compounding_batch_parser.add_argument("--max-budget-usd", default="10.00")
    compounding_batch_parser.add_argument(
        "--acknowledge-paid-cap-usd",
        required=True,
        help="confirm the exact one-pair cap: per-session budget x 4 sessions",
    )
    compounding_batch_parser.add_argument("--timeout-seconds", type=float, default=900.0)
    compounding_batch_parser.add_argument(
        "--gateway-reserve-config",
        type=Path,
        help=(
            "path to the shared-Gateway spending fence configuration "
            f"(defaults to <state-root>/{gateway_reserve.DEFAULT_CONFIG_NAME})"
        ),
    )

    k5_admission_parser = subparsers.add_parser(
        "run-k5-admission",
        help="run the one explicitly approved, discarded Click K=5 admission pair",
    )
    k5_admission_parser.add_argument("run_id")
    k5_admission_parser.add_argument("--source", type=Path, required=True)
    k5_admission_parser.add_argument(
        "--chain-manifest", type=Path, default=DEFAULT_K5_CHAIN_MANIFEST
    )
    k5_admission_parser.add_argument(
        "--certification-fixture", type=Path, default=DEFAULT_COMPOUNDING_FIXTURE
    )
    k5_admission_parser.add_argument("--artifact-manifest", type=Path)
    k5_admission_parser.add_argument(
        "--acknowledge-paid-cap-usd",
        required=True,
        help="must be exactly 30.00 for ten sessions at the registered $3 cap",
    )
    k5_admission_parser.add_argument("--gateway-reserve-config", type=Path)

    k5_batch_parser = subparsers.add_parser(
        "run-k5-batch",
        help="append exactly the next pair to the frozen 20-pair Click K=5 cohort",
    )
    k5_batch_parser.add_argument("cohort_id")
    k5_batch_parser.add_argument("--attempt-index", type=int, required=True)
    k5_batch_parser.add_argument("--source", type=Path, required=True)
    k5_batch_parser.add_argument("--chain-manifest", type=Path, default=DEFAULT_K5_CHAIN_MANIFEST)
    k5_batch_parser.add_argument(
        "--certification-fixture", type=Path, default=DEFAULT_COMPOUNDING_FIXTURE
    )
    k5_batch_parser.add_argument("--artifact-manifest", type=Path)
    k5_batch_parser.add_argument(
        "--acknowledge-paid-cap-usd",
        required=True,
        help="must be exactly 30.00 for this pair",
    )
    k5_batch_parser.add_argument(
        "--acknowledge-cohort-cap-usd",
        required=True,
        help="must be exactly 600.00 for the frozen 20-pair cohort",
    )
    k5_batch_parser.add_argument("--gateway-reserve-config", type=Path)

    continuity_admission_parser = subparsers.add_parser(
        "run-continuity-admission",
        help="run the one explicitly approved, discarded Fieldnote continuity admission pair",
    )
    continuity_admission_parser.add_argument("run_id")
    continuity_admission_parser.add_argument("--source", type=Path, required=True)
    continuity_admission_parser.add_argument(
        "--chain-manifest", type=Path, default=DEFAULT_CONTINUITY_CHAIN_MANIFEST
    )
    continuity_admission_parser.add_argument(
        "--certification-fixture", type=Path, default=DEFAULT_COMPOUNDING_FIXTURE
    )
    continuity_admission_parser.add_argument("--artifact-manifest", type=Path)
    continuity_admission_parser.add_argument(
        "--acknowledge-paid-cap-usd",
        required=True,
        help="must be exactly 30.00 for ten billed steps across the paired chains",
    )
    continuity_admission_parser.add_argument("--gateway-reserve-config", type=Path)

    continuity_batch_parser = subparsers.add_parser(
        "run-continuity-batch",
        help="append the next pair to the frozen 20-pair Fieldnote continuity cohort",
    )
    continuity_batch_parser.add_argument("cohort_id")
    continuity_batch_parser.add_argument("--attempt-index", type=int, required=True)
    continuity_batch_parser.add_argument("--source", type=Path, required=True)
    continuity_batch_parser.add_argument(
        "--chain-manifest", type=Path, default=DEFAULT_CONTINUITY_CHAIN_MANIFEST
    )
    continuity_batch_parser.add_argument(
        "--certification-fixture", type=Path, default=DEFAULT_COMPOUNDING_FIXTURE
    )
    continuity_batch_parser.add_argument("--artifact-manifest", type=Path)
    continuity_batch_parser.add_argument(
        "--acknowledge-paid-cap-usd",
        required=True,
        help="must be exactly 30.00 for this pair",
    )
    continuity_batch_parser.add_argument(
        "--acknowledge-cohort-cap-usd",
        required=True,
        help="must be exactly 600.00 for the frozen 20-pair cohort",
    )
    continuity_batch_parser.add_argument("--gateway-reserve-config", type=Path)

    suite_parser = subparsers.add_parser(
        "run-suite",
        help="pair two arms across tasks borrowed from another benchmark",
    )
    suite_parser.add_argument("suite_id")
    suite_parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="a dataset directory produced by prepare_external_dataset.py",
    )
    suite_parser.add_argument(
        "--task",
        action="append",
        dest="tasks",
        help="task to run (repeat); default is every task in the dataset",
    )
    suite_parser.add_argument(
        "--control-condition", choices=_BORROWED_CONDITIONS, default="no-stele"
    )
    suite_parser.add_argument(
        "--treatment-condition", choices=_BORROWED_CONDITIONS, default="stele-fresh"
    )
    suite_parser.add_argument(
        "--non-inferiority-margin",
        type=float,
        default=DEFAULT_NON_INFERIORITY_MARGIN,
        help=(
            "the largest solve-rate drop that would still count as no harm, as a "
            "rate — declare it before running, not after seeing the number "
            f"(default {DEFAULT_NON_INFERIORITY_MARGIN})"
        ),
    )
    suite_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    suite_parser.add_argument("--revision", default=DEFAULT_REVISION)
    suite_parser.add_argument("--artifact-manifest", type=Path)
    suite_parser.add_argument("--model", default=DEFAULT_MODEL)
    suite_parser.add_argument("--max-turns", type=int, default=60)
    suite_parser.add_argument("--max-budget-usd", default="0.50")
    suite_parser.add_argument(
        "--acknowledge-paid-suite-cap-usd",
        required=True,
        help=(
            "confirm the exact whole-suite requested cap (per-arm budget x 2 x tasks) "
            "and that AI_GATEWAY_KEY_EVAL is a dedicated eval key protected by an "
            "external budget ceiling"
        ),
    )
    suite_parser.add_argument("--timeout-seconds", type=float, default=900.0)
    suite_parser.add_argument(
        "--gateway-reserve-config",
        type=Path,
        help=(
            "path to the shared-Gateway spending fence configuration "
            f"(defaults to <state-root>/{gateway_reserve.DEFAULT_CONFIG_NAME})"
        ),
    )

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("pair_id")

    billing_parser = subparsers.add_parser("record-billing")
    billing_parser.add_argument("pair_id")
    billing_parser.add_argument("--gateway-total-usd", required=True)

    clean_parser = subparsers.add_parser("clean")
    clean_parser.add_argument("pair_id")
    clean_parser.add_argument("--delete-work", action="store_true")

    export_parser = subparsers.add_parser(
        "export-result",
        help="write a run's durable record into the repository (commit it)",
    )
    export_parser.add_argument("run_id")
    export_parser.add_argument("--results-root", type=Path)
    export_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing record for this run",
    )

    report_parser = subparsers.add_parser(
        "report",
        help="render every durable record into one self-contained historical view",
    )
    report_parser.add_argument("--results-root", type=Path)
    report_parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        # Exported, not passed: every runner subprocess reads the provider from
        # the environment, so no arm can be billed differently from the fence.
        model_provider.activate(args.model_provider)
        if args.model_provider == model_provider.ANTHROPIC and getattr(args, "model", None):
            model_provider.direct_model_id(args.model)
        with _controlled_termination_signals():
            if args.command == "doctor":
                result = doctor(
                    source=args.source,
                    revision=args.revision,
                    fixture=args.fixture,
                    challenge=args.challenge,
                    artifact_manifest=args.artifact_manifest,
                    gateway_reserve_config=args.gateway_reserve_config,
                    state_root=args.state_root,
                )
            elif args.command == "build-local-artifact":
                result = build_local_artifact(state_root=args.state_root)
            elif args.command == "preflight":
                result = preflight(
                    run_id=args.run_id,
                    source=args.source,
                    revision=args.revision,
                    fixture=args.fixture,
                    challenge=args.challenge,
                    artifact_manifest=args.artifact_manifest,
                    timeout_seconds=args.timeout_seconds,
                    state_root=args.state_root,
                )
            elif args.command == "run-pair":
                result = run_pair(
                    pair_id=args.pair_id,
                    source=args.source,
                    revision=args.revision,
                    fixture=args.fixture,
                    challenge=args.challenge,
                    artifact_manifest=args.artifact_manifest,
                    model=args.model,
                    max_turns=args.max_turns,
                    max_budget_usd=args.max_budget_usd,
                    acknowledge_paid_pair_cap_usd=args.acknowledge_paid_pair_cap_usd,
                    gateway_reserve_config=args.gateway_reserve_config,
                    timeout_seconds=args.timeout_seconds,
                    state_root=args.state_root,
                )
            elif args.command == "run-quad":
                result = run_quad(
                    quad_id=args.quad_id,
                    cohort_root=args.cohort_root,
                    challenge=args.challenge,
                    variants=tuple(args.variants) if args.variants else SESSION_BOUNDARY_VARIANTS,
                    source=args.source,
                    revision=args.revision,
                    artifact_manifest=args.artifact_manifest,
                    model=args.model,
                    max_turns=args.max_turns,
                    max_budget_usd=args.max_budget_usd,
                    acknowledge_paid_quad_cap_usd=args.acknowledge_paid_quad_cap_usd,
                    gateway_reserve_config=args.gateway_reserve_config,
                    timeout_seconds=args.timeout_seconds,
                    state_root=args.state_root,
                )
            elif args.command == "run-repeat":
                result = run_repeat(
                    repeat_id=args.repeat_id,
                    attempts=args.attempts,
                    control_condition=args.control_condition,
                    treatment_condition=args.treatment_condition,
                    source=args.source,
                    revision=args.revision,
                    fixture=args.fixture,
                    challenge=args.challenge,
                    artifact_manifest=args.artifact_manifest,
                    model=args.model,
                    max_turns=args.max_turns,
                    max_budget_usd=args.max_budget_usd,
                    acknowledge_paid_repeat_cap_usd=args.acknowledge_paid_repeat_cap_usd,
                    gateway_reserve_config=args.gateway_reserve_config,
                    timeout_seconds=args.timeout_seconds,
                    state_root=args.state_root,
                )
            elif args.command == "run-compounding":
                result = run_compounding(
                    run_id=args.run_id,
                    admission=args.admission,
                    attempts=(
                        args.attempts if args.attempts is not None else 1 if args.admission else 10
                    ),
                    source=args.source,
                    revision=args.revision,
                    certification_fixture=args.certification_fixture,
                    artifact_manifest=args.artifact_manifest,
                    model=args.model,
                    max_turns=args.max_turns,
                    max_budget_usd=args.max_budget_usd,
                    acknowledge_paid_cap_usd=args.acknowledge_paid_cap_usd,
                    gateway_reserve_config=args.gateway_reserve_config,
                    timeout_seconds=args.timeout_seconds,
                    state_root=args.state_root,
                )
            elif args.command == "run-compounding-batch":
                result = run_compounding_batch(
                    cohort_id=args.cohort_id,
                    attempt_index=args.attempt_index,
                    source=args.source,
                    revision=args.revision,
                    certification_fixture=args.certification_fixture,
                    artifact_manifest=args.artifact_manifest,
                    model=args.model,
                    max_turns=args.max_turns,
                    max_budget_usd=args.max_budget_usd,
                    acknowledge_paid_cap_usd=args.acknowledge_paid_cap_usd,
                    gateway_reserve_config=args.gateway_reserve_config,
                    timeout_seconds=args.timeout_seconds,
                    state_root=args.state_root,
                )
            elif args.command == "run-k5-admission":
                result = run_k5_pair(
                    run_id=args.run_id,
                    admission=True,
                    source=args.source,
                    chain_manifest=args.chain_manifest,
                    certification_fixture=args.certification_fixture,
                    artifact_manifest=args.artifact_manifest,
                    acknowledge_paid_cap_usd=args.acknowledge_paid_cap_usd,
                    gateway_reserve_config=args.gateway_reserve_config,
                    state_root=args.state_root,
                )
            elif args.command == "run-k5-batch":
                result = run_k5_batch(
                    cohort_id=args.cohort_id,
                    attempt_index=args.attempt_index,
                    source=args.source,
                    acknowledge_paid_cap_usd=args.acknowledge_paid_cap_usd,
                    acknowledge_cohort_cap_usd=args.acknowledge_cohort_cap_usd,
                    chain_manifest=args.chain_manifest,
                    certification_fixture=args.certification_fixture,
                    artifact_manifest=args.artifact_manifest,
                    gateway_reserve_config=args.gateway_reserve_config,
                    state_root=args.state_root,
                )
            elif args.command == "run-continuity-admission":
                result = run_continuity_pair(
                    run_id=args.run_id,
                    admission=True,
                    source=args.source,
                    chain_manifest=args.chain_manifest,
                    certification_fixture=args.certification_fixture,
                    artifact_manifest=args.artifact_manifest,
                    acknowledge_paid_cap_usd=args.acknowledge_paid_cap_usd,
                    gateway_reserve_config=args.gateway_reserve_config,
                    state_root=args.state_root,
                )
            elif args.command == "run-continuity-batch":
                result = run_continuity_batch(
                    cohort_id=args.cohort_id,
                    attempt_index=args.attempt_index,
                    source=args.source,
                    acknowledge_paid_cap_usd=args.acknowledge_paid_cap_usd,
                    acknowledge_cohort_cap_usd=args.acknowledge_cohort_cap_usd,
                    chain_manifest=args.chain_manifest,
                    certification_fixture=args.certification_fixture,
                    artifact_manifest=args.artifact_manifest,
                    gateway_reserve_config=args.gateway_reserve_config,
                    state_root=args.state_root,
                )
            elif args.command == "run-suite":
                result = run_suite(
                    suite_id=args.suite_id,
                    dataset=args.dataset,
                    tasks=args.tasks,
                    control_condition=args.control_condition,
                    treatment_condition=args.treatment_condition,
                    non_inferiority_margin=args.non_inferiority_margin,
                    source=args.source,
                    revision=args.revision,
                    artifact_manifest=args.artifact_manifest,
                    model=args.model,
                    max_turns=args.max_turns,
                    max_budget_usd=args.max_budget_usd,
                    acknowledge_paid_suite_cap_usd=args.acknowledge_paid_suite_cap_usd,
                    gateway_reserve_config=args.gateway_reserve_config,
                    timeout_seconds=args.timeout_seconds,
                    state_root=args.state_root,
                )
            elif args.command == "inspect":
                result = inspect_pair(args.pair_id, state_root=args.state_root)
            elif args.command == "record-billing":
                result = record_billing(
                    args.pair_id, args.gateway_total_usd, state_root=args.state_root
                )
            elif args.command == "export-result":
                result = export_result(
                    args.run_id,
                    state_root=args.state_root,
                    results_root=args.results_root,
                    overwrite=args.overwrite,
                )
            elif args.command == "report":
                result = report_results(results_root=args.results_root, output=args.output)
            else:
                result = clean_pair(
                    args.pair_id,
                    delete_work=args.delete_work,
                    state_root=args.state_root,
                )
    except (OracleError, OracleInterrupted, KeyboardInterrupt, OSError, ValueError) as error:
        message = str(error) or "runner interrupted by a termination signal"
        print(json.dumps({"ok": False, "error": message[:500]}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
