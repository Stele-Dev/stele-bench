"""Harbor adapter for a Claude Code run with a prebuilt integration artifact.

The public benchmark package deliberately owns only the portable adapter.  It
does not build or otherwise depend on the source tree that produced the
uploaded executable.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from .preflight import (
    CREATED_PROJECT_REPORT_NAME,
    DEFAULT_EVALUATED_PROJECT_DIR,
    VERIFY_ROOTS,
    ActiveProjectBinding,
    AutoRagInjection,
    ProjectProvisioning,
    StructuralPreflightError,
    binding_from_project_cache,
    create_project_command,
    discover_workspace_command,
    parse_created_slug,
    parse_workspace,
    run_structural_preflight,
    strip_injection_channel_command,
    validate_project_cache,
    verify_injection_channel,
    with_claude_local_bin,
)

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_RE = re.compile(r"^[0-9]+:[0-9]+$")
_HOME_RE = re.compile(r"^/[A-Za-z0-9._/@+:-]+$")

_ARTIFACT_DESTINATION = "/opt/agent-tools/stele"
_STELE_COMMAND_LINK = "/usr/local/bin/stele"
_CLAUDE_RUNTIME_CONFIG_DIR = PurePosixPath("/logs/agent/sessions")
# The fences `stele install` writes around its instructions block in the
# user-level CLAUDE.md. A real install tells the agent how to use Stele there,
# so the configured arm must run with it (the evaluated project's own CLAUDE.md
# is a different file, stripped from every arm while packaging).
_STELE_INSTRUCTIONS_MARKERS = (
    "======== Stele: Important Project Context  ===========",
    "======== Stele: Instructions End  ====================",
)


class HarborUnavailableError(RuntimeError):
    """Raised when an adapter is requested without Harbor installed."""


class ArtifactVerificationError(RuntimeError):
    """Raised before any upload when a declared artifact is not immutable."""


class AttemptStateError(ValueError):
    """Raised when attempt-local configuration is not valid JSON object data."""


def _validate_sha256(value: str) -> str:
    normalized = value.lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("sha256 must be exactly 64 lowercase hexadecimal characters")
    return normalized


def sha256_file(path: Path) -> str:
    """Return a content digest without loading a potentially large artifact at once."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArtifactProvenance:
    """Portable artifact identity; deliberately excludes its host filesystem path."""

    source_kind: Literal["release", "local-dirty-worktree"]
    sha256: str
    release_version: str | None = None


@dataclass(frozen=True)
class _ArtifactBase:
    path: Path = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).expanduser())
        object.__setattr__(self, "sha256", _validate_sha256(self.sha256))

    def verify(self) -> Path:
        """Verify the immutable content identity immediately before upload."""

        try:
            artifact_path = self.path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ArtifactVerificationError("artifact file does not exist") from exc
        if not artifact_path.is_file():
            raise ArtifactVerificationError("artifact path is not a regular file")
        if sha256_file(artifact_path) != self.sha256:
            raise ArtifactVerificationError("artifact SHA-256 does not match its declaration")
        return artifact_path


@dataclass(frozen=True)
class ReleasedArtifact(_ArtifactBase):
    """A previously released, prebuilt executable selected for a run."""

    release_version: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.release_version.strip():
            raise ValueError("release_version must be non-empty")

    @property
    def provenance(self) -> ArtifactProvenance:
        return ArtifactProvenance(
            source_kind="release",
            sha256=self.sha256,
            release_version=self.release_version,
        )


@dataclass(frozen=True)
class LocalDirtyWorktreeArtifact(_ArtifactBase):
    """A prebuilt executable produced from a local, possibly dirty worktree."""

    @property
    def provenance(self) -> ArtifactProvenance:
        return ArtifactProvenance(source_kind="local-dirty-worktree", sha256=self.sha256)


type AgentArtifact = ReleasedArtifact | LocalDirtyWorktreeArtifact


def select_artifact(
    *,
    released: ReleasedArtifact,
    local_dirty_worktree: LocalDirtyWorktreeArtifact | None = None,
) -> AgentArtifact:
    """Prefer an explicit local prebuilt artifact without ever building one."""

    return local_dirty_worktree if local_dirty_worktree is not None else released


def _require_json_object(payload: bytes, label: str) -> None:
    try:
        decoded = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttemptStateError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise AttemptStateError(f"{label} must be a JSON object")


@dataclass(frozen=True)
class AttemptState:
    """Fresh, attempt-scoped state uploaded outside the evaluated project."""

    credentials_json: bytes = field(repr=False)
    preferences_json: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require_json_object(self.credentials_json, "credentials_json")
        _require_json_object(self.preferences_json, "preferences_json")


def merge_json_objects(
    existing: Mapping[str, JsonValue], incoming: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """Merge object branches recursively while replacing only explicit leaf values."""

    result: dict[str, JsonValue] = dict(existing)
    for key, incoming_value in incoming.items():
        existing_value = result.get(key)
        if isinstance(existing_value, dict) and isinstance(incoming_value, dict):
            result[key] = merge_json_objects(existing_value, incoming_value)
        else:
            result[key] = incoming_value
    return result


_HOST_ALIAS_COMMAND = (
    "if ! getent hosts host.docker.internal >/dev/null 2>&1; then "
    "gw=$(awk '$2==\"00000000\" {print $3; exit}' /proc/net/route); "
    'if [ -n "$gw" ]; then '
    "printf '%d.%d.%d.%d host.docker.internal\\n' "
    '"0x$(echo "$gw" | cut -c7-8)" "0x$(echo "$gw" | cut -c5-6)" '
    '"0x$(echo "$gw" | cut -c3-4)" "0x$(echo "$gw" | cut -c1-2)" >> /etc/hosts; '
    "fi; fi"
)


class _Environment(Protocol):
    async def exec(self, command: str, **kwargs: Any) -> Any: ...

    async def upload_file(self, source_path: Path | str, target_path: str) -> None: ...


def _parse_agent_home(result: Any) -> tuple[PurePosixPath, str]:
    output = (getattr(result, "stdout", None) or "").strip()
    home, separator, identity = output.partition("\n")
    if not separator or not _HOME_RE.fullmatch(home) or not _IDENTITY_RE.fullmatch(identity):
        raise RuntimeError("could not determine a safe default-agent home directory")
    return PurePosixPath(home), identity


def _quoted(path: PurePosixPath | str) -> str:
    return shlex.quote(str(path))


def _parse_claude_settings(raw: str) -> dict[str, JsonValue]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("Claude settings must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("Claude settings must be a JSON object")
    return parsed


def _install_stele_command_link() -> str:
    """Expose the uploaded artifact to plugin MCP and hook commands by name."""

    expected_target = (
        f'[ "$(readlink {_STELE_COMMAND_LINK})" = '
        f'"{_ARTIFACT_DESTINATION}" ]'
    )
    return "\n".join(
        (
            "install -d -m 0755 /usr/local/bin",
            f"if [ -e {_STELE_COMMAND_LINK} ] || [ -L {_STELE_COMMAND_LINK} ]; then",
            f"  if [ ! -L {_STELE_COMMAND_LINK} ] || ! {expected_target}; then",
            '    echo "refusing to replace an unexpected stele command" >&2',
            "    exit 1",
            "  fi",
            "else",
            f"  ln -s {_ARTIFACT_DESTINATION} {_STELE_COMMAND_LINK}",
            "fi",
            f"chown -h root:root {_STELE_COMMAND_LINK}",
        )
    )


class _ClaudeSetupBehavior:
    """Mixin whose ``super()`` resolves to Harbor's stock Claude adapter."""

    def __init__(
        self,
        logs_dir: Path,
        *args: Any,
        artifact: AgentArtifact,
        attempt_state: AttemptState,
        project_binding: ActiveProjectBinding | None = None,
        project_provisioning: ProjectProvisioning = "restore",
        project_name: str = "Workspace",
        claude_settings_patch: Mapping[str, JsonValue] | None = None,
        auto_rag_injection: AutoRagInjection = "on",
        **kwargs: Any,
    ) -> None:
        self.artifact = artifact
        self.attempt_state = attempt_state
        if project_provisioning not in ("restore", "create"):
            raise ValueError("project_provisioning must be restore or create")
        if project_provisioning == "restore" and project_binding is None:
            raise ValueError("restoring a project requires a binding")
        self.project_provisioning: ProjectProvisioning = project_provisioning
        self.project_name = project_name
        self.project_binding = project_binding
        self.created_project_report_path: Path | None = None
        # Replaced by what the container reports before anything is bound. The
        # default is only right for a task pack we generated ourselves.
        self.evaluated_project_dir = DEFAULT_EVALUATED_PROJECT_DIR
        if auto_rag_injection not in ("on", "off"):
            raise ValueError("auto_rag_injection must be on or off")
        self.auto_rag_injection: AutoRagInjection = auto_rag_injection
        self.preflight_report_path: Path | None = None
        self.injection_channel_report_path: Path | None = None
        if claude_settings_patch is None:
            self.claude_settings_patch: dict[str, JsonValue] = {}
        elif not isinstance(claude_settings_patch, Mapping):
            raise ValueError("claude_settings_patch must be an object")
        else:
            self.claude_settings_patch = dict(claude_settings_patch)
        try:
            encoded_patch = json.dumps(self.claude_settings_patch, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("claude_settings_patch must contain JSON values") from exc
        if not isinstance(json.loads(encoded_patch), dict):
            raise ValueError("claude_settings_patch must be an object")
        super().__init__(logs_dir, *args, **kwargs)

    async def _upload_credentials_and_preferences(
        self, environment: _Environment
    ) -> tuple[str, PurePosixPath]:
        identity_result = await environment.exec(
            'printf "%s\\n%s:%s" "$HOME" "$(id -u)" "$(id -g)"'
        )
        home, owner = _parse_agent_home(identity_result)
        config_dir = home / ".config" / "stele"
        claude_dir = home / ".claude"
        credentials_destination = config_dir / "credentials.json"
        preferences_destination = config_dir / "preferences.json"

        await self.exec_as_agent(
            environment,
            command=(
                f"install -d -m 0700 {_quoted(config_dir)} {_quoted(claude_dir)}"
            ),
        )

        with tempfile.TemporaryDirectory(prefix="agent-state-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            credentials_path = temp_dir / "credentials.json"
            preferences_path = temp_dir / "preferences.json"
            credentials_path.write_bytes(self.attempt_state.credentials_json)
            preferences_path.write_bytes(self.attempt_state.preferences_json)
            credentials_path.chmod(0o600)
            preferences_path.chmod(0o600)
            await environment.upload_file(credentials_path, str(credentials_destination))
            await environment.upload_file(preferences_path, str(preferences_destination))

        await self.exec_as_root(
            environment,
            command=(
                f"chown {_quoted(owner)} {_quoted(credentials_destination)} "
                f"{_quoted(preferences_destination)} && chmod 0600 "
                f"{_quoted(credentials_destination)} {_quoted(preferences_destination)}"
            ),
        )

        return owner, claude_dir / "settings.json"

    async def _merge_claude_settings(
        self,
        environment: _Environment,
        owner: str,
        settings_destination: PurePosixPath,
    ) -> None:
        if not self.claude_settings_patch:
            return

        existing = _parse_claude_settings(
            (
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"if [ -f {_quoted(settings_destination)} ]; then "
                        f"cat {_quoted(settings_destination)}; else printf '{{}}'; fi"
                    ),
                )
            ).stdout
            or ""
        )
        merged = merge_json_objects(existing, self.claude_settings_patch)
        merged_bytes = (
            json.dumps(merged, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="claude-settings-") as temp_dir_name:
            patch_path = Path(temp_dir_name) / "settings.json"
            patch_path.write_bytes(merged_bytes)
            patch_path.chmod(0o600)
            await environment.upload_file(patch_path, str(settings_destination))
        await self.exec_as_root(
            environment,
            command=(
                f"chown {_quoted(owner)} {_quoted(settings_destination)} && "
                f"chmod 0600 {_quoted(settings_destination)}"
            ),
        )

    async def _discover_evaluated_project(self, environment: _Environment) -> str:
        """Ask the container where the agent starts, instead of assuming.

        Our own task pack writes `WORKDIR /workspace`, so this returns the same
        value it always did there. A *borrowed* image uses whatever its own
        Dockerfile says — `aider-polyglot` uses `/app` — and assuming otherwise
        fails at `stele project create` with a `chdir to cwd` error, after the
        install has already succeeded, which reads like a Stele bug rather than
        a wrong assumption about somebody else's image.
        """

        result = await self.exec_as_agent(
            environment, command=discover_workspace_command()
        )
        return parse_workspace(getattr(result, "stdout", "") or "")

    async def _create_evaluated_project(self, environment: _Environment) -> None:
        """Make a project the way a new user does, then bind to it.

        This is the arm for borrowed tasks that have no memory intent: there is
        no graph to restore, because the thing being measured is what a *fresh*
        install costs. A hand-built empty fixture would approximate this; the
        real command IS this, including the starter nodes a new project seeds
        (a root component, the "fill this graph" task, and one decision). Those
        matter — with them, recall actually retrieves something instead of
        short-circuiting on an empty result, so the overhead measured is the
        real path rather than the degenerate one.
        """

        created = await self.exec_as_agent(
            environment,
            command=create_project_command(_ARTIFACT_DESTINATION, self.project_name),
            cwd=self.evaluated_project_dir,
        )
        slug = parse_created_slug(getattr(created, "stdout", "") or "")
        # `project create` does NOT write the directory binding — that is
        # `project use`, a separate step. Skipping it leaves the agent unbound,
        # which is indistinguishable from having no Stele at all.
        await self.exec_as_agent(
            environment,
            command=f"{_quoted(_ARTIFACT_DESTINATION)} project use {_quoted(slug)}",
            cwd=self.evaluated_project_dir,
        )
        cache = await self.exec_as_agent(
            environment,
            command=f"cat {self.evaluated_project_dir}/.stele/project.json",
            cwd=self.evaluated_project_dir,
        )
        binding = binding_from_project_cache(cache.stdout or "")
        if binding.slug != slug:
            raise StructuralPreflightError(
                "directory is bound to a different project than the one created"
            )
        self.project_binding = binding
        self.created_project_report_path = self._write_created_project(binding)

    def _write_created_project(self, binding: ActiveProjectBinding) -> Path:
        """Record the created project and the exact binding for a later session.

        The aggregate fields stay for the existing fresh-project uptake reader.
        ``binding`` is the lossless hand-off: a compounding run writes it back as
        the next container's ordinary project cache instead of reconstructing a
        binding from partial database metadata.
        """

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        path = self.logs_dir / CREATED_PROJECT_REPORT_NAME
        path.write_text(
            json.dumps(
                {
                    "project_id": binding.project_id,
                    "slug": binding.slug,
                    "binding": binding.as_cache_object(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    async def _bind_evaluated_project(self, environment: _Environment) -> None:
        if self.project_provisioning == "create":
            await self._create_evaluated_project(environment)
            return
        assert self.project_binding is not None
        await self.exec_as_agent(
            environment,
            command=(
                f"{_quoted(_ARTIFACT_DESTINATION)} project use "
                f"{_quoted(self.project_binding.slug)}"
            ),
            cwd=self.evaluated_project_dir,
        )
        cache = await self.exec_as_agent(
            environment,
            command=f"cat {self.evaluated_project_dir}/.stele/project.json",
            cwd=self.evaluated_project_dir,
        )
        validate_project_cache(cache.stdout or "", self.project_binding)

    async def _stage_claude_runtime_config(
        self,
        environment: _Environment,
        *,
        owner: str,
        source_dir: PurePosixPath,
    ) -> None:
        """Stage the installed plugin state and instructions Harbor's Claude process reads.

        Harbor points ``CLAUDE_CONFIG_DIR`` at the runtime dir, so anything the
        install wrote to ``~/.claude`` and not copied here never reaches the
        agent. The user-level ``CLAUDE.md`` carries the Stele instructions block;
        without it the agent is not told to activate recall.
        """

        uid, gid = owner.split(":", maxsplit=1)
        source_plugins = source_dir / "plugins"
        source_settings = source_dir / "settings.json"
        source_instructions = source_dir / "CLAUDE.md"
        destination_plugins = _CLAUDE_RUNTIME_CONFIG_DIR / "plugins"
        destination_settings = _CLAUDE_RUNTIME_CONFIG_DIR / "settings.json"
        destination_instructions = _CLAUDE_RUNTIME_CONFIG_DIR / "CLAUDE.md"

        await self.exec_as_root(
            environment,
            command=(
                f"install -d -m 0700 -o {_quoted(uid)} -g {_quoted(gid)} "
                f"{_quoted(_CLAUDE_RUNTIME_CONFIG_DIR)}"
            ),
        )
        await self.exec_as_agent(
            environment,
            command="\n".join(
                (
                    "set -eu",
                    "umask 077",
                    f"test -d {_quoted(source_plugins)}",
                    f"test ! -L {_quoted(source_plugins)}",
                    f"test -f {_quoted(source_settings)}",
                    f"test ! -L {_quoted(source_settings)}",
                    f"test ! -e {_quoted(destination_plugins)}",
                    f"test ! -L {_quoted(destination_plugins)}",
                    f"test ! -e {_quoted(destination_settings)}",
                    f"test ! -L {_quoted(destination_settings)}",
                    f"cp -a -- {_quoted(source_plugins)} {_quoted(destination_plugins)}",
                    f"cp -a -- {_quoted(source_settings)} {_quoted(destination_settings)}",
                    f"cmp -s -- {_quoted(source_settings)} {_quoted(destination_settings)}",
                    f"test -f {_quoted(destination_plugins / 'installed_plugins.json')}",
                    f"test -f {_quoted(source_instructions)}",
                    f"test ! -L {_quoted(source_instructions)}",
                    f"test ! -e {_quoted(destination_instructions)}",
                    f"test ! -L {_quoted(destination_instructions)}",
                    f"cp -a -- {_quoted(source_instructions)} {_quoted(destination_instructions)}",
                    *(
                        f"grep -qF -- {_quoted(marker)} {_quoted(destination_instructions)}"
                        for marker in _STELE_INSTRUCTIONS_MARKERS
                    ),
                )
            ),
        )

    async def setup(self, environment: _Environment) -> None:
        """Prepare the agent without changing Harbor's stock execution or receipt path."""

        artifact_path = self.artifact.verify()
        await super().setup(environment)

        await self.exec_as_root(
            environment, command="install -d -m 0755 /opt/agent-tools"
        )
        await environment.upload_file(artifact_path, _ARTIFACT_DESTINATION)
        await self.exec_as_root(
            environment, command=f"chmod 0755 {_quoted(_ARTIFACT_DESTINATION)}"
        )
        await self.exec_as_root(environment, command=_install_stele_command_link())
        # Docker Desktop resolves host.docker.internal by itself; plain Docker on
        # Linux does not. Map it to this container's default gateway (the host)
        # here, in the configured arm only, so the stock arm and the shared task
        # pack carry no trace of a host-side backend.
        await self.exec_as_root(environment, command=_HOST_ALIAS_COMMAND)

        owner, settings_destination = await self._upload_credentials_and_preferences(environment)
        await self.exec_as_agent(
            environment,
            command=with_claude_local_bin(
                f"{_quoted(_ARTIFACT_DESTINATION)} install --only=claude --yes --json"
            ),
        )
        if self.auto_rag_injection == "off":
            # Before the preflight, so the preflight inspects the arm as the
            # agent will actually run it, and before the runtime config is
            # staged, so the staged copy carries the strip too.
            await self.exec_as_agent(
                environment, command=strip_injection_channel_command()
            )
        # Before anything is bound, because binding runs commands *inside* the
        # evaluated project and a borrowed image does not put it where ours does.
        self.evaluated_project_dir = await self._discover_evaluated_project(environment)
        # Either binds the restored project or creates a fresh one; both paths
        # leave `project_binding` populated, which the preflight then verifies
        # the container actually agrees with.
        await self._bind_evaluated_project(environment)
        assert self.project_binding is not None
        await self._merge_claude_settings(environment, owner, settings_destination)
        self.preflight_report_path = await run_structural_preflight(
            self,
            environment,
            artifact_path=_ARTIFACT_DESTINATION,
            expected_artifact_sha256=self.artifact.sha256,
            binding=self.project_binding,
            project_dir=self.evaluated_project_dir,
        )
        await self._stage_claude_runtime_config(
            environment,
            owner=owner,
            source_dir=settings_destination.parent,
        )
        # Last, deliberately: the staged tree is what the harness reads at run
        # time, so this is the only copy whose wiring decides what the agent
        # actually receives. Fails the whole setup if the arm is not what it says.
        self.injection_channel_report_path = await verify_injection_channel(
            self,
            environment,
            roots=(*VERIFY_ROOTS, _quoted(_CLAUDE_RUNTIME_CONFIG_DIR)),
            expected=self.auto_rag_injection,
        )


def _make_harbor_adapter[T](base_class: type[T]) -> type[T]:
    """Build the adapter lazily so importing this module does not require Harbor."""

    class HarborClaudeAdapter(_ClaudeSetupBehavior, base_class):  # type: ignore[misc, valid-type]
        pass

    HarborClaudeAdapter.__name__ = "SteleClaudeCode"
    HarborClaudeAdapter.__qualname__ = "SteleClaudeCode"
    return HarborClaudeAdapter


try:  # Harbor is intentionally an optional runtime dependency of this package.
    from harbor.agents.installed.claude_code import ClaudeCode as _HarborClaudeCode
except ImportError:  # pragma: no cover - exercised by import-only consumers.
    _HarborClaudeCode = None


if _HarborClaudeCode is not None:
    SteleClaudeCode = _make_harbor_adapter(_HarborClaudeCode)
else:

    class SteleClaudeCode:
        """Placeholder that fails clearly when the optional Harbor extra is absent."""

        def __init__(self, *_: Any, **__: Any) -> None:
            raise HarborUnavailableError(
                "Harbor is required to construct SteleClaudeCode; install the Harbor extra"
            )
