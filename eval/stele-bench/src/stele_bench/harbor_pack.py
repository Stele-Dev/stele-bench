"""Build a local Harbor 0.20 task pack without invoking an agent or model."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from .certification import (
    CertificationError,
    CertificationPolicy,
    RedactionRecord,
    certify_project_tree,
    matched_redaction_pattern,
)
from .challenges import LoadedChallenge, select_challenges
from .contracts import ChallengeManifestV1
from .corpus import (
    BlindnessAuditConfig,
    DestinationError,
    MaterializedCorpus,
    audit_effective_context,
)
from .harbor_entrypoint import HARBOR_AGENT_IMPORT_PATH
from .preflight import AutoRagInjection, ProjectProvisioning

_PROJECT_DIRECTORY = "project"
_PLAIN_CONFIG_NAME = "route-one.json"
_CONFIGURED_CONFIG_NAME = "route-two.json"
_PLAIN_JOB_NAME = "route-one"
_CONFIGURED_JOB_NAME = "route-two"
_DIGEST_LENGTH = 64
_MUTATION_BASELINE_NAME = "mutation-baseline.tsv"
_CHALLENGE_VERIFIER_NAME = "challenge-verifier.sh"
# Running Stele as the memory system legitimately writes its project binding
# (`.stele/`) into the workspace, and certification redacts the corpus's own
# `.stele/` so the agent cannot tell it is inside an instrumented repo. The
# binding therefore reads as a NEW file to a naive mutation audit and would
# force-zero every faithful arm before the real verifier runs. This is Stele's
# own infrastructure, not agent output, and the verifier never reads it, so it
# is excluded from the audit on BOTH sides (baseline + live snapshot).
_MUTATION_IGNORED_PATHS = (".stele",)
# Importing source directly from a packaged Python checkout creates bytecode
# caches inside the workspace. They are interpreter infrastructure, not agent
# output, and the hidden verifier never reads them. Ignore the same runtime
# paths in both the host baseline and the in-container snapshot so ordinary
# imports cannot force-zero an otherwise valid patch.
_MUTATION_IGNORED_COMPONENTS = ("__pycache__",)
_MUTATION_IGNORED_SUFFIXES = (".pyc", ".pyo")
# Escape a shell value for embedding in a JSON string: backslash then doublequote.
# Repo paths carry no control characters, so these two are sufficient in practice.
_JSON_ESCAPE_SED = r's/\\/\\\\/g; s/"/\\"/g'
_DEFAULT_CHALLENGE_IDS = ("service-readiness",)
# The only hosts a benchmark arm may write to. `host.docker.internal` is here
# because that is how a container reaches the host's loopback stack — it is the
# same local Supabase, named from the other side of the network namespace.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})
_ESTABLISH_STEP_NAME = "establish"
_FOLLOW_UP_STEP_NAME = "follow-up"
_REDACTION_RECEIPT_NAME = "context-redaction.json"
HARBOR_TASK_IMAGE = "harbor-local/claude-rust:2.1.220-rust-1.88.0"
# A run may substitute another build of images/claude-rust (for example a newer
# Claude Code that knows a newer model's price). The registered chain manifests
# pin the default, and their runner refuses any override.
TASK_IMAGE_ENV = "STELE_BENCH_TASK_IMAGE"
_TASK_IMAGE_TAG = re.compile(r"^harbor-local/claude-rust:[0-9][0-9A-Za-z.\-]{0,127}$")


def task_image() -> str:
    """The task base image for this run: the pinned default unless overridden."""

    value = os.environ.get(TASK_IMAGE_ENV, "").strip()
    if not value:
        return HARBOR_TASK_IMAGE
    if not _TASK_IMAGE_TAG.fullmatch(value):
        raise ValueError(f"{TASK_IMAGE_ENV} must name a harbor-local/claude-rust:<tag> image")
    return value
_HARBOR_NETWORK_MODE = {"none": "no-network", "public": "public"}
_REVEALING_MARKER = re.compile(
    r"(?<![a-z0-9])(?:benchmark|eval|condition|arm|stele)(?![a-z0-9])",
    re.IGNORECASE,
)


class HarborPackError(RuntimeError):
    """A safe, self-contained Harbor pack could not be generated."""


@dataclass(frozen=True)
class FaithfulAgentConfig:
    """Host-only scalar inputs consumed by the configured Harbor entrypoint."""

    artifact_path: Path
    artifact_sha256: str
    artifact_source_kind: Literal["release", "local-dirty-worktree"]
    credentials_path: Path
    preferences_path: Path
    project_binding_path: Path | None
    backend_url: str
    backend_anon_key: str
    artifact_release_version: str | None = None
    # "off" is the `faithful-norag` arm: identical install, prompt-time context
    # injection removed. See preflight.strip_injection_channel_command.
    auto_rag_injection: AutoRagInjection = "on"
    # "create" makes a project in-container the way a new user does, instead of
    # restoring a frozen graph. Used by the borrowed-task arm, where the thing
    # being measured is what a fresh install costs.
    project_provisioning: ProjectProvisioning = "restore"
    project_name: str = "Workspace"

    def __post_init__(self) -> None:
        digest = self.artifact_sha256.casefold()
        if len(digest) != _DIGEST_LENGTH or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("artifact_sha256 must be a 64-character hexadecimal digest")
        if self.artifact_source_kind == "release":
            if self.artifact_release_version is None or not self.artifact_release_version.strip():
                raise ValueError("a release artifact requires artifact_release_version")
        elif self.artifact_source_kind == "local-dirty-worktree":
            if self.artifact_release_version not in (None, ""):
                raise ValueError("artifact_release_version is valid only for release artifacts")
        else:
            raise ValueError("artifact_source_kind must be release or local-dirty-worktree")
        parsed_backend = urlsplit(self.backend_url)
        try:
            parsed_backend.port
        except ValueError as error:
            raise ValueError("backend_url contains an invalid port") from error
        if (
            parsed_backend.scheme not in {"http", "https"}
            or parsed_backend.hostname is None
            or parsed_backend.username is not None
            or parsed_backend.password is not None
        ):
            raise ValueError("backend_url must be an http or https URL without credentials")
        # A benchmark arm creates projects, writes nodes, and is then destroyed.
        # Pointed at a real backend it would do all of that to somebody's actual
        # graph. The oracle already refuses a non-loopback backend when it reads
        # the local stack's configuration, but the runner accepts `--backend-url`
        # from anyone, so the refusal belongs here too — at the one place every
        # arm passes through, whatever invoked it.
        if parsed_backend.hostname not in _LOOPBACK_HOSTS:
            raise ValueError(
                "backend_url must be a loopback address — a benchmark arm must "
                "never write to a non-local backend"
            )
        if not self.backend_anon_key or any(char.isspace() for char in self.backend_anon_key):
            raise ValueError("backend_anon_key must be a non-blank public client key")
        if self.auto_rag_injection not in ("on", "off"):
            raise ValueError("auto_rag_injection must be on or off")
        if self.project_provisioning not in ("restore", "create"):
            raise ValueError("project_provisioning must be restore or create")
        if self.project_provisioning == "restore" and self.project_binding_path is None:
            raise ValueError("restoring a project requires project_binding_path")
        object.__setattr__(self, "artifact_sha256", digest)


@dataclass(frozen=True)
class HarborTaskPack:
    """Paths produced by :func:`generate_harbor_task_pack`."""

    destination: Path
    task_directories: tuple[Path, ...]
    project_directories: tuple[Path, ...]
    plain_config: Path
    configured_config: Path | None
    plain_job_name: str
    configured_job_name: str | None
    redactions: tuple[RedactionRecord, ...] = ()

    @property
    def configs(self) -> tuple[Path, ...]:
        """Return all generated JobConfigs in route order."""

        if self.configured_config is None:
            return (self.plain_config,)
        return (self.plain_config, self.configured_config)

    @property
    def task_directory(self) -> Path:
        """Return the sole task directory for legacy one-task callers."""

        if len(self.task_directories) != 1:
            raise HarborPackError("task_directory is available only for a one-task pack")
        return self.task_directories[0]

    @property
    def project_directory(self) -> Path:
        """Return the sole project directory for legacy one-task callers."""

        if len(self.project_directories) != 1:
            raise HarborPackError("project_directory is available only for a one-task pack")
        return self.project_directories[0]


def generate_harbor_task_pack(
    corpus: MaterializedCorpus,
    destination: Path,
    faithful: FaithfulAgentConfig | None = None,
    *,
    challenges: Iterable[LoadedChallenge] | None = None,
    audit_config: BlindnessAuditConfig = BlindnessAuditConfig(),
    certification_policy: CertificationPolicy = CertificationPolicy(),
    redact_auto_loaded_context: bool = True,
) -> HarborTaskPack:
    """Write selected local tasks and two route-isolated Harbor JobConfig files.

    ``destination`` may be absent or an empty real directory.  The corpus is
    copied independently into every task from its materialized manifest;
    unlisted files and redirected paths fail closed.  Credentials and artifact
    locations are written only to the optional configured job file, which
    remains outside every task and build context.  A stock-agent pack requires
    no configured-agent credentials or artifact inputs.

    Auto-loaded harness context (``CLAUDE.md``, ``.claude/``, ``.cursorrules``
    …) and stray metadata are dropped while copying, identically for every
    task, and recorded in a runner-owned receipt.  The copied tree is then
    certified and the pack fails closed if anything forbidden survives.  Pass
    ``redact_auto_loaded_context=False`` only to prove that failure happens.
    """

    selected = _normalize_challenges(challenges)
    destination = destination.absolute()
    destination_existed = _prepare_destination(destination)
    try:
        created = tuple(
            _create_task(
                corpus,
                destination,
                challenge,
                audit_config,
                certification_policy,
                redact_auto_loaded_context,
            )
            for challenge in selected
        )
        task_directories = tuple(task for task, _project, _records in created)
        project_directories = tuple(project for _task, project, _records in created)
        redactions = created[0][2] if created else ()

        plain_config = destination / _PLAIN_CONFIG_NAME
        task_paths = tuple(os.fspath(path) for path in task_directories)
        resume_trajectory = any(challenge.manifest.is_multi_step for challenge in selected)
        _write_json(plain_config, _plain_job_config(task_paths, resume_trajectory))
        configured_config: Path | None = None
        configured_job_name: str | None = None
        if faithful is not None:
            configured_config = destination / _CONFIGURED_CONFIG_NAME
            configured_job_name = _CONFIGURED_JOB_NAME
            _write_json(
                configured_config,
                _configured_job_config(task_paths, faithful, resume_trajectory),
            )

        return HarborTaskPack(
            destination=destination,
            task_directories=task_directories,
            project_directories=project_directories,
            plain_config=plain_config,
            configured_config=configured_config,
            plain_job_name=_PLAIN_JOB_NAME,
            configured_job_name=configured_job_name,
            redactions=redactions,
        )
    except BaseException:
        _clear_directory(destination)
        if not destination_existed:
            destination.rmdir()
        raise


def _normalize_challenges(
    challenges: Iterable[LoadedChallenge] | None,
) -> tuple[LoadedChallenge, ...]:
    selected = (
        select_challenges(_DEFAULT_CHALLENGE_IDS) if challenges is None else tuple(challenges)
    )
    if not selected:
        raise HarborPackError("at least one challenge is required")
    if any(not isinstance(challenge, LoadedChallenge) for challenge in selected):
        raise TypeError("challenges must contain only LoadedChallenge values")
    ids = tuple(challenge.challenge_id for challenge in selected)
    duplicates = sorted(challenge_id for challenge_id in set(ids) if ids.count(challenge_id) > 1)
    if duplicates:
        raise HarborPackError(f"duplicate challenge ids: {', '.join(duplicates)}")
    for challenge in selected:
        _require_neutral_manifest(challenge)
    return selected


def _require_neutral_manifest(challenge: LoadedChallenge) -> None:
    manifest = challenge.manifest
    visible = "\n".join(
        (
            manifest.challenge_id,
            manifest.title,
            manifest.description,
            manifest.prompt,
            manifest.follow_up_prompt or "",
        )
    )
    match = _REVEALING_MARKER.search(visible)
    if match is not None:
        raise HarborPackError(
            f"agent-visible challenge metadata contains a revealing marker: {match.group(0)!r}"
        )


def _create_task(
    corpus: MaterializedCorpus,
    destination: Path,
    challenge: LoadedChallenge,
    audit_config: BlindnessAuditConfig,
    certification_policy: CertificationPolicy,
    redact: bool,
) -> tuple[Path, Path, tuple[RedactionRecord, ...]]:
    task_directory = destination / challenge.challenge_id
    environment_directory = task_directory / "environment"
    project_directory = environment_directory / _PROJECT_DIRECTORY
    tests_directory = task_directory / "tests"
    project_directory.mkdir(parents=True)
    tests_directory.mkdir()

    records = _copy_manifest(corpus, project_directory, certification_policy, redact)
    effective_audit = _audit_config_for_corpus(audit_config, corpus)
    audit_effective_context(project_directory, effective_audit)
    _certify_project_directory(
        project_directory,
        challenge,
        certification_policy,
        allow_git_history=corpus.history is not None,
    )
    _write_json(
        tests_directory / _REDACTION_RECEIPT_NAME,
        {
            "schema_version": 1,
            "source_commit": corpus.source_commit,
            "redactions": [record.as_dict() for record in records],
        },
    )

    if challenge.manifest.is_multi_step:
        _write_step_instructions(task_directory, challenge.manifest)
    else:
        _write_text(task_directory / "instruction.md", challenge.manifest.prompt)
    _write_text(task_directory / "task.toml", _task_toml(challenge))
    _write_text(
        environment_directory / "Dockerfile",
        _dockerfile(challenge.manifest.environment.prebuild_argv),
    )
    missing_output_ancestors = _missing_output_ancestors(
        project_directory, challenge.manifest.allowed_output_paths
    )
    _write_mutation_baseline(
        project_directory,
        tests_directory / _MUTATION_BASELINE_NAME,
        challenge.manifest.allowed_output_paths,
        ignored_paths=(".git",) if corpus.history is not None else (),
    )
    _copy_verifier(challenge, tests_directory / _CHALLENGE_VERIFIER_NAME)
    _write_text(
        tests_directory / "test.sh",
        _verifier_wrapper(
            challenge.manifest.allowed_output_paths,
            missing_output_ancestors,
            ignored_paths=(".git",) if corpus.history is not None else (),
        ),
        mode=0o755,
    )

    # The verifier is a sibling of the Docker build context and Harbor uploads
    # it separately.  Re-audit after adding all agent-visible build inputs.
    audit_effective_context(environment_directory, effective_audit)
    audit_effective_context(task_directory, effective_audit)
    return task_directory, project_directory, records


def _certify_project_directory(
    project_directory: Path,
    challenge: LoadedChallenge,
    policy: CertificationPolicy,
    *,
    allow_git_history: bool = False,
) -> None:
    """Fail closed if anything forbidden reached the evaluated project tree."""

    findings, _probed = certify_project_tree(
        project_directory,
        solution_markers=challenge.manifest.solution_markers,
        policy=policy,
        ignored_top_level_components=(".git",) if allow_git_history else (),
    )
    if findings:
        raise CertificationError(findings)


def build_install_only_argv(
    config_path: Path, *, harbor_executable: str = "harbor"
) -> tuple[str, ...]:
    """Return the no-shell Harbor install preflight command."""

    _require_config_path(config_path)
    return (
        harbor_executable,
        "run",
        "--config",
        os.fspath(config_path.absolute()),
        "--install-only",
        "--n-concurrent",
        "1",
        "--yes",
    )


def build_print_config_argv(
    config_path: Path, *, harbor_executable: str = "harbor"
) -> tuple[str, ...]:
    """Return Harbor's schema-only resolved-config command."""

    _require_config_path(config_path)
    return (
        harbor_executable,
        "run",
        "--config",
        os.fspath(config_path.absolute()),
        "--print-config",
    )


def _prepare_destination(destination: Path) -> bool:
    if destination.is_symlink():
        raise DestinationError(f"destination must not be a symlink: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise DestinationError(f"destination exists and is not a directory: {destination}")
        if any(destination.iterdir()):
            raise DestinationError(f"destination must be empty: {destination}")
        return True
    destination.mkdir(parents=True, exist_ok=False)
    return False


def _clear_directory(directory: Path) -> None:
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _copy_manifest(
    corpus: MaterializedCorpus,
    destination: Path,
    policy: CertificationPolicy,
    redact: bool,
) -> tuple[RedactionRecord, ...]:
    # This validates both the declared root and the caller-provided blindness
    # contract before any bytes are copied.
    audit_effective_context(corpus, raise_on_violation=False)
    source = corpus.destination.resolve(strict=True)
    declared_paths = {entry.path for entry in corpus.entries}
    actual_paths = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if not (corpus.history is not None and path.relative_to(source).parts[:1] == (".git",))
        if not path.is_dir() or path.is_symlink()
    }
    if actual_paths != declared_paths:
        raise HarborPackError("materialized corpus no longer matches its manifest paths")

    seen: set[str] = set()
    records: list[RedactionRecord] = []
    for entry in corpus.entries:
        relative = _safe_relative_path(entry.path)
        if entry.path in seen:
            raise HarborPackError(f"duplicate corpus manifest path: {entry.path!r}")
        seen.add(entry.path)
        matched = matched_redaction_pattern(entry.path, policy)
        if matched is not None and redact:
            pattern, kind = matched
            records.append(RedactionRecord(entry.path, entry.sha256, kind, pattern))
            continue
        source_path = source.joinpath(*relative.parts)
        target_path = destination.joinpath(*relative.parts)
        _ensure_real_parents(source, source_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = source_path.lstat()

        if entry.kind == "file":
            if not stat.S_ISREG(metadata.st_mode):
                raise HarborPackError(f"corpus entry is not a regular file: {entry.path}")
            digest = hashlib.sha256()
            with source_path.open("rb") as input_file, target_path.open("xb") as output_file:
                for block in iter(lambda: input_file.read(1024 * 1024), b""):
                    digest.update(block)
                    output_file.write(block)
            if digest.hexdigest() != entry.sha256:
                raise HarborPackError(f"corpus file digest changed: {entry.path}")
            target_path.chmod(entry.mode)
            if target_path.stat().st_mode & 0o777 != entry.mode:
                raise HarborPackError(f"could not preserve corpus mode: {entry.path}")
        elif entry.kind == "symlink":
            if not stat.S_ISLNK(metadata.st_mode):
                raise HarborPackError(f"corpus entry is not a symlink: {entry.path}")
            link_target = os.readlink(source_path)
            _require_contained_link(source, source_path, link_target)
            digest = hashlib.sha256(f"symlink\0{link_target}".encode()).hexdigest()
            if digest != entry.sha256:
                raise HarborPackError(f"corpus symlink digest changed: {entry.path}")
            target_path.symlink_to(link_target)
            _require_contained_link(destination, target_path, link_target)
        else:
            raise HarborPackError(f"unsupported corpus entry kind: {entry.kind!r}")
    if corpus.history is not None:
        _copy_git_history(source / ".git", destination / ".git")
    return tuple(records)


def _audit_config_for_corpus(
    config: BlindnessAuditConfig, corpus: MaterializedCorpus
) -> BlindnessAuditConfig:
    if corpus.history is None:
        return config
    return replace(
        config,
        forbidden_path_components=tuple(
            component
            for component in config.forbidden_path_components
            if component.casefold() != ".git"
        ),
    )


def _copy_git_history(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise HarborPackError("materialized Git history is missing or redirected")
    for path in source.rglob("*"):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise HarborPackError("materialized Git history contains an unsafe entry")
    shutil.copytree(source, destination, symlinks=False)


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or path.as_posix() != value
        or "." in path.parts
        or ".." in path.parts
    ):
        raise HarborPackError(f"unsafe corpus manifest path: {value!r}")
    return path


def _ensure_real_parents(root: Path, path: Path) -> None:
    cursor = root
    for part in path.relative_to(root).parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise HarborPackError(f"corpus path traverses a symlink: {path}")


def _require_contained_link(root: Path, link: Path, target: str) -> None:
    if not target or Path(target).is_absolute():
        raise HarborPackError(f"unsafe corpus symlink target: {target!r}")
    resolved = (link.parent / target).resolve(strict=False)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise HarborPackError(f"corpus symlink escapes its project: {target!r}") from error


def _write_step_instructions(task_directory: Path, manifest: ChallengeManifestV1) -> None:
    """Write each step's instruction.md; Harbor ignores the task-root instruction.md.

    Both steps share the task-level ``tests/test.sh`` verifier (Harbor falls
    back to it when a step declares no verifier override of its own), so only
    the per-step prompts differ; grading of the intermediate ``establish``
    step is discarded by ``multi_step_reward_strategy = "final"``.
    """

    assert manifest.follow_up_prompt is not None
    for step_name, prompt in (
        (_ESTABLISH_STEP_NAME, manifest.prompt),
        (_FOLLOW_UP_STEP_NAME, manifest.follow_up_prompt),
    ):
        step_directory = task_directory / "steps" / step_name
        step_directory.mkdir(parents=True)
        _write_text(step_directory / "instruction.md", prompt)


def _task_toml(challenge: LoadedChallenge) -> str:
    manifest = challenge.manifest
    environment = manifest.environment
    multi_step_header = '\nmulti_step_reward_strategy = "final"\n' if manifest.is_multi_step else ""
    steps_section = (
        f"""
[[steps]]
name = {_toml_string(_ESTABLISH_STEP_NAME)}

[[steps]]
name = {_toml_string(_FOLLOW_UP_STEP_NAME)}
"""
        if manifest.is_multi_step
        else ""
    )
    network_mode = _HARBOR_NETWORK_MODE[environment.network_mode]
    return f"""schema_version = "1.3"{multi_step_header}
[task]
name = {_toml_string(f"local/{manifest.challenge_id}")}
description = {_toml_string(manifest.description)}

[verifier]
timeout_sec = {manifest.verifier.timeout_seconds}

[agent]
timeout_sec = {manifest.agent_timeout_seconds}

[environment]
network_mode = {_toml_string(network_mode)}
build_timeout_sec = {environment.build_timeout_seconds}
cpus = {environment.cpus}
memory_mb = {environment.memory_mb}
storage_mb = {environment.storage_mb}
{steps_section}"""


def _dockerfile(prebuild_argv: tuple[str, ...] = ()) -> str:
    prebuild = f"RUN {json.dumps(list(prebuild_argv))}\n" if prebuild_argv else ""
    return f"""FROM {task_image()}
WORKDIR /workspace
COPY project/ /workspace/
{prebuild}
"""


def _toml_string(value: str) -> str:
    # JSON strings are a strict subset of TOML basic strings.
    return json.dumps(value, ensure_ascii=True)


def _copy_verifier(challenge: LoadedChallenge, destination: Path) -> None:
    source = challenge.verifier_path
    if source.is_symlink():
        raise HarborPackError(f"verifier must not be a symlink: {source}")
    try:
        metadata = source.stat()
    except FileNotFoundError as error:
        raise HarborPackError(f"verifier no longer exists: {source}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise HarborPackError(f"verifier is not a regular file: {source}")

    digest = hashlib.sha256()
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
            output_file.write(block)
    expected = challenge.manifest.verifier.asset.sha256
    if digest.hexdigest() != expected:
        destination.unlink()
        raise HarborPackError(f"verifier digest changed: {challenge.challenge_id}")
    destination.chmod(0o755)


def _write_mutation_baseline(
    project_directory: Path,
    destination: Path,
    allowed_output_paths: tuple[str, ...],
    *,
    ignored_paths: tuple[str, ...] = (),
) -> None:
    """Write the hidden, canonical pre-agent workspace snapshot."""

    allowed = tuple(path.removeprefix("/workspace/") for path in allowed_output_paths)
    records: list[bytes] = []
    ignored = (*_MUTATION_IGNORED_PATHS, *ignored_paths)
    for path in sorted(project_directory.rglob("*"), key=lambda item: os.fsencode(item)):
        relative = path.relative_to(project_directory).as_posix()
        metadata = path.lstat()
        kind = _snapshot_kind(metadata.st_mode)
        if (
            _mutation_path_is_allowed(relative, allowed)
            or _mutation_path_is_allowed(relative, ignored)
            or _mutation_path_is_runtime_cache(relative)
        ):
            continue
        encoded_path = _base64_bytes(os.fsencode(relative))
        mode = format(stat.S_IMODE(metadata.st_mode), "o").encode("ascii")
        value = b""
        if kind == "file":
            value = hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii")
        elif kind == "symlink":
            value = _base64_bytes(os.fsencode(os.readlink(path)))
        records.append(b"\t".join((encoded_path, kind.encode("ascii"), mode, value)) + b"\n")
    records.sort()
    with destination.open("xb") as output:
        output.writelines(records)
    destination.chmod(0o644)


def _snapshot_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block"
    if stat.S_ISCHR(mode):
        return "character"
    return "unknown"


def _mutation_path_is_allowed(relative: str, allowed: tuple[str, ...]) -> bool:
    return any(relative == path or relative.startswith(f"{path}/") for path in allowed)


def _mutation_path_is_runtime_cache(relative: str) -> bool:
    path = PurePosixPath(relative)
    return any(component in path.parts for component in _MUTATION_IGNORED_COMPONENTS) or any(
        relative.endswith(suffix) for suffix in _MUTATION_IGNORED_SUFFIXES
    )


def _base64_bytes(value: bytes) -> bytes:
    return base64.b64encode(value)


def _verifier_wrapper(
    allowed_output_paths: tuple[str, ...],
    missing_output_ancestors: tuple[str, ...],
    *,
    ignored_paths: tuple[str, ...] = (),
) -> str:
    allowed = tuple(path.removeprefix("/workspace/") for path in allowed_output_paths)
    allowed_cases = "\n".join(
        f"    {shlex.quote(path)}|{shlex.quote(path)}/*) return 0 ;;" for path in allowed
    )
    ancestor_pattern = "|".join(shlex.quote(path) for path in missing_output_ancestors)
    ancestor_case = f"    {ancestor_pattern}) return 0 ;;" if missing_output_ancestors else ""
    # Prune Stele-infrastructure paths (its workspace binding) from the walk so
    # the audit never sees them on either side.
    ignored_prune = ""
    ignored = (*_MUTATION_IGNORED_PATHS, *ignored_paths)
    if ignored:
        ignored_paths = " -o ".join("-path " + shlex.quote("./" + path) for path in ignored)
        ignored_prune = f"\\( {ignored_paths} \\) -prune -o "
    runtime_cache_prune = (
        r"\( -type d -name '__pycache__' -o -type f "
        r"\( -name '*.pyc' -o -name '*.pyo' \) \) -prune -o "
    )
    # The task tests and verifier logs are deliberately outside workspace_root,
    # so the verifier owns no implicit exceptions inside the evaluated project.
    return f"""#!/bin/bash
set -uo pipefail

workspace_root=${{MUTATION_WORKSPACE_ROOT:-/workspace}}
tests_root=${{MUTATION_TESTS_ROOT:-/tests}}
logs_root=${{MUTATION_LOGS_ROOT:-/logs/verifier}}
export MUTATION_WORKSPACE_ROOT="$workspace_root"
export MUTATION_TESTS_ROOT="$tests_root"
export MUTATION_LOGS_ROOT="$logs_root"
baseline=$tests_root/{_MUTATION_BASELINE_NAME}
current=$logs_root/mutation-current.tsv
unsorted=$logs_root/mutation-unsorted.tsv
receipt=$logs_root/mutation.json
mkdir -p "$logs_root"

is_allowed() {{
  path=$1
  kind=$2
  case "$path" in
{allowed_cases}
  esac
  if [ "$kind" = directory ]; then
    case "$path" in
{ancestor_case}
    esac
  fi
  return 1
}}

snapshot_entry() {{
  path=$1
  relative=${{path#./}}
  if [ -L "$path" ]; then kind=symlink
  elif [ -f "$path" ]; then kind=file
  elif [ -d "$path" ]; then kind=directory
  elif [ -p "$path" ]; then kind=fifo
  elif [ -S "$path" ]; then kind=socket
  elif [ -b "$path" ]; then kind=block
  elif [ -c "$path" ]; then kind=character
  else kind=unknown
  fi
  if is_allowed "$relative" "$kind"; then return 0; fi
  encoded_path=$(printf %s "$relative" | base64 | tr -d '\n') || return 1
  mode=$(file_mode "$path") || return 1
  value=
  if [ "$kind" = file ]; then
    value=$(sha256_file "$path") || return 1
  elif [ "$kind" = symlink ]; then
    target=$(readlink "$path") || return 1
    value=$(printf %s "$target" | base64 | tr -d '\n') || return 1
  fi
  printf '%s\t%s\t%s\t%s\n' "$encoded_path" "$kind" "$mode" "$value"
}}

file_mode() {{
  if stat -c %a "$1" >/dev/null 2>&1; then
    stat -c %a "$1"
  else
    stat -f %Lp "$1"
  fi
}}

sha256_file() {{
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{{print $1}}'
  else
    shasum -a 256 "$1" | awk '{{print $1}}'
  fi
}}

mutation_paths() {{
  # stdin: mutation TSV lines; emit a JSON array of decoded, escaped paths so a
  # human can read exactly which files broke the audit (base64 path is field 1).
  sep=
  printf '['
  while read -r encoded rest; do
    [ -n "$encoded" ] || continue
    decoded=$(printf %s "$encoded" | base64 -d 2>/dev/null) || decoded=$encoded
    escaped=$(printf %s "$decoded" | sed '{_JSON_ESCAPE_SED}')
    printf '%s"%s"' "$sep" "$escaped"
    sep=,
  done
  printf ']'
}}

snapshot() {{
  cd "$workspace_root" || return 1
  : > "$unsorted" || return 1
  while IFS= read -r -d '' candidate; do
    snapshot_entry "$candidate" >> "$unsorted" || return 1
  done < <(find . -mindepth 1 {ignored_prune}{runtime_cache_prune}-print0)
  LC_ALL=C sort "$unsorted"
}}

before_sha=$(sha256_file "$baseline") || before_sha=
audit_status=error
after_sha=
only_in_workspace='[]'
only_in_baseline='[]'
if snapshot > "$current"; then
  after_sha=$(sha256_file "$current") || after_sha=
  if [ -n "$before_sha" ] && [ -n "$after_sha" ] && cmp -s "$baseline" "$current"; then
    audit_status=passed
  else
    audit_status=failed
    only_in_workspace=$(LC_ALL=C comm -13 "$baseline" "$current" | mutation_paths)
    only_in_baseline=$(LC_ALL=C comm -23 "$baseline" "$current" | mutation_paths)
  fi
fi
{{
  printf '{{"schema_version":1,"status":"%s",' "$audit_status"
  printf '"before_sha256":"%s","after_sha256":"%s",' "$before_sha" "$after_sha"
  printf '"only_in_workspace":%s,' "$only_in_workspace"
  printf '"only_in_baseline":%s}}\n' "$only_in_baseline"
}} > "$receipt"
rm -f "$current" "$unsorted"

if [ "$audit_status" != passed ]; then
  printf '{{"reward":0}}\n' > "$logs_root/reward.json"
  {{
    printf '{{"reason":"undeclared workspace mutation",'
    printf '"mutation_receipt":"mutation.json","only_in_workspace":%s,' "$only_in_workspace"
    printf '"only_in_baseline":%s}}\n' "$only_in_baseline"
  }} > "$logs_root/evidence.json"
  exit 0
fi

exec "$tests_root/{_CHALLENGE_VERIFIER_NAME}"
"""


def _path_ancestors(path: str) -> tuple[str, ...]:
    parts = PurePosixPath(path).parts
    return tuple(PurePosixPath(*parts[:index]).as_posix() for index in range(1, len(parts)))


def _missing_output_ancestors(
    project_directory: Path, allowed_output_paths: tuple[str, ...]
) -> tuple[str, ...]:
    missing = {
        ancestor
        for output in allowed_output_paths
        for ancestor in _path_ancestors(output.removeprefix("/workspace/"))
        if not os.path.lexists(project_directory / ancestor)
    }
    return tuple(sorted(missing))


def _plain_job_config(
    task_paths: tuple[str, ...], resume_trajectory: bool = False
) -> dict[str, object]:
    agent: dict[str, object] = {"name": "claude-code"}
    if resume_trajectory:
        agent["resume_trajectory"] = True
    return {
        "job_name": _PLAIN_JOB_NAME,
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "agents": [agent],
        "tasks": [{"path": task_path} for task_path in task_paths],
    }


def _configured_job_config(
    task_paths: tuple[str, ...],
    faithful: FaithfulAgentConfig,
    resume_trajectory: bool = False,
) -> dict[str, object]:
    kwargs: dict[str, str] = {
        "artifact_path": os.fspath(faithful.artifact_path.absolute()),
        "artifact_sha256": faithful.artifact_sha256,
        "artifact_source_kind": faithful.artifact_source_kind,
        "credentials_path": os.fspath(faithful.credentials_path.absolute()),
        "preferences_path": os.fspath(faithful.preferences_path.absolute()),
        **(
            {"project_binding_path": os.fspath(faithful.project_binding_path.absolute())}
            if faithful.project_binding_path is not None
            else {}
        ),
        "auto_rag_injection": faithful.auto_rag_injection,
        "project_provisioning": faithful.project_provisioning,
        "project_name": faithful.project_name,
    }
    if faithful.artifact_release_version:
        kwargs["artifact_release_version"] = faithful.artifact_release_version
    agent: dict[str, object] = {
        "import_path": HARBOR_AGENT_IMPORT_PATH,
        "kwargs": kwargs,
        "env": {
            "SUPABASE_URL": faithful.backend_url,
            "SUPABASE_ANON_KEY": faithful.backend_anon_key,
        },
    }
    if resume_trajectory:
        agent["resume_trajectory"] = True
    return {
        "job_name": _CONFIGURED_JOB_NAME,
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "agents": [agent],
        "tasks": [{"path": task_path} for task_path in task_paths],
    }


def _write_text(path: Path, contents: str, *, mode: int = 0o644) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(contents)
    path.chmod(mode)


def _write_json(path: Path, data: dict[str, object]) -> None:
    _write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _require_config_path(path: Path) -> None:
    if not os.fspath(path):
        raise ValueError("config_path must not be empty")
    if path.is_symlink():
        raise ValueError("config_path must not be a symlink")
