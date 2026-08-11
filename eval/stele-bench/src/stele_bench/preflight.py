"""Structural, zero-model checks for a prepared Claude Code environment."""

from __future__ import annotations

import json
import re
import shlex
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

type AutoRagInjection = Literal["on", "off"]
# "restore" installs a frozen fixture graph; "create" makes a project the way a
# new user does, starter nodes and all.
type ProjectProvisioning = Literal["restore", "create"]

_BINDING_KEYS = {
    "projectId",
    "slug",
    "name",
    "defaultBranch",
    "activeWorkspaceIds",
}
_PROJECT_CACHE_REQUIRED_KEYS = {"projectId", "slug", "name"}
_PROJECT_CACHE_OPTIONAL_KEYS = {"defaultBranch", "activeWorkspaceIds"}
_PROJECT_CACHE_ALLOWED_KEYS = _PROJECT_CACHE_REQUIRED_KEYS | _PROJECT_CACHE_OPTIONAL_KEYS
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_EXPECTED_PLUGIN_ID = "stele@stele-local"
_EXPECTED_MARKETPLACE = "stele-local"
_EXPECTED_MCP_COMMAND = "stele"
_EXPECTED_MCP_ARGS = ("mcp", "--harness", "claude-code")
_EXPECTED_HOOK_GROUPS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PreCompact",
)
_EXPECTED_ALLOWLIST = ("Bash(stele account status:*)",)
_HOOK_PROBES = (
    "session-start",
    "auto-rag",
    "discovery",
    "change-nudge",
    "complete-task-reminder",
    "task-watch",
    "boundary-recall",
    "precompact-reset",
)
# Where the evaluated project lives inside the container, when nobody has told
# us otherwise. This is right for a task pack we generate — `harbor_pack` writes
# `WORKDIR /workspace` — and WRONG for a borrowed one, which uses whatever its
# own Dockerfile says (`aider-polyglot` uses `/app`). Binding a project in a
# directory that does not exist fails with
#
#   OCI runtime exec failed: ... chdir to cwd ("/workspace") ... no such file or
#   directory
#
# and it fails at `stele project create`, i.e. after the install has succeeded,
# which makes it look like a Stele bug rather than a wrong assumption about
# somebody else's image. So this is only a fallback: `discover_workspace_command`
# asks the container where the agent actually starts.
DEFAULT_EVALUATED_PROJECT_DIR = "/workspace"
# Kept as the historical name for callers that pass no directory of their own.
EVALUATED_PROJECT_DIR = DEFAULT_EVALUATED_PROJECT_DIR
_CLAUDE_LOCAL_BIN_PREFIX = 'export PATH="$HOME/.local/bin:$PATH"; '
_ABSOLUTE_PATH = re.compile(r"^/[^\0\n]*$")

# Auto-RAG-as-an-arm-dimension (TASK-2497). The `faithful-norag` condition is
# `faithful` with prompt-time injection removed, so the *only* thing separating
# the two arms is whether these hook wirings survive in the installed plugin.
#
# Both directions are asserted, and that is the point. An arm that was supposed
# to have injection but silently lost it looks exactly like a real "auto-RAG
# doesn't help" result — the one failure this experiment could never detect from
# its own output. So `faithful` fails closed if the wiring is absent, not just
# `faithful-norag` if it survives.
#
# The roots deliberately include every place a copy can live. A single logical
# install scatters across three: the synthesized marketplace, the harness config
# directory, and the installed plugin's own cache — and at run time the harness
# reads a *staged* copy under its config-directory override, not the home-dir
# original (KNOW-2415). Verifying the tree that is easy to reach instead of the
# tree the agent actually reads is how this check would quietly pass while the
# arm still injects.
_AUTO_RAG_HOOK_NEEDLE = "stele hook auto-rag"
# What the hook command is rewritten to. `true` ignores the `--harness=claude`
# argument left behind and exits 0 with no stdout, which is exactly how the
# harness reads "this hook had nothing to inject". Substituting one plain word
# for another cannot invalidate the surrounding JSON, so the rewrite needs no
# JSON parser in the container (the base image has no Python).
_AUTO_RAG_NEUTRALISED = "true"
# A sibling hook that both arms keep: proves a zero auto-RAG count means "edited"
# rather than "the whole install went missing".
_SIBLING_HOOK_NEEDLE = "stele hook discovery"
# Where the strip is allowed to WRITE: the two directories a Stele install owns.
INSTALL_ROOTS = ('"$HOME/.stele"', '"$HOME/.claude"')
# Where the verification READS. Deliberately wider than the strip: the whole
# home directory, plus whatever staged copy the caller adds. Strip narrowly,
# verify broadly — if the install ever grows a location we did not anticipate,
# a surviving reference there aborts the arm instead of passing unseen.
VERIFY_ROOTS = ('"$HOME"',)
INJECTION_CHANNEL_REPORT_NAME = "injection-channel.json"
CREATED_PROJECT_REPORT_NAME = "created-project.json"

# `stele project create` prints the slug it chose; it does not accept --json, and
# it does not bind the directory — `project use` is a separate step. The slug is
# PARSED rather than derived from the name because the CLI appends a suffix on
# collision, and the same project name is used for every task in a suite.
_CREATED_PROJECT = re.compile(r"Created project\s+(?P<slug>[a-z0-9][a-z0-9-]*)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class BindingValidationError(ValueError):
    """A project binding file does not have the narrow expected public shape."""


class StructuralPreflightError(RuntimeError):
    """A zero-model installation postcondition did not hold."""

    def __init__(self, message: str, *, report_path: Path | None = None) -> None:
        super().__init__(message)
        self.report_path = report_path


@dataclass(frozen=True)
class ActiveProjectBinding:
    """The exact, fully populated project-binding input for one attempt."""

    project_id: str
    slug: str
    name: str
    default_branch: str
    active_workspace_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _canonical_uuid(self.project_id, "projectId")
        if not _SLUG_RE.fullmatch(self.slug):
            raise BindingValidationError("slug must be a safe lowercase identifier")
        if not _safe_name(self.name):
            raise BindingValidationError("name must be a safe non-empty display name")
        if not _BRANCH_RE.fullmatch(self.default_branch) or ".." in self.default_branch:
            raise BindingValidationError("defaultBranch must be a safe branch name")
        if len(set(self.active_workspace_ids)) != len(self.active_workspace_ids):
            raise BindingValidationError("activeWorkspaceIds must not repeat values")
        for workspace_id in self.active_workspace_ids:
            _canonical_uuid(workspace_id, "activeWorkspaceIds")

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> ActiveProjectBinding:
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BindingValidationError("project binding must be valid JSON") from error
        if not isinstance(parsed, dict) or set(parsed) != _BINDING_KEYS:
            raise BindingValidationError(
                "project binding must have exactly the active-project fields"
            )
        workspaces = parsed["activeWorkspaceIds"]
        if not isinstance(workspaces, list) or not all(
            isinstance(item, str) for item in workspaces
        ):
            raise BindingValidationError("activeWorkspaceIds must be an array of UUID strings")
        fields = ("projectId", "slug", "name", "defaultBranch")
        if not all(isinstance(parsed[field], str) for field in fields):
            raise BindingValidationError("project binding fields must be strings")
        return cls(
            project_id=parsed["projectId"],
            slug=parsed["slug"],
            name=parsed["name"],
            default_branch=parsed["defaultBranch"],
            active_workspace_ids=tuple(workspaces),
        )

    def as_cache_object(self) -> dict[str, object]:
        return {
            "projectId": self.project_id,
            "slug": self.slug,
            "name": self.name,
            "defaultBranch": self.default_branch,
            "activeWorkspaceIds": list(self.active_workspace_ids),
        }


class AgentCommandRunner(Protocol):
    logs_dir: Path

    async def exec_as_agent(
        self, environment: Any, command: str, **kwargs: Any
    ) -> Any: ...


def with_claude_local_bin(command: str) -> str:
    """Run a setup-only command with Harbor's stock Claude install directory."""

    return f"{_CLAUDE_LOCAL_BIN_PREFIX}{command}"


def _canonical_uuid(value: str, label: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise BindingValidationError(f"{label} must contain UUID values") from error
    if str(parsed) != value:
        raise BindingValidationError(f"{label} UUID values must use canonical lowercase form")


def _safe_name(value: str) -> bool:
    return bool(value.strip()) and len(value) <= 160 and all(ord(char) >= 32 for char in value)


def binding_from_project_cache(payload: str) -> ActiveProjectBinding:
    """Read the directory binding a created project produced.

    Deliberately parses the CACHE shape, not the strict provisioner-input shape:
    `project use` serialises only the required active-project fields when the
    optional ones are absent, so the strict reader rejects a perfectly good
    binding (KNOW-2436). Optional fields fall back to the same defaults the
    product itself uses.
    """

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise StructuralPreflightError("active project cache must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise StructuralPreflightError("active project cache must be an object")
    keys = set(parsed)
    if not _PROJECT_CACHE_REQUIRED_KEYS <= keys or not keys <= _PROJECT_CACHE_ALLOWED_KEYS:
        raise StructuralPreflightError("active project cache has an unexpected field shape")
    if not all(isinstance(parsed[key], str) for key in _PROJECT_CACHE_REQUIRED_KEYS):
        raise StructuralPreflightError("active project cache required fields must be strings")
    workspace_ids = parsed.get("activeWorkspaceIds", [])
    if not isinstance(workspace_ids, list) or not all(
        isinstance(value, str) for value in workspace_ids
    ):
        raise StructuralPreflightError("active project cache workspace IDs must be strings")
    try:
        return ActiveProjectBinding(
            project_id=parsed["projectId"],
            slug=parsed["slug"],
            name=parsed["name"],
            default_branch=str(parsed.get("defaultBranch") or "main"),
            active_workspace_ids=tuple(workspace_ids),
        )
    except BindingValidationError as error:
        raise StructuralPreflightError("active project cache values are invalid") from error


def validate_project_cache(payload: str, expected: ActiveProjectBinding) -> None:
    """Validate the authoritative cache without requiring optional enrichments."""

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise StructuralPreflightError("active project cache must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise StructuralPreflightError("active project cache must be an object")
    keys = set(parsed)
    if not _PROJECT_CACHE_REQUIRED_KEYS <= keys or not keys <= _PROJECT_CACHE_ALLOWED_KEYS:
        raise StructuralPreflightError("active project cache has an unexpected field shape")
    if not all(isinstance(parsed[key], str) for key in _PROJECT_CACHE_REQUIRED_KEYS):
        raise StructuralPreflightError("active project cache required fields must be strings")
    default_branch = parsed.get("defaultBranch", expected.default_branch)
    workspace_ids = parsed.get("activeWorkspaceIds", list(expected.active_workspace_ids))
    if not isinstance(default_branch, str) or not isinstance(workspace_ids, list):
        raise StructuralPreflightError("active project cache optional fields have invalid types")
    if not all(isinstance(workspace_id, str) for workspace_id in workspace_ids):
        raise StructuralPreflightError("active project cache workspace IDs must be strings")
    try:
        observed = ActiveProjectBinding(
            project_id=parsed["projectId"],
            slug=parsed["slug"],
            name=parsed["name"],
            default_branch=default_branch,
            active_workspace_ids=tuple(workspace_ids),
        )
    except BindingValidationError as error:
        raise StructuralPreflightError("active project cache values are invalid") from error
    if observed != expected:
        raise StructuralPreflightError("active project cache does not match the requested binding")


@dataclass(frozen=True)
class StructuralPreflightReport:
    """Inspectable structural evidence, not proof of a live Claude session."""

    schema_version: int
    passed: bool
    binding_verified: bool
    artifact_sha256: str | None
    artifact_version: str | None
    stele_command_resolves_to_artifact: bool
    account_authenticated: bool
    doctor_checked_with_explicit_slug: bool
    plugin: dict[str, object] | None
    marketplace: dict[str, object] | None
    settings_allowlist: tuple[str, ...]
    hook_probes: tuple[str, ...]
    lifecycle_firing_verified: bool
    managed_permission_precedence_verified: bool
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


async def run_structural_preflight(
    runner: AgentCommandRunner,
    environment: Any,
    *,
    artifact_path: str,
    expected_artifact_sha256: str,
    binding: ActiveProjectBinding,
    project_dir: str = DEFAULT_EVALUATED_PROJECT_DIR,
) -> Path:
    """Verify installed artifacts without asking Claude Code to start a session."""

    report = StructuralPreflightReport(
        schema_version=1,
        passed=False,
        binding_verified=False,
        artifact_sha256=None,
        artifact_version=None,
        stele_command_resolves_to_artifact=False,
        account_authenticated=False,
        doctor_checked_with_explicit_slug=False,
        plugin=None,
        marketplace=None,
        settings_allowlist=(),
        hook_probes=(),
        lifecycle_firing_verified=False,
        managed_permission_precedence_verified=False,
    )
    try:
        artifact_sha256 = _first_token(
            await _command(runner, environment, f"sha256sum {shlex.quote(artifact_path)}")
        )
        if artifact_sha256 != expected_artifact_sha256:
            raise StructuralPreflightError("container artifact SHA-256 does not match")
        artifact_version = _version(
            await _command(runner, environment, f"{shlex.quote(artifact_path)} --version")
        )
        report = replace(
            report,
            artifact_sha256=artifact_sha256,
            artifact_version=artifact_version,
        )
        resolved_stele_command = (
            await _command(runner, environment, 'readlink -f "$(command -v stele)"')
        ).strip()
        if resolved_stele_command != artifact_path:
            raise StructuralPreflightError(
                "stele command does not resolve to the uploaded artifact"
            )
        report = replace(report, stele_command_resolves_to_artifact=True)

        account = _json_object(
            await _command(
                runner, environment, f"{shlex.quote(artifact_path)} account status --json"
            ),
            "account status",
        )
        if account.get("authenticated") is not True:
            raise StructuralPreflightError("account status is not authenticated")
        report = replace(report, account_authenticated=True)

        await _command(
            runner,
            environment,
            f"{shlex.quote(artifact_path)} --project {shlex.quote(binding.slug)} doctor --json",
            cwd=project_dir,
        )
        report = replace(report, doctor_checked_with_explicit_slug=True)

        validate_project_cache(
            await _command(
                runner,
                environment,
                f"cat {project_dir}/.stele/project.json",
                cwd=project_dir,
            ),
            binding,
        )
        report = replace(report, binding_verified=True)

        plugins = _json_list(
            await _command(
                runner,
                environment,
                with_claude_local_bin("claude plugin list --json"),
            ),
            "plugin list",
        )
        plugin = _find_plugin(plugins)
        install_path = plugin.get("installPath")
        if not isinstance(install_path, str) or not install_path.startswith("/"):
            raise StructuralPreflightError("plugin install path is missing")
        plugin_version = plugin.get("version")
        if plugin.get("enabled") is not True or not isinstance(plugin_version, str):
            raise StructuralPreflightError("plugin is not enabled with a version")
        report = replace(
            report,
            plugin={
                "id": plugin["id"],
                "expected_version": artifact_version,
                "observed_version": plugin_version,
                "enabled": plugin["enabled"],
            },
        )
        if plugin_version != artifact_version:
            raise StructuralPreflightError("plugin version does not match uploaded artifact")
        mcp = plugin.get("mcpServers")
        if not isinstance(mcp, dict) or not _has_expected_mcp(mcp):
            raise StructuralPreflightError("plugin MCP command or arguments do not match")
        await _command(runner, environment, f"test -d {shlex.quote(install_path)}")

        marketplaces = _json_list(
            await _command(
                runner,
                environment,
                with_claude_local_bin("claude plugin marketplace list --json"),
            ),
            "marketplace list",
        )
        marketplace = _find_marketplace(marketplaces)
        marketplace_path = marketplace.get("installLocation")
        if not isinstance(marketplace_path, str) or not marketplace_path.startswith("/"):
            raise StructuralPreflightError("marketplace install location is missing")
        await _command(runner, environment, f"test -d {shlex.quote(marketplace_path)}")

        details = await _command(
            runner,
            environment,
            with_claude_local_bin(f"claude plugin details {_EXPECTED_PLUGIN_ID}"),
        )
        if not all(hook in details for hook in _EXPECTED_HOOK_GROUPS):
            raise StructuralPreflightError("plugin details do not declare expected hook groups")

        allow_entries = _settings_allowlist(
            await _command(
                runner,
                environment,
                _settings_allowlist_command(),
            )
        )
        if not all(entry in allow_entries for entry in _EXPECTED_ALLOWLIST) or not any(
            entry.startswith("mcp__plugin_stele_stele__") for entry in allow_entries
        ):
            raise StructuralPreflightError("Claude settings allowlist is incomplete")

        for hook in _HOOK_PROBES:
            await _command(
                runner,
                environment,
                _hook_probe_command(artifact_path, hook, project_dir),
                cwd=project_dir,
            )

        report = replace(
            report,
            passed=True,
            binding_verified=True,
            plugin={
                "id": plugin["id"],
                "expected_version": artifact_version,
                "observed_version": plugin_version,
                "enabled": plugin["enabled"],
                "install_path": install_path,
                "mcp_command": _EXPECTED_MCP_COMMAND,
                "mcp_args": list(_EXPECTED_MCP_ARGS),
            },
            marketplace={"name": marketplace["name"], "install_path": marketplace_path},
            settings_allowlist=allow_entries,
            hook_probes=_HOOK_PROBES,
        )
    except BaseException as error:
        report = StructuralPreflightReport(
            **{**report.__dict__, "error": _safe_error(error)}
        )
        path = _write_report(runner.logs_dir, report)
        raise StructuralPreflightError("structural preflight failed", report_path=path) from error
    return _write_report(runner.logs_dir, report)


async def _command(
    runner: AgentCommandRunner, environment: Any, command: str, **kwargs: Any
) -> str:
    result = await runner.exec_as_agent(environment, command=command, **kwargs)
    return getattr(result, "stdout", "") or ""


def _json_object(raw: str, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise StructuralPreflightError(f"{label} did not return JSON") from error
    if not isinstance(parsed, dict):
        raise StructuralPreflightError(f"{label} did not return an object")
    return parsed


def _json_list(raw: str, label: str) -> list[object]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise StructuralPreflightError(f"{label} did not return JSON") from error
    if not isinstance(parsed, list):
        raise StructuralPreflightError(f"{label} did not return an array")
    return parsed


def _first_token(raw: str) -> str:
    value = raw.strip().split(maxsplit=1)
    if len(value) != 2 or not re.fullmatch(r"[a-f0-9]{64}", value[0]):
        raise StructuralPreflightError("artifact digest output is malformed")
    return value[0]


def _version(raw: str) -> str:
    """Normalize the CLI's single-line version output without guessing semver."""

    value = raw.strip()
    if value.startswith("stele "):
        value = value.removeprefix("stele ")
    if not value or "\n" in value or any(ord(character) < 32 for character in value):
        raise StructuralPreflightError("uploaded artifact version output is malformed")
    return value


def _settings_allowlist_command() -> str:
    return 'cat "$HOME/.claude/settings.json"'


def _settings_allowlist(raw: str) -> tuple[str, ...]:
    settings = _json_object(raw, "Claude settings")
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        raise StructuralPreflightError("Claude settings permissions must be an object")
    allowlist = permissions.get("allow")
    if not isinstance(allowlist, list) or not all(
        isinstance(entry, str) for entry in allowlist
    ):
        raise StructuralPreflightError("Claude settings allowlist must be an array of strings")
    return tuple(allowlist)


def injection_channel_command(roots: Sequence[str] = VERIFY_ROOTS) -> str:
    """Count installed files still wiring the auto-RAG hook, and a sibling hook.

    Reported as counts rather than a boolean so the caller can distinguish "the
    strip removed the wiring" from "the install tree isn't there at all".

    `roots` are shell words, already quoted by the caller — they are fixed
    literals from this module and the adapter, never user input.
    """

    roots = " ".join(roots)
    return "\n".join(
        (
            "set -u",
            "present=''",
            f"for root in {roots}; do",
            '  if [ -d "$root" ]; then present="$present $root"; fi',
            "done",
            'if [ -z "$present" ]; then',
            "  printf 'roots=0 auto_rag=0 sibling=0\\n'",
            "  exit 0",
            "fi",
            # Word-splitting `$present` is deliberate: it is a list of roots this
            # script just built from a fixed literal set, never external input.
            # -R, not -r: recursion must FOLLOW symlinks. A symlinked copy that
            # `-r` skips is invisible here while still being live for the agent,
            # which reports a stripped channel that is in fact still injecting.
            # Search only Claude plugin manifests. Searching every file under
            # the runtime root is self-contaminating: Harbor records this probe
            # in trial.log, so the literal needle in the command itself looks
            # like surviving hook wiring.
            "manifests() { find -L $present -type f -path '*/.claude-plugin/plugin.json' 2>/dev/null; }",  # noqa: E501
            "count() { manifests | xargs grep -lIF -- \"$1\" 2>/dev/null | wc -l | tr -d ' '; }",
            f"auto_rag=$(count {shlex.quote(_AUTO_RAG_HOOK_NEEDLE)})",
            f"sibling=$(count {shlex.quote(_SIBLING_HOOK_NEEDLE)})",
            "roots=$(printf '%s' \"$present\" | wc -w | tr -d ' ')",
            "printf 'roots=%s auto_rag=%s sibling=%s\\n' \"$roots\" \"$auto_rag\" \"$sibling\"",
        )
    )


def strip_injection_channel_command() -> str:
    """Neutralise the auto-RAG hook wiring in the installed plugin, in place.

    This is the whole of the `faithful-norag` manipulation: everything else about
    the arm — artifact, graph, credentials, binding, tools, skills, memory block,
    every other lifecycle hook — is byte-identical to `faithful`.

    It only *applies* the change; `evaluate_injection_channel` is what proves it
    took. Deliberately separate: a strip that reports its own success is a strip
    that can lie by finding nothing and saying nothing.
    """

    roots = " ".join(INSTALL_ROOTS)
    substitution = f"s/{_AUTO_RAG_HOOK_NEEDLE}/{_AUTO_RAG_NEUTRALISED}/g"
    return "\n".join(
        (
            "set -eu",
            "present=''",
            f"for root in {roots}; do",
            '  if [ -d "$root" ]; then present="$present $root"; fi',
            "done",
            'if [ -z "$present" ]; then',
            '  echo "no Stele install root was found to strip" >&2',
            "  exit 1",
            "fi",
            # -R matches the verifier: see every copy, including symlinked ones.
            # Writing through a symlink is then refused below rather than
            # skipped, so an install shape we did not anticipate aborts the arm
            # instead of quietly producing a half-stripped one.
            "manifests=$(find -L $present -type f -path '*/.claude-plugin/plugin.json' 2>/dev/null)",  # noqa: E501
            f"matches=$(printf '%s\\n' \"$manifests\" | xargs grep -lIF -- "
            f"{shlex.quote(_AUTO_RAG_HOOK_NEEDLE)} 2>/dev/null || true)",
            'if [ -z "$matches" ]; then',
            '  echo "auto-RAG hook wiring was already absent" >&2',
            "  exit 1",
            "fi",
            # The while loop is the last stage of the pipeline, so a failure
            # inside it becomes the pipeline's status and `set -e` aborts.
            'printf "%s\\n" "$matches" | while IFS= read -r file; do',
            '  [ -n "$file" ] || continue',
            '  if [ -L "$file" ]; then',
            '    echo "refusing to rewrite a symlinked plugin file" >&2',
            "    exit 1",
            "  fi",
            # Not `sed -i`: that spelling means different things to GNU and BSD
            # sed, and this command has to stay runnable outside the container
            # for its own test. Writing through `cat` also keeps the original
            # inode and mode rather than replacing the file.
            '  scratch="$file.stele-bench-norag"',
            '  if [ -e "$scratch" ]; then',
            '    echo "refusing to clobber an existing scratch file" >&2',
            "    exit 1",
            "  fi",
            f'  sed {shlex.quote(substitution)} "$file" > "$scratch"',
            '  cat "$scratch" > "$file"',
            '  rm -f "$scratch"',
            "done",
        )
    )


def parse_injection_channel(raw: str) -> dict[str, int]:
    """Parse `injection_channel_command` output into its three counts."""

    fields: dict[str, int] = {}
    for token in (raw or "").split():
        name, separator, value = token.partition("=")
        if separator and name in {"roots", "auto_rag", "sibling"}:
            try:
                fields[name] = int(value)
            except ValueError as error:
                raise StructuralPreflightError(
                    "injection-channel probe returned a non-numeric count"
                ) from error
    if set(fields) != {"roots", "auto_rag", "sibling"}:
        raise StructuralPreflightError("injection-channel probe returned an unexpected shape")
    return fields


def evaluate_injection_channel(
    counts: Mapping[str, int], expected: AutoRagInjection
) -> dict[str, object]:
    """Fail closed unless the observed wiring matches what the arm claims to be."""

    roots, auto_rag, sibling = counts["roots"], counts["auto_rag"], counts["sibling"]
    if roots == 0:
        raise StructuralPreflightError("no Stele install root was found to inspect")
    if sibling == 0:
        # Zero auto-RAG references would otherwise be indistinguishable from a
        # missing install, which would score as a valid `faithful-norag` arm.
        raise StructuralPreflightError("installed plugin does not wire any lifecycle hooks")
    if expected == "on" and auto_rag == 0:
        raise StructuralPreflightError(
            "auto-RAG injection is not wired but this arm requires it"
        )
    if expected == "off" and auto_rag != 0:
        raise StructuralPreflightError(
            "auto-RAG injection survived the strip but this arm forbids it"
        )
    return {
        "expected": expected,
        "install_roots": roots,
        "auto_rag_hook_files": auto_rag,
        "sibling_hook_files": sibling,
        "verified": True,
    }


def create_project_command(artifact_path: str, name: str) -> str:
    """Create a project the way a new user does, from inside the workspace."""

    return (
        f"{shlex.quote(artifact_path)} project create {shlex.quote(name)}"
    )


def parse_created_slug(stdout: str) -> str:
    """Pull the slug the CLI actually chose out of its success line."""

    match = _CREATED_PROJECT.search(_ANSI.sub("", stdout or ""))
    if match is None:
        raise StructuralPreflightError("project create did not report a slug")
    return match.group("slug")


async def verify_injection_channel(
    runner: AgentCommandRunner,
    environment: Any,
    *,
    roots: Sequence[str],
    expected: AutoRagInjection,
) -> Path:
    """Assert the arm's injection channel is what it claims, and record it.

    Run this AFTER the runtime config is staged: the harness reads the staged
    copy, so that is the tree whose wiring decides what the agent actually gets.
    """

    counts = parse_injection_channel(
        await _command(runner, environment, injection_channel_command(roots))
    )
    report = evaluate_injection_channel(counts, expected)
    logs_dir = runner.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / INJECTION_CHANNEL_REPORT_NAME
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def discover_workspace_command() -> str:
    """Ask the container where the agent actually starts.

    Run with no `cwd` override, so the shell lands in the image's own `WORKDIR`.
    That directory IS the evaluated project by definition — it is where the
    borrowed task put its files and where the agent will be working — which
    makes this both more correct and more general than reading a Dockerfile.
    """

    return "pwd"


def parse_workspace(stdout: str) -> str:
    """Validate the discovered working directory before it becomes a shell word.

    Everything downstream interpolates this into commands, so a value that is
    empty, relative, or multi-line is refused here rather than producing a
    confusing failure several commands later.
    """

    candidate = _ANSI.sub("", stdout or "").strip()
    if not candidate or not _ABSOLUTE_PATH.fullmatch(candidate):
        raise StructuralPreflightError(
            "could not determine the evaluated project directory in the container"
        )
    return candidate


def _hook_probe_command(
    artifact_path: str, hook: str, project_dir: str = DEFAULT_EVALUATED_PROJECT_DIR
) -> str:
    payload = json.dumps(
        {
            "session_id": "structural-preflight",
            "prompt": "status",
            "cwd": project_dir,
        },
        separators=(",", ":"),
    )
    return (
        f"printf '%s' {shlex.quote(payload)} | {shlex.quote(artifact_path)} hook "
        f"{shlex.quote(hook)} --harness=claude"
    )


def _find_plugin(items: list[object]) -> dict[str, object]:
    for item in items:
        if isinstance(item, dict) and item.get("id") == _EXPECTED_PLUGIN_ID:
            return item
    raise StructuralPreflightError("expected Claude plugin is absent")


def _find_marketplace(items: list[object]) -> dict[str, object]:
    for item in items:
        if isinstance(item, dict) and item.get("name") == _EXPECTED_MARKETPLACE:
            return item
    raise StructuralPreflightError("expected Claude marketplace is absent")


def _has_expected_mcp(servers: Mapping[str, object]) -> bool:
    candidate = servers.get("stele")
    return (
        isinstance(candidate, dict)
        and candidate.get("command") == _EXPECTED_MCP_COMMAND
        and candidate.get("args") == list(_EXPECTED_MCP_ARGS)
    )


def _safe_error(error: BaseException) -> str:
    return error.__class__.__name__


def _write_report(logs_dir: Path, report: StructuralPreflightReport) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "preflight.json"
    path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path
