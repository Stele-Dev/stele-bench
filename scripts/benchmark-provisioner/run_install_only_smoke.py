#!/usr/bin/env python3
"""Run the two install-only Harbor smoke conditions without an agent turn.

This is deliberately private orchestration.  The portable benchmark package owns
the fixture contracts, corpus materialisation, and Harbor task pack; this file
only joins those public pieces to the schema-aware local provisioner.  It never
builds an integration artifact and never calls an agent's ``run`` method.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol

# Keep this tool runnable directly from the private monorepo checkout without
# making the private provisioner a dependency of the publishable package.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC_SOURCE = _REPOSITORY_ROOT / "eval" / "stele-bench" / "src"
if os.fspath(_PUBLIC_SOURCE) not in sys.path:
    sys.path.insert(0, os.fspath(_PUBLIC_SOURCE))

import model_provider  # noqa: E402

from stele_bench.challenges import LoadedChallenge, select_challenges  # noqa: E402
from stele_bench.contracts import GraphArm, RunIdentity  # noqa: E402
from stele_bench.corpus import (  # noqa: E402
    MaterializedCorpus,
    audit_effective_context,
    materialize_pinned_tree,
)
from stele_bench.fixtures import load_fixture  # noqa: E402
from stele_bench.harbor_pack import (  # noqa: E402
    FaithfulAgentConfig,
    HarborTaskPack,
    build_install_only_argv,
    generate_harbor_task_pack,
)
from stele_bench.preflight import ActiveProjectBinding  # noqa: E402
from stele_bench.provisioning import ExternalCommandProvisioner, Provisioner  # noqa: E402

_MAX_COMMAND_OUTPUT = 64 * 1024
_MAX_PREFLIGHT_BYTES = 256 * 1024
_MAX_PREFLIGHT_FILES = 32
_MAX_DIAGNOSTIC_TEXT = 16 * 1024
_CHILD_ENV_ALLOWLIST = (
    "APPDATA",
    "COMSPEC",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "XDG_RUNTIME_DIR",
)
# Every scored arm caps one model turn at this many output tokens.
AGENT_MAX_OUTPUT_TOKENS = "2048"

_PUBLIC_CONFIG_ENV_ALLOWLIST = (
    "STELE_BENCH_ATTEMPT_ROOT",
    "STELE_BENCH_DB_CONTAINER",
    "STELE_BENCH_DB_NAME",
    "STELE_BENCH_DB_USER",
    "STELE_BENCH_MODEL_PROVIDER",
    "STELE_BENCH_SUPABASE_ANON_KEY",
    "STELE_BENCH_SUPABASE_URL",
    "STELE_BENCH_TASK_IMAGE",
    "SUPABASE_TEST_ANON_KEY",
    "SUPABASE_TEST_URL",
)


class InstallOnlySmokeError(RuntimeError):
    """A smoke preparation step failed without exposing host-only inputs."""

    def __init__(self, code: str, *, receipt: InstallOnlySmokeReceipt | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.receipt = receipt


class RunnerInterrupted(BaseException):
    """Translate process termination signals into ordinary runner cleanup."""

    def __init__(self, signum: int) -> None:
        super().__init__("runner_interrupted")
        self.signum = signum


class PrivateCall(Protocol):
    def __call__(self, operation: str, payload: Mapping[str, object]) -> dict[str, object]: ...


class HarborCall(Protocol):
    def __call__(self, argv: tuple[str, ...]) -> CommandObservation: ...


class PackCall(Protocol):
    def __call__(
        self,
        corpus: MaterializedCorpus,
        destination: Path,
        faithful: FaithfulAgentConfig | None = None,
        *,
        challenges: tuple[LoadedChallenge, ...],
    ) -> HarborTaskPack: ...


@dataclass(frozen=True)
class CommandObservation:
    """Bounded process evidence; stdout/stderr are intentionally not retained."""

    exit_code: int
    wall_seconds: float | None = None
    diagnostic_summary: str | None = None
    diagnostic_path: str | None = None


@dataclass(frozen=True)
class ConditionReceipt:
    condition: Literal["no-stele", "faithful"]
    challenge_ids: tuple[str, ...]
    install_only_invoked: bool
    harbor_exit_code: int | None
    structural_preflight_passed: bool | None


@dataclass(frozen=True)
class InstallOnlySmokeReceipt:
    """Portable result for a no-model setup comparison.

    Host paths, credentials, command output, and project identifiers never
    appear here.  This is intentionally not an attempt result: no task was run
    and no provider token or cost claim is meaningful.
    """

    schema_version: int
    mode: Literal["install-only"]
    source_commit: str
    source_tree_sha256: str
    fixture_id: str
    graph_arm: Literal["faithful"]
    integration_artifact: dict[str, str | None]
    conditions: tuple[ConditionReceipt, ...]
    restore_verified: bool
    snapshot_verified: bool
    cleanup_completed: bool
    access_binding_verified: bool
    error: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    harbor_diagnostics: tuple[dict[str, str | None], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class InstallOnlySmokeInputs:
    repository: Path
    revision: str
    fixture_path: Path
    provisioner_executable: Path
    attempt_root: Path
    work_root: Path
    run_id: str
    artifact_path: Path | None = None
    artifact_sha256: str | None = None
    artifact_source_kind: Literal["release", "local-dirty-worktree"] | None = None
    backend_url: str | None = None
    backend_anon_key: str | None = None
    artifact_release_version: str | None = None
    harbor_executable: str = "harbor"
    timeout_seconds: float = 600.0
    conditions: tuple[Literal["no-stele", "faithful"], ...] = ("no-stele", "faithful")
    challenge_ids: tuple[str, ...] = ("service-readiness",)
    # Applies to the faithful condition. "off" performs the `faithful-norag`
    # strip and its verification, which is the whole point of running it here:
    # this path spends no model tokens, so it is the cheap way to prove the
    # manipulation works on the real image rather than on a host-shaped fake.
    auto_rag_injection: Literal["on", "off"] = "on"

    def __post_init__(self) -> None:
        if not self.run_id or any(char.isspace() for char in self.run_id):
            raise ValueError("run_id must be a non-blank identifier without whitespace")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not self.conditions:
            raise ValueError("select at least one condition")
        if len(set(self.conditions)) != len(self.conditions):
            raise ValueError("conditions must not repeat")
        if self.auto_rag_injection not in ("on", "off"):
            raise ValueError("auto_rag_injection must be on or off")
        if not self.challenge_ids:
            raise ValueError("select at least one challenge")
        if len(set(self.challenge_ids)) != len(self.challenge_ids):
            raise ValueError("challenges must not repeat")
        if any(
            not challenge_id or challenge_id != challenge_id.strip()
            for challenge_id in self.challenge_ids
        ):
            raise ValueError("challenge ids must be non-blank and have no surrounding whitespace")
        if "faithful" in self.conditions:
            if (
                self.artifact_path is None
                or self.artifact_sha256 is None
                or self.artifact_source_kind is None
                or self.backend_url is None
                or self.backend_anon_key is None
            ):
                raise ValueError("faithful requires artifact and backend inputs")
            if self.artifact_source_kind == "release" and not self.artifact_release_version:
                raise ValueError("artifact_release_version is required for a release artifact")
            if (
                self.artifact_source_kind == "local-dirty-worktree"
                and self.artifact_release_version
            ):
                raise ValueError("local artifacts must not set artifact_release_version")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _child_environment(*, include_gateway: bool = False) -> dict[str, str]:
    """Build the explicit environment passed to Harbor and its descendants."""

    environment = {
        name: os.environ[name]
        for name in (*_CHILD_ENV_ALLOWLIST, *_PUBLIC_CONFIG_ENV_ALLOWLIST)
        if name in os.environ
    }
    environment["PYTHONPATH"] = os.fspath(_PUBLIC_SOURCE)
    if include_gateway:
        # "Gateway" here means "the model endpoint": the selected provider's key
        # and base URL, so Harbor can expand the agent's credential placeholder.
        environment.update(model_provider.harbor_process_env())
        environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = AGENT_MAX_OUTPUT_TOKENS
    return environment


@contextmanager
def _scoped_child_environment(*, include_gateway: bool = False) -> object:
    """Temporarily scope libraries that do not accept an explicit child env."""

    previous = dict(os.environ)
    child = _child_environment(include_gateway=include_gateway)
    os.environ.clear()
    os.environ.update(child)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _redact_diagnostic(text: str, environment: Mapping[str, str]) -> str:
    for name, value in environment.items():
        if value and any(marker in name for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    return text[-_MAX_DIAGNOSTIC_TEXT:]


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically persist a private JSON receipt in the destination directory."""

    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.resolve(strict=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(dict(payload), output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _write_harbor_diagnostic(
    path: Path,
    *,
    exit_code: int | None,
    stdout: bytes,
    stderr: bytes,
    environment: Mapping[str, str],
    timed_out: bool = False,
) -> CommandObservation:
    elapsed_summary = "timeout" if timed_out else f"exit={exit_code}"
    summary = f"{elapsed_summary}; stdout_bytes={len(stdout)}; stderr_bytes={len(stderr)}"
    payload = {
        "schema_version": 1,
        "summary": summary,
        "stdout_tail": _redact_diagnostic(stdout.decode("utf-8", "replace"), environment),
        "stderr_tail": _redact_diagnostic(stderr.decode("utf-8", "replace"), environment),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    _atomic_write_json(path, payload)
    return CommandObservation(
        exit_code or 0,
        diagnostic_summary=summary,
        diagnostic_path=os.fspath(path),
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Request termination of the managed child group and always reap it."""

    if process.poll() is not None:
        process.wait()
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        process.wait()


@contextmanager
def _controlled_termination_signals() -> object:
    """Raise inside the runner on SIGTERM/SIGHUP so its finally blocks execute."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    watched = tuple(
        candidate
        for candidate in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None))
        if candidate is not None
    )
    previous = {candidate: signal.getsignal(candidate) for candidate in watched}

    def interrupt(signum: int, _frame: object) -> None:
        raise RunnerInterrupted(signum)

    try:
        for candidate in watched:
            signal.signal(candidate, interrupt)
        yield
    finally:
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)


def _failure_details(error: BaseException) -> tuple[str, str]:
    if isinstance(error, RunnerInterrupted):
        return "runner_interrupted", "runner interrupted by a termination signal"
    if isinstance(error, InstallOnlySmokeError) and re.fullmatch(r"[a-z0-9_]{1,96}", error.code):
        return error.code, error.code.replace("_", " ")
    if isinstance(error, ValueError):
        return "invalid_arguments", "runner arguments are invalid"
    if isinstance(error, OSError):
        return "operating_system_error", "runner could not access a required local resource"
    return "unexpected_error", "runner failed unexpectedly"


def _recover_harbor_diagnostics(
    run_directory: Path, existing: list[dict[str, str | None]]
) -> tuple[dict[str, str | None], ...]:
    known = {item.get("path") for item in existing}
    recovered = list(existing)
    for path in sorted(run_directory.glob("harbor*.diagnostic.json")):
        if os.fspath(path) in known:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            summary = value.get("summary") if isinstance(value, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            summary = "diagnostic unavailable"
        recovered.append(
            {
                "condition": None,
                "summary": summary if isinstance(summary, str) else None,
                "path": os.fspath(path),
            }
        )
    return tuple(recovered)


def _identity(run_id: str, condition: str) -> RunIdentity:
    return RunIdentity(
        run_id=run_id,
        experiment_id="install-only-smoke",
        condition_id=condition,
        challenge_id="install-only-preflight",
        attempt_id=f"{run_id}-{condition}",
        attempt_index=0,
    )


def _attempt_directory(attempt_root: Path, run_id: str) -> Path:
    return attempt_root.resolve() / hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _private_command(
    executable: Path,
    operation: str,
    payload: Mapping[str, object],
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    """Call a private operation with one bounded JSON request and response."""

    request = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()
    try:
        completed = subprocess.run(
            (os.fspath(executable), operation),
            input=request,
            capture_output=True,
            env=_child_environment(),
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallOnlySmokeError(f"private_{operation}_unavailable") from error
    if len(completed.stdout) > _MAX_COMMAND_OUTPUT or len(completed.stderr) > _MAX_COMMAND_OUTPUT:
        raise InstallOnlySmokeError(f"private_{operation}_output_exceeded_limit")
    if completed.returncode:
        raise InstallOnlySmokeError(f"private_{operation}_failed")
    try:
        response = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallOnlySmokeError(f"private_{operation}_invalid_response") from error
    if not isinstance(response, dict):
        raise InstallOnlySmokeError(f"private_{operation}_invalid_response")
    return response


# Harbor sets CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 on every Claude Code
# run. With it (or DISABLE_TELEMETRY) set, Claude Code 2.1.296 silently drops
# every MCP tool whose input schema has a top-level anyOf: 12 of Stele's 17
# tools, `organize` among them, so no arm could activate recall. An arm must run
# the client a default user runs, so both arms clear it. Harbor applies the agent
# env after its own, and only the empty string counts as unset ("0" does not).
# The two narrower switches keep the pinned CLI version and the container quiet;
# neither changes which tools load.
AGENT_CLIENT_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_ERROR_REPORTING": "1",
}


def configure_agent_job(
    path: Path,
    *,
    model: str,
    max_turns: int,
    max_budget_usd: str,
    gateway_url: str,
    semantic_endpoint: str | None = None,
) -> None:
    """Pin the model, the turn and money caps, and the model-call credential.

    Shared by every scored runner so an arm cannot be billed differently
    depending on which run shape launched it. The credential follows the
    selected model provider (`model_provider.current()`): the Gateway key, or
    the first-party Anthropic key. It is written as a literal `${...}`
    placeholder for Harbor to expand at run time — the secret never enters a
    config file on disk, which is what lets the file be kept as evidence.
    """

    data = json.loads(path.read_text(encoding="utf-8"))
    agents = data.get("agents")
    if not isinstance(agents, list) or len(agents) != 1 or not isinstance(agents[0], dict):
        raise InstallOnlySmokeError("configured_job_has_unexpected_agent_shape")
    agent = agents[0]
    kwargs = agent.setdefault("kwargs", {})
    env = agent.setdefault("env", {})
    if not isinstance(kwargs, dict) or not isinstance(env, dict):
        raise InstallOnlySmokeError("configured_job_has_unexpected_agent_shape")
    try:
        agent["model_name"] = model_provider.harbor_model_name(model)
    except model_provider.ModelProviderError as error:
        raise InstallOnlySmokeError("configured_model_is_not_available_on_provider") from error
    kwargs.update(
        {
            "max_turns": max_turns,
            "max_budget_usd": max_budget_usd,
            "permission_mode": "bypassPermissions",
        }
    )
    agent_env = model_provider.harbor_agent_env()
    if model_provider.current() == model_provider.GATEWAY:
        agent_env["ANTHROPIC_BASE_URL"] = gateway_url
    # The output cap is deliberately NOT set here. Harbor treats every agent env
    # key containing TOKEN as a secret and replaces its value throughout the
    # retained evidence, so "2048" became "[REDACTED]" wherever a turn produced
    # exactly 2048 tokens, and the trajectory stopped being JSON. Harbor reads
    # the same variable from its own process environment (_child_environment).
    env.pop("CLAUDE_CODE_MAX_OUTPUT_TOKENS", None)
    env.update(agent_env)
    env.update(AGENT_CLIENT_ENV)
    if semantic_endpoint is not None:
        env["STELE_SEMANTIC_ENDPOINT"] = semantic_endpoint
    data["n_attempts"] = 1
    data["n_concurrent_trials"] = 1
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def scored_harbor_argv(
    config: Path, jobs_root: Path, *, harbor_executable: str
) -> tuple[str, ...]:
    """The one scored Harbor invocation: a single attempt, no retries, no fan-out."""

    return (
        harbor_executable,
        "run",
        "--config",
        os.fspath(config.absolute()),
        "--jobs-dir",
        os.fspath(jobs_root.absolute()),
        "--n-attempts",
        "1",
        "--n-concurrent",
        "1",
        "--max-retries",
        "0",
        "--yes",
    )


def _harbor_command(
    argv: tuple[str, ...],
    *,
    timeout_seconds: float,
    diagnostic_path: Path | None = None,
    include_gateway: bool = False,
) -> CommandObservation:
    """Execute exactly Harbor's setup-only mode; never accept a run-capable argv."""

    if "--install-only" not in argv or "run" not in argv:
        raise InstallOnlySmokeError("harbor_command_is_not_install_only")
    forbidden = {"--model", "--agent", "--resume-trajectory", "--load-trajectory"}
    if any(argument in forbidden for argument in argv):
        raise InstallOnlySmokeError("harbor_command_has_agent_run_options")
    return _managed_harbor_command(
        argv,
        timeout_seconds=timeout_seconds,
        diagnostic_path=diagnostic_path,
        include_gateway=include_gateway,
    )


def _managed_harbor_command(
    argv: tuple[str, ...],
    *,
    timeout_seconds: float,
    diagnostic_path: Path | None = None,
    include_gateway: bool = False,
) -> CommandObservation:
    """Run Harbor in an isolated process group with bounded, private diagnostics."""

    environment = _child_environment(include_gateway=include_gateway)
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=os.name == "posix",
        )
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        assert process is not None
        _terminate_process_group(process)
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        if diagnostic_path is not None:
            _write_harbor_diagnostic(
                diagnostic_path,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                environment=environment,
                timed_out=True,
            )
        raise InstallOnlySmokeError("harbor_install_only_timeout") from error
    except BaseException as error:
        if process is not None:
            _terminate_process_group(process)
        if diagnostic_path is not None:
            _write_harbor_diagnostic(
                diagnostic_path,
                exit_code=process.returncode if process is not None else None,
                stdout=b"",
                stderr=b"",
                environment=environment,
            )
        if isinstance(error, (RunnerInterrupted, KeyboardInterrupt)):
            raise
        raise InstallOnlySmokeError("harbor_install_only_unavailable") from error
    diagnostic: CommandObservation | None = None
    if diagnostic_path is not None:
        diagnostic = _write_harbor_diagnostic(
            diagnostic_path,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            environment=environment,
        )
    if len(stdout) > _MAX_COMMAND_OUTPUT or len(stderr) > _MAX_COMMAND_OUTPUT:
        raise InstallOnlySmokeError("harbor_install_only_output_exceeded_limit")
    observation = CommandObservation(
        exit_code=process.returncode,
        wall_seconds=time.monotonic() - started,
    )
    if diagnostic is not None:
        observation = CommandObservation(
            exit_code=process.returncode,
            wall_seconds=observation.wall_seconds,
            diagnostic_summary=diagnostic.diagnostic_summary,
            diagnostic_path=diagnostic.diagnostic_path,
        )
    return observation


def _read_binding(path: Path) -> ActiveProjectBinding:
    try:
        initial = os.lstat(path)
        if stat.S_ISLNK(initial.st_mode):
            raise InstallOnlySmokeError("project_binding_is_symlink")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except InstallOnlySmokeError:
        raise
    except OSError as error:
        raise InstallOnlySmokeError("project_binding_unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (initial.st_dev, initial.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise InstallOnlySmokeError("project_binding_changed_while_opening")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise InstallOnlySmokeError("project_binding_is_not_private_regular_file")
        if metadata.st_size > _MAX_PREFLIGHT_BYTES:
            raise InstallOnlySmokeError("project_binding_exceeds_limit")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size:
            raise InstallOnlySmokeError("project_binding_read_incomplete")
    finally:
        os.close(descriptor)
    try:
        return ActiveProjectBinding.from_json_bytes(payload)
    except ValueError as error:
        raise InstallOnlySmokeError("project_binding_invalid") from error


def _configured_preflights_passed(
    jobs_root: Path,
    job_name: str,
    artifact_sha256: str,
    *,
    expected_count: int,
) -> bool:
    """Read only the small structural report emitted by configured setup.

    Harbor owns the exact trial folder naming, so search below the configured
    job only.  The report count must exactly match the generated task count;
    every report must independently prove the same artifact and binding checks.
    """

    if expected_count <= 0 or not job_name:
        return False
    job_directory = jobs_root / job_name
    if not job_directory.is_dir():
        return False
    matches: list[Path] = []
    for candidate in job_directory.rglob("preflight.json"):
        if len(matches) >= _MAX_PREFLIGHT_FILES:
            return False
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(job_directory.resolve(strict=True))
            if candidate.is_symlink() or not candidate.is_file():
                return False
            if candidate.stat().st_size > _MAX_PREFLIGHT_BYTES:
                return False
        except (OSError, ValueError):
            return False
        matches.append(candidate)
    if len(matches) != expected_count:
        return False
    for match in matches:
        try:
            report = json.loads(match.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(report, dict) or not (
            report.get("schema_version") == 1
            and report.get("passed") is True
            and report.get("binding_verified") is True
            and report.get("artifact_sha256") == artifact_sha256
            and report.get("stele_command_resolves_to_artifact") is True
            and report.get("account_authenticated") is True
            and report.get("doctor_checked_with_explicit_slug") is True
            and report.get("lifecycle_firing_verified") is False
            and report.get("managed_permission_precedence_verified") is False
        ):
            return False
    return True


def _safe_work_directory(work_root: Path, run_id: str) -> Path:
    root = work_root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    directory = root / hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:20]
    if directory.exists() or directory.is_symlink():
        raise InstallOnlySmokeError("work_directory_already_exists")
    directory.mkdir(mode=0o700)
    return directory


def _artifact_provenance(inputs: InstallOnlySmokeInputs) -> dict[str, str | None]:
    return {
        "source_kind": inputs.artifact_source_kind,
        "sha256": inputs.artifact_sha256.lower() if inputs.artifact_sha256 else None,
        "release_version": inputs.artifact_release_version,
    }


def _snapshot_matches_fixture(observation: object, fixture: object) -> bool:
    """Check the portable exact-restore claims before deleting the graph."""

    try:
        expected = fixture.graphs[GraphArm.FAITHFUL]
        state = observation.state
        actual = state.canonical_state
        probe_kinds = {
            getattr(getattr(probe, "kind"), "value", getattr(probe, "kind"))
            for probe in observation.probes
        }
        return (
            observation.queue_before_count == observation.queue_after_count
            and observation.project.project_id == state.project_id
            and probe_kinds == {"fts", "semantic", "graph"}
            and actual.sha256 == expected.sha256
            and actual.graph_format_version == expected.graph_format_version
            and actual.backend_compatibility_id == expected.backend_compatibility_id
            and actual.embedding_model_id == expected.embedding_model_id
            and actual.embedding_dimensions == expected.embedding_dimensions
        )
    except (AttributeError, KeyError, TypeError):
        return False


def _run_install_only_smoke_with_attempt_root(
    inputs: InstallOnlySmokeInputs,
    *,
    private_call: PrivateCall | None = None,
    provisioner: Provisioner | None = None,
    harbor_call: HarborCall | None = None,
    materialize: Callable[[Path, str, Path], MaterializedCorpus] = materialize_pinned_tree,
    pack: PackCall = generate_harbor_task_pack,
    challenge_loader: Callable[[tuple[str, ...]], tuple[LoadedChallenge, ...]] = select_challenges,
) -> InstallOnlySmokeReceipt:
    """Prepare selected conditions, snapshot the graph, and destroy private state."""

    fixture_path = inputs.fixture_path.resolve(strict=True)
    fixture = load_fixture(fixture_path)
    fixture_root = fixture_path.parent
    selected_challenges = challenge_loader(inputs.challenge_ids)
    selected_ids = tuple(challenge.challenge_id for challenge in selected_challenges)
    if selected_ids != inputs.challenge_ids:
        raise InstallOnlySmokeError("challenge_selection_did_not_preserve_order")
    faithful_selected = "faithful" in inputs.conditions
    artifact: Path | None = None
    if faithful_selected:
        assert inputs.artifact_path is not None
        assert inputs.artifact_sha256 is not None
        artifact = inputs.artifact_path.resolve(strict=True)
        if not artifact.is_file() or _sha256_file(artifact) != inputs.artifact_sha256.lower():
            raise InstallOnlySmokeError("integration_artifact_digest_does_not_match")
        inputs.attempt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        inputs.attempt_root.chmod(0o700)
    if faithful_selected and private_call is None:
        def default_private_call(
            operation: str, payload: Mapping[str, object]
        ) -> dict[str, object]:
            return _private_command(
                inputs.provisioner_executable.resolve(strict=True),
                operation,
                payload,
                timeout_seconds=inputs.timeout_seconds,
            )

        private_call = default_private_call
    if faithful_selected and provisioner is None:
        provisioner = ExternalCommandProvisioner(
            (os.fspath(inputs.provisioner_executable.resolve(strict=True)),),
            fixture_root,
            timeout_seconds=inputs.timeout_seconds,
        )
    run_directory = _safe_work_directory(inputs.work_root, inputs.run_id)
    jobs_root = run_directory / "jobs"
    if harbor_call is None:
        diagnostic_index = 0

        def default_harbor_call(argv: tuple[str, ...]) -> CommandObservation:
            nonlocal diagnostic_index
            diagnostic_index += 1
            return _harbor_command(
                argv,
                timeout_seconds=inputs.timeout_seconds,
                diagnostic_path=run_directory / f"harbor-{diagnostic_index}.diagnostic.json",
            )

        harbor_call = default_harbor_call
    conditions: list[ConditionReceipt] = []
    project = None
    restored = False
    snapshot_verified = False
    cleanup_completed = False
    access_binding_verified = False
    primary_error: BaseException | None = None
    corpus: MaterializedCorpus | None = None
    harbor_diagnostics: list[dict[str, str | None]] = []

    try:
        corpus = materialize(
            inputs.repository, inputs.revision, run_directory / "corpus" / "project"
        )
        audit_effective_context(corpus)
        attempt_directory = run_directory / "unused-configured-inputs"
        if faithful_selected:
            assert private_call is not None
            assert provisioner is not None
            response = private_call("prepare", {"run_id": inputs.run_id, "harnesses": ["claude"]})
            if response.get("ok") is not True:
                raise InstallOnlySmokeError("private_prepare_invalid_response")
            with _scoped_child_environment():
                restore = asyncio.run(
                    provisioner.restore_with_receipt(
                        fixture, GraphArm.FAITHFUL, _identity(inputs.run_id, "faithful")
                    )
                )
            project = restore.project
            restored = True
            attempt_directory = _attempt_directory(inputs.attempt_root, inputs.run_id)
            _read_binding(attempt_directory / "project.json")
        faithful = None
        if faithful_selected:
            assert artifact is not None
            assert inputs.artifact_sha256 is not None
            assert inputs.artifact_source_kind is not None
            assert inputs.backend_url is not None
            assert inputs.backend_anon_key is not None
            faithful = FaithfulAgentConfig(
                artifact_path=artifact,
                artifact_sha256=inputs.artifact_sha256,
                artifact_source_kind=inputs.artifact_source_kind,
                artifact_release_version=inputs.artifact_release_version,
                credentials_path=attempt_directory / "credentials.json",
                preferences_path=attempt_directory / "preferences.json",
                project_binding_path=attempt_directory / "project.json",
                backend_url=inputs.backend_url,
                backend_anon_key=inputs.backend_anon_key,
                auto_rag_injection=inputs.auto_rag_injection,
            )
        task_pack = pack(
            corpus,
            run_directory / "pack",
            faithful,
            challenges=selected_challenges,
        )
        if len(task_pack.task_directories) != len(selected_challenges):
            raise InstallOnlySmokeError("generated_task_count_does_not_match_selection")

        for condition in inputs.conditions:
            if condition == "no-stele":
                control_argv = build_install_only_argv(
                    task_pack.plain_config, harbor_executable=inputs.harbor_executable
                ) + ("--jobs-dir", os.fspath(jobs_root))
                control = harbor_call(control_argv)
                harbor_diagnostics.append(
                    {
                        "condition": "no-stele",
                        "summary": control.diagnostic_summary,
                        "path": control.diagnostic_path,
                    }
                )
                conditions.append(
                    ConditionReceipt("no-stele", selected_ids, True, control.exit_code, None)
                )
                if control.exit_code:
                    raise InstallOnlySmokeError("control_install_only_failed")
            else:
                if task_pack.configured_config is None or task_pack.configured_job_name is None:
                    raise InstallOnlySmokeError("configured_pack_is_missing")
                configured_argv = build_install_only_argv(
                    task_pack.configured_config, harbor_executable=inputs.harbor_executable
                ) + ("--jobs-dir", os.fspath(jobs_root))
                configured = harbor_call(configured_argv)
                harbor_diagnostics.append(
                    {
                        "condition": "faithful",
                        "summary": configured.diagnostic_summary,
                        "path": configured.diagnostic_path,
                    }
                )
                access_binding_verified = (
                    configured.exit_code == 0
                    and _configured_preflights_passed(
                        jobs_root,
                        task_pack.configured_job_name,
                        inputs.artifact_sha256.lower(),
                        expected_count=len(task_pack.task_directories),
                    )
                )
                conditions.append(
                    ConditionReceipt(
                        "faithful",
                        selected_ids,
                        True,
                        configured.exit_code,
                        access_binding_verified,
                    )
                )
                if configured.exit_code:
                    raise InstallOnlySmokeError("configured_install_only_failed")
                if not access_binding_verified:
                    raise InstallOnlySmokeError("configured_structural_preflight_failed")

        if faithful_selected:
            assert provisioner is not None and project is not None
            with _scoped_child_environment():
                snapshot = asyncio.run(provisioner.snapshot_with_receipt(project))
            snapshot_verified = _snapshot_matches_fixture(snapshot.observation(restore), fixture)
            if not snapshot_verified:
                raise InstallOnlySmokeError("snapshot_mismatch")
    except BaseException as error:
        primary_error = error
    finally:
        if faithful_selected and project is not None:
            try:
                with _scoped_child_environment():
                    asyncio.run(provisioner.destroy(project))
            except BaseException as cleanup_error:
                if primary_error is None:
                    primary_error = cleanup_error
        if faithful_selected:
            assert private_call is not None
            try:
                response = private_call("cleanup", {"run_id": inputs.run_id})
                cleanup_completed = response == {"ok": True}
                if not cleanup_completed and primary_error is None:
                    primary_error = InstallOnlySmokeError("private_cleanup_invalid_response")
            except BaseException as cleanup_error:
                if primary_error is None:
                    primary_error = cleanup_error

    if corpus is None:
        # Materialization is the provenance source.  Do not invent a receipt
        # when we did not establish it.
        raise InstallOnlySmokeError("corpus_materialization_failed") from primary_error
    failure_code, failure_message = (
        _failure_details(primary_error) if primary_error is not None else (None, None)
    )
    receipt = InstallOnlySmokeReceipt(
        schema_version=2,
        mode="install-only",
        source_commit=corpus.source_commit,
        source_tree_sha256=corpus.tree_sha256,
        fixture_id=fixture.fixture_id,
        graph_arm="faithful",
        integration_artifact=_artifact_provenance(inputs),
        conditions=tuple(conditions),
        restore_verified=restored,
        snapshot_verified=snapshot_verified,
        cleanup_completed=cleanup_completed,
        access_binding_verified=access_binding_verified,
        error=failure_code,
        failure_code=failure_code,
        failure_message=failure_message,
        harbor_diagnostics=_recover_harbor_diagnostics(run_directory, harbor_diagnostics),
    )
    if primary_error is not None:
        raise InstallOnlySmokeError("install_only_smoke_failed", receipt=receipt) from primary_error
    return receipt


def run_install_only_smoke(
    inputs: InstallOnlySmokeInputs,
    *,
    private_call: PrivateCall | None = None,
    provisioner: Provisioner | None = None,
    harbor_call: HarborCall | None = None,
    materialize: Callable[[Path, str, Path], MaterializedCorpus] = materialize_pinned_tree,
    pack: PackCall = generate_harbor_task_pack,
    challenge_loader: Callable[[tuple[str, ...]], tuple[LoadedChallenge, ...]] = select_challenges,
) -> InstallOnlySmokeReceipt:
    """Scope private attempt state to child commands and restore the caller's env."""

    if "faithful" not in inputs.conditions:
        return _run_install_only_smoke_with_attempt_root(
            inputs,
            private_call=private_call,
            provisioner=provisioner,
            harbor_call=harbor_call,
            materialize=materialize,
            pack=pack,
            challenge_loader=challenge_loader,
        )

    key = "STELE_BENCH_ATTEMPT_ROOT"
    previous = os.environ.get(key)
    os.environ[key] = os.fspath(inputs.attempt_root.resolve())
    try:
        return _run_install_only_smoke_with_attempt_root(
            inputs,
            private_call=private_call,
            provisioner=provisioner,
            harbor_call=harbor_call,
            materialize=materialize,
            pack=pack,
            challenge_loader=challenge_loader,
        )
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("revision")
    parser.add_argument("fixture", type=Path)
    parser.add_argument(
        "--provisioner",
        type=Path,
        default=_REPOSITORY_ROOT / "scripts/benchmark-provisioner/provision.py",
    )
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--artifact-sha256")
    parser.add_argument(
        "--artifact-source-kind",
        choices=("release", "local-dirty-worktree"),
    )
    parser.add_argument("--artifact-release-version")
    parser.add_argument("--backend-url")
    parser.add_argument("--backend-anon-key")
    parser.add_argument("--harbor", default="harbor")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument(
        "--receipt-out",
        type=Path,
        help="atomically persist the success or failure envelope as private JSON",
    )
    parser.add_argument(
        "--condition",
        action="append",
        choices=("no-stele", "faithful"),
        dest="conditions",
        help="repeat to run only selected setup conditions (default: both)",
    )
    parser.add_argument(
        "--auto-rag-injection",
        choices=("on", "off"),
        default="on",
        help=(
            "'off' strips prompt-time context injection from the faithful "
            "condition and verifies the strip took — the zero-token way to "
            "check the faithful-norag arm on the real container image"
        ),
    )
    parser.add_argument(
        "--challenge",
        action="append",
        dest="challenges",
        help="repeat to select challenges in order (default: service-readiness)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    envelope: dict[str, object]
    try:
        with _controlled_termination_signals():
            receipt = run_install_only_smoke(
                InstallOnlySmokeInputs(
                    repository=args.repository,
                    revision=args.revision,
                    fixture_path=args.fixture,
                    provisioner_executable=args.provisioner,
                    attempt_root=args.attempt_root,
                    work_root=args.work_root,
                    run_id=args.run_id,
                    artifact_path=args.artifact,
                    artifact_sha256=args.artifact_sha256,
                    artifact_source_kind=args.artifact_source_kind,
                    artifact_release_version=args.artifact_release_version,
                    backend_url=args.backend_url,
                    backend_anon_key=args.backend_anon_key,
                    harbor_executable=args.harbor,
                    timeout_seconds=args.timeout_seconds,
                    conditions=tuple(args.conditions or ("no-stele", "faithful")),
                    challenge_ids=tuple(args.challenges or ("service-readiness",)),
                    auto_rag_injection=args.auto_rag_injection,
                )
            )
    except (InstallOnlySmokeError, RunnerInterrupted, OSError, ValueError) as error:
        cause = (
            error.__cause__
            if isinstance(error, InstallOnlySmokeError)
            and error.code == "install_only_smoke_failed"
            else None
        )
        code, message = _failure_details(cause or error)
        retained = error.receipt if isinstance(error, InstallOnlySmokeError) else None
        envelope = {
            "ok": False,
            "failure": {"code": code, "message": message},
            "receipt": retained.as_dict() if retained is not None else None,
        }
        if args.receipt_out is not None:
            _atomic_write_json(args.receipt_out, envelope)
        print(json.dumps(envelope, sort_keys=True))
        return 1
    envelope = {"ok": True, "receipt": receipt.as_dict()}
    if args.receipt_out is not None:
        _atomic_write_json(args.receipt_out, envelope)
    print(json.dumps(envelope, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
