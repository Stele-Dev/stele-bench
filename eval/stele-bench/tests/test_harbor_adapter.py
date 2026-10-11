from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from stele_bench.harbor_adapter import (
    ArtifactVerificationError,
    AttemptState,
    LocalDirtyWorktreeArtifact,
    ReleasedArtifact,
    _make_harbor_adapter,
    _parse_agent_home,
    merge_json_objects,
    select_artifact,
)
from stele_bench.preflight import (
    EVALUATED_PROJECT_DIR,
    ActiveProjectBinding,
    StructuralPreflightError,
    validate_project_cache,
)

PROJECT_BINDING = ActiveProjectBinding(
    project_id="11111111-1111-4111-8111-111111111111",
    slug="bound-project",
    name="Bound Project",
    default_branch="main",
    active_workspace_ids=("22222222-2222-4222-8222-222222222222",),
)
CLAUDE_LOCAL_BIN_PREFIX = 'export PATH="$HOME/.local/bin:$PATH"; '


@dataclass
class Result:
    stdout: str
    return_code: int = 0


class FakeEnvironment:
    CREATED_SLUG = "bound-project"

    def __init__(
        self,
        *,
        fail_preflight: bool = False,
        plugin_version: str = "1.2.3",
        require_claude_local_bin: bool = False,
        stele_command_path: str = "/opt/agent-tools/stele",
        claude_settings_json: str | None = None,
    ) -> None:
        self.commands: list[tuple[str, str | None]] = []
        self.command_cwds: list[tuple[str, str | None]] = []
        self.uploads: list[tuple[Path, str, int]] = []
        self.uploaded_contents: dict[str, bytes] = {}
        self.fail_preflight = fail_preflight
        self.plugin_version = plugin_version
        self.require_claude_local_bin = require_claude_local_bin
        self.stele_command_path = stele_command_path
        self.claude_settings_json = claude_settings_json or json.dumps(
            {
                "permissions": {
                    "allow": [
                        "Bash(stele account status:*)",
                        "mcp__plugin_stele_stele__search",
                    ]
                }
            }
        )
        self.artifact_sha256: str | None = None
        # A real install scatters the hook wiring across the synthesized
        # marketplace and the plugin's own cache directory, so the fake starts
        # with more than one file carrying it — a fake with exactly one would
        # hide a strip that only ever rewrites the first match it finds.
        self.install_roots = 2
        self.auto_rag_hook_files = 2
        self.sibling_hook_files = 2
        self.strips = 0
        # What `project create` reports, and what the directory ends up bound
        # to. Separate fields so a test can make them disagree.
        self.created_slug: str | None = FakeEnvironment.CREATED_SLUG
        self.bound_slug: str | None = None
        # Where the image says the agent starts. Our generated pack uses
        # /workspace; a borrowed image can use anything.
        self.workspace = EVALUATED_PROJECT_DIR

    async def exec(self, command: str, **kwargs: Any) -> Result:
        self.commands.append((command, kwargs.get("user")))
        self.command_cwds.append((command, kwargs.get("cwd")))
        if command == "pwd":
            return Result(f"{self.workspace}\n")
        if "auto_rag=%s sibling=%s" in command:
            return Result(
                f"roots={self.install_roots} auto_rag={self.auto_rag_hook_files} "
                f"sibling={self.sibling_hook_files}\n"
            )
        if "project create" in command:
            if self.created_slug is None:
                return Result("nothing useful\n")
            self.bound_slug = self.bound_slug or self.created_slug
            return Result(f"\u2713 Created project {self.created_slug} (id=abc)\n")
        if "refusing to rewrite a symlinked plugin file" in command:
            self.strips += 1
            if self.auto_rag_hook_files == 0:
                # The real script exits non-zero rather than silently no-opping.
                raise RuntimeError("auto-RAG hook wiring was already absent")
            self.auto_rag_hook_files = 0
            return Result("")
        if self.require_claude_local_bin and (
            "claude plugin" in command or "install --only=claude" in command
        ):
            if not command.startswith(CLAUDE_LOCAL_BIN_PREFIX):
                raise RuntimeError("claude is not on the default PATH")
        if "sha256sum" in command:
            return Result(f"{self.artifact_sha256}  /opt/agent-tools/stele\n")
        if "command -v stele" in command:
            return Result(f"{self.stele_command_path}\n")
        if command.endswith("--version"):
            return Result("stele 1.2.3\n")
        if "id -u" in command:
            return Result("/home/agent\n1000:1000")
        if "account status --json" in command:
            return Result(json.dumps({"authenticated": not self.fail_preflight}))
        if "doctor --json" in command:
            return Result("{}")
        if command == f"cat {self.workspace}/.stele/project.json":
            return Result(
                json.dumps(
                    {
                        "projectId": PROJECT_BINDING.project_id,
                        "slug": self.bound_slug or PROJECT_BINDING.slug,
                        "name": PROJECT_BINDING.name,
                    }
                )
            )
        if "settings.json" in command and (
            command == 'cat "$HOME/.claude/settings.json"' or command.startswith("if [ -f ")
        ):
            return Result(self.claude_settings_json)
        if command.endswith("claude plugin list --json"):
            return Result(
                json.dumps(
                    [
                        {
                            "id": "stele@stele-local",
                            "version": self.plugin_version,
                            "enabled": True,
                            "installPath": (
                                "/home/agent/.claude/plugins/cache/"
                                "stele-local/stele/1.2.3"
                            ),
                            "mcpServers": {
                                "stele": {
                                    "command": "stele",
                                    "args": ["mcp", "--harness", "claude-code"],
                                }
                            },
                        }
                    ]
                )
            )
        if command.endswith("claude plugin marketplace list --json"):
            return Result(
                json.dumps(
                    [
                        {
                            "name": "stele-local",
                            "installLocation": "/home/agent/.stele/claude-marketplace",
                        }
                    ]
                )
            )
        if command.endswith("claude plugin details stele@stele-local"):
            return Result("SessionStart UserPromptSubmit PreToolUse PostToolUse PreCompact")
        return Result("")

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        self.uploads.append((source, target_path, source.stat().st_mode & 0o777))
        self.uploaded_contents[target_path] = source.read_bytes()
        if target_path == "/opt/agent-tools/stele":
            self.artifact_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        if target_path == "/home/agent/.claude/settings.json":
            self.claude_settings_json = source.read_text(encoding="utf-8")


class FakeClaudeCode:
    def __init__(self, logs_dir: Path, **_: Any) -> None:
        self.logs_dir = logs_dir
        self.stock_setup_calls = 0
        self.agent_run_calls = 0

    async def setup(self, environment: FakeEnvironment) -> None:
        self.stock_setup_calls += 1

    async def run(self, *_: Any, **__: Any) -> None:
        self.agent_run_calls += 1

    async def exec_as_root(
        self, environment: FakeEnvironment, command: str, **kwargs: Any
    ) -> Result:
        return await environment.exec(command, user="root", **kwargs)

    async def exec_as_agent(
        self, environment: FakeEnvironment, command: str, **kwargs: Any
    ) -> Result:
        return await environment.exec(command, user="agent", **kwargs)


def _artifact(path: Path, *, local: bool = False):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if local:
        return LocalDirtyWorktreeArtifact(path=path, sha256=digest)
    return ReleasedArtifact(path=path, sha256=digest, release_version="1.2.3")


def _adapter(
    tmp_path: Path,
    artifact,
    *,
    patch: dict[str, Any] | None = None,
    auto_rag_injection: str = "on",
    project_provisioning: str = "restore",
):
    adapter_class = _make_harbor_adapter(FakeClaudeCode)
    return adapter_class(
        tmp_path / "logs",
        artifact=artifact,
        attempt_state=AttemptState(b'{"token":"secret"}', b'{"theme":"dark"}'),
        project_binding=PROJECT_BINDING if project_provisioning == "restore" else None,
        project_provisioning=project_provisioning,
        claude_settings_patch=patch,
        auto_rag_injection=auto_rag_injection,
    )


def test_selects_local_artifact_and_preserves_path_free_provenance(tmp_path: Path) -> None:
    release_file = tmp_path / "release-cli"
    local_file = tmp_path / "local-cli"
    release_file.write_bytes(b"release")
    local_file.write_bytes(b"local")
    released = _artifact(release_file)
    local = _artifact(local_file, local=True)

    assert select_artifact(released=released) is released
    assert select_artifact(released=released, local_dirty_worktree=local) is local
    assert local.provenance.source_kind == "local-dirty-worktree"
    assert str(local_file) not in repr(local.provenance)


def test_project_cache_accepts_the_authoritative_minimal_shape() -> None:
    validate_project_cache(
        json.dumps(
            {
                "projectId": PROJECT_BINDING.project_id,
                "slug": PROJECT_BINDING.slug,
                "name": PROJECT_BINDING.name,
            }
        ),
        PROJECT_BINDING,
    )


@pytest.mark.parametrize(
    "change",
    (
        {"slug": "other-project"},
        {"defaultBranch": "trunk"},
        {"activeWorkspaceIds": ["33333333-3333-4333-8333-333333333333"]},
    ),
)
def test_project_cache_rejects_mismatched_required_or_optional_values(
    change: dict[str, object],
) -> None:
    cache = {
        "projectId": PROJECT_BINDING.project_id,
        "slug": PROJECT_BINDING.slug,
        "name": PROJECT_BINDING.name,
        **change,
    }

    with pytest.raises(StructuralPreflightError, match="does not match"):
        validate_project_cache(json.dumps(cache), PROJECT_BINDING)


def test_project_cache_rejects_unknown_fields() -> None:
    cache = {
        "projectId": PROJECT_BINDING.project_id,
        "slug": PROJECT_BINDING.slug,
        "name": PROJECT_BINDING.name,
        "fixtureMetadata": "forbidden",
    }

    with pytest.raises(StructuralPreflightError, match="field shape"):
        validate_project_cache(json.dumps(cache), PROJECT_BINDING)


def test_hash_mismatch_fails_before_any_upload(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"first")
    artifact = _artifact(binary)
    binary.write_bytes(b"mutated")
    environment = FakeEnvironment()

    with pytest.raises(ArtifactVerificationError):
        adapter = _adapter(tmp_path, artifact)
        asyncio.run(adapter.setup(environment))

    assert environment.uploads == []
    assert adapter.stock_setup_calls == 0


def test_setup_uses_stock_install_neutral_paths_and_private_attempt_state(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    assert adapter.stock_setup_calls == 1
    assert [target for _, target, _ in environment.uploads] == [
        "/opt/agent-tools/stele",
        "/home/agent/.config/stele/credentials.json",
        "/home/agent/.config/stele/preferences.json",
    ]
    assert all(mode == 0o600 for _, target, mode in environment.uploads[1:])
    commands = [command for command, _ in environment.commands]
    assert any(command.endswith("project use bound-project") for command in commands)
    assert any("install --only=claude --yes --json" in command for command in commands)
    assert any("chmod 0600" in command for command in commands)
    assert all("secret" not in command for command in commands)
    assert all("/root/project" not in command for command in commands)
    assert all(cwd != "/root/project" for _, cwd in environment.command_cwds)
    assert any(
        command.endswith("project use bound-project") and cwd == EVALUATED_PROJECT_DIR
        for command, cwd in environment.command_cwds
    )
    assert adapter.preflight_report_path == tmp_path / "logs" / "preflight.json"
    assert adapter.preflight_report_path.is_file()
    report = json.loads(adapter.preflight_report_path.read_text(encoding="utf-8"))
    assert report["artifact_version"] == "1.2.3"
    assert report["stele_command_resolves_to_artifact"] is True
    assert report["plugin"]["expected_version"] == "1.2.3"
    assert report["plugin"]["observed_version"] == "1.2.3"
    assert "11111111-1111-4111-8111-111111111111" not in json.dumps(report)


def test_setup_and_preflight_find_claude_without_a_login_shell(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment(require_claude_local_bin=True)

    asyncio.run(adapter.setup(environment))

    commands = [command for command, _ in environment.commands]
    discovery_commands = [
        command
        for command in commands
        if "install --only=claude" in command or "claude plugin" in command
    ]
    assert discovery_commands
    assert all(command.startswith(CLAUDE_LOCAL_BIN_PREFIX) for command in discovery_commands)


def test_setup_stages_installed_plugin_state_after_preflight(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    commands = [command for command, _ in environment.commands]
    install_index = next(
        index for index, command in enumerate(commands) if "install --only=claude" in command
    )
    preflight_index = next(
        index
        for index, command in enumerate(commands)
        if command.endswith("claude plugin details stele@stele-local")
    )
    stage_index = next(
        index for index, command in enumerate(commands) if "cp -a --" in command
    )

    injection_index = next(
        index for index, command in enumerate(commands) if "auto_rag=%s sibling=%s" in command
    )

    # The injection-channel check comes last on purpose: the harness reads the
    # STAGED copy at run time, so verifying before `cp -a` would inspect a tree
    # the agent never uses.
    assert install_index < preflight_index < stage_index < injection_index
    assert injection_index == len(commands) - 1
    stage_command, stage_user = environment.commands[stage_index]
    assert stage_user == "agent"
    assert "test -d /home/agent/.claude/plugins" in stage_command
    assert (
        "cp -a -- /home/agent/.claude/plugins /logs/agent/sessions/plugins"
        in stage_command
    )
    assert (
        "cp -a -- /home/agent/.claude/settings.json "
        "/logs/agent/sessions/settings.json"
    ) in stage_command
    assert "installed_plugins.json" in stage_command
    assert "test ! -e /logs/agent/sessions/plugins" in stage_command
    assert "test ! -e /logs/agent/sessions/settings.json" in stage_command
    assert "credentials" not in stage_command
    assert "/home/agent/.claude /logs/agent/sessions" not in stage_command


def test_setup_stages_the_stele_instructions_the_install_wrote(tmp_path: Path) -> None:
    # Harbor runs Claude with CLAUDE_CONFIG_DIR at the runtime dir. A user-level
    # CLAUDE.md left in ~/.claude never reaches the agent, and without its Stele
    # block the agent is never told to activate recall.
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    stage_command = next(
        command for command, _ in environment.commands if "cp -a --" in command
    )
    assert "test -f /home/agent/.claude/CLAUDE.md" in stage_command
    assert "test ! -L /home/agent/.claude/CLAUDE.md" in stage_command
    assert "test ! -e /logs/agent/sessions/CLAUDE.md" in stage_command
    assert (
        "cp -a -- /home/agent/.claude/CLAUDE.md /logs/agent/sessions/CLAUDE.md"
        in stage_command
    )
    # Setup fails unless the staged copy carries both fences of the Stele block.
    assert (
        "grep -qF -- '======== Stele: Important Project Context  ===========' "
        "/logs/agent/sessions/CLAUDE.md"
    ) in stage_command
    assert (
        "grep -qF -- '======== Stele: Instructions End  ====================' "
        "/logs/agent/sessions/CLAUDE.md"
    ) in stage_command
    assert stage_command.splitlines()[0] == "set -eu"


def test_runtime_config_staging_does_not_depend_on_a_login_shell(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment(require_claude_local_bin=True)

    asyncio.run(adapter.setup(environment))

    stage_command = next(
        command for command, _ in environment.commands if "cp -a --" in command
    )
    assert not stage_command.startswith(CLAUDE_LOCAL_BIN_PREFIX)
    assert ".bashrc" not in stage_command
    assert "source " not in stage_command


def test_setup_installs_a_guarded_root_owned_stele_command_link(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    root_commands = [
        command for command, user in environment.commands if user == "root"
    ]
    link_command = next(
        command for command in root_commands if "ln -s /opt/agent-tools/stele" in command
    )
    assert "[ -e /usr/local/bin/stele ] || [ -L /usr/local/bin/stele ]" in link_command
    assert "refusing to replace an unexpected stele command" in link_command
    assert "chown -h root:root /usr/local/bin/stele" in link_command
    agent_commands = [command for command, user in environment.commands if user == "agent"]
    assert 'readlink -f "$(command -v stele)"' in agent_commands


def test_preflight_rejects_a_stele_command_that_is_not_the_uploaded_artifact(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment(stele_command_path="/usr/bin/stele")

    with pytest.raises(StructuralPreflightError, match="structural preflight failed"):
        asyncio.run(adapter.setup(environment))

    report = json.loads((tmp_path / "logs" / "preflight.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["stele_command_resolves_to_artifact"] is False


def test_settings_patch_is_merged_host_side_without_node(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(
        tmp_path,
        _artifact(binary),
        patch={
            "permissions": {
                "allow": [
                    "Bash(stele account status:*)",
                    "mcp__plugin_stele_stele__search",
                ]
            },
            "feature": True,
        },
    )
    environment = FakeEnvironment(
        claude_settings_json=json.dumps(
            {"permissions": {"deny": ["Bash"]}, "existing": True}
        )
    )

    asyncio.run(adapter.setup(environment))

    assert environment.uploads[-1][1] == "/home/agent/.claude/settings.json"
    assert environment.uploads[-1][2] == 0o600
    commands = [command for command, _ in environment.commands]
    assert not any("node" in command for command in commands)
    merged = json.loads(environment.uploaded_contents["/home/agent/.claude/settings.json"])
    assert merged == {
        "existing": True,
        "feature": True,
        "permissions": {
            "allow": [
                "Bash(stele account status:*)",
                "mcp__plugin_stele_stele__search",
            ],
            "deny": ["Bash"],
        },
    }


@pytest.mark.parametrize(
    "settings_json",
    ("not-json", json.dumps({"permissions": {"allow": "not-a-list"}})),
)
def test_preflight_rejects_malformed_claude_settings_without_node(
    tmp_path: Path, settings_json: str
) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment(claude_settings_json=settings_json)

    with pytest.raises(StructuralPreflightError, match="structural preflight failed"):
        asyncio.run(adapter.setup(environment))

    commands = [command for command, _ in environment.commands]
    assert 'cat "$HOME/.claude/settings.json"' in commands
    assert not any("node" in command for command in commands)


def test_host_side_settings_merge_rejects_malformed_existing_json(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), patch={"feature": True})
    environment = FakeEnvironment(claude_settings_json="not-json")

    with pytest.raises(ValueError, match="Claude settings must be valid JSON"):
        asyncio.run(adapter.setup(environment))

    assert "/home/agent/.claude/settings.json" not in environment.uploaded_contents
    commands = [command for command, _ in environment.commands]
    assert not any("node" in command for command in commands)


def test_commands_quote_dynamic_container_paths_and_never_embed_state(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    commands = [command for command, _ in environment.commands]
    assert all("agent-state-" not in command for command in commands)
    assert all("credentials_json" not in command for command in commands)
    assert all("token" not in command for command in commands)
    assert all("; rm -rf" not in command for command in commands)


def test_rejects_an_unsafe_agent_home_before_it_can_be_interpolated() -> None:
    with pytest.raises(RuntimeError, match="safe default-agent home"):
        _parse_agent_home(Result("/home/agent; touch /tmp/pwned\n1000:1000"))


def test_merge_json_objects_preserves_unmentioned_branches() -> None:
    assert merge_json_objects(
        {"permissions": {"deny": ["Bash"]}, "existing": True},
        {"permissions": {"allow": ["Read"]}},
    ) == {
        "permissions": {"deny": ["Bash"], "allow": ["Read"]},
        "existing": True,
    }


def test_setup_only_prepares_the_agent_and_leaves_stock_run_path_intact(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    assert "run" not in type(adapter).__dict__
    assert adapter.agent_run_calls == 0


def test_structural_preflight_failure_writes_a_failed_report(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment(fail_preflight=True)

    with pytest.raises(StructuralPreflightError) as error:
        asyncio.run(adapter.setup(environment))

    assert error.value.report_path == tmp_path / "logs" / "preflight.json"
    report = json.loads(error.value.report_path.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["lifecycle_firing_verified"] is False
    assert report["managed_permission_precedence_verified"] is False


def test_structural_preflight_rejects_plugin_version_mismatch(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment(plugin_version="9.9.9")

    with pytest.raises(StructuralPreflightError, match="structural preflight failed"):
        asyncio.run(adapter.setup(environment))

    report = json.loads((tmp_path / "logs" / "preflight.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["artifact_version"] == "1.2.3"
    assert report["plugin"]["expected_version"] == "1.2.3"
    assert report["plugin"]["observed_version"] == "9.9.9"


# ── auto-RAG as an arm dimension (TASK-2497) ─────────────────────────────────
#
# The `faithful-norag` arm differs from `faithful` in exactly one way, so these
# tests are mostly about the failure modes: a strip that silently did nothing,
# and an arm that lost its injection channel without asking to. Both would
# produce a plausible-looking "auto-RAG made no difference" result.


def test_faithful_arm_does_not_touch_the_injection_channel(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    assert environment.strips == 0
    assert environment.auto_rag_hook_files == 2
    report = json.loads(
        (tmp_path / "logs" / "injection-channel.json").read_text(encoding="utf-8")
    )
    assert report == {
        "expected": "on",
        "install_roots": 2,
        "auto_rag_hook_files": 2,
        "sibling_hook_files": 2,
        "verified": True,
    }


def test_norag_arm_strips_every_copy_and_records_the_evidence(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), auto_rag_injection="off")
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    assert environment.strips == 1
    assert environment.auto_rag_hook_files == 0
    # The sibling hook surviving is what separates "stripped" from "the install
    # never happened" — a zero count alone would satisfy both.
    assert environment.sibling_hook_files == 2
    report = json.loads(
        (tmp_path / "logs" / "injection-channel.json").read_text(encoding="utf-8")
    )
    assert report == {
        "expected": "off",
        "install_roots": 2,
        "auto_rag_hook_files": 0,
        "sibling_hook_files": 2,
        "verified": True,
    }


def test_norag_arm_fails_closed_when_the_strip_did_not_take(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), auto_rag_injection="off")
    environment = FakeEnvironment()
    # Simulate a strip that ran but missed a copy — e.g. a fourth install
    # location nobody knew about. Setup must not proceed to a scored trial.
    environment.auto_rag_hook_files = 1

    def _refuse_to_clear(command: str) -> None:
        environment.auto_rag_hook_files = 1

    original = environment.exec

    async def exec_without_effect(command: str, **kwargs: Any):
        result = await original(command, **kwargs)
        if "refusing to rewrite a symlinked plugin file" in command:
            _refuse_to_clear(command)
        return result

    environment.exec = exec_without_effect  # type: ignore[method-assign]

    with pytest.raises(StructuralPreflightError, match="survived the strip"):
        asyncio.run(adapter.setup(environment))


def test_faithful_arm_fails_closed_when_injection_is_missing(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary))
    environment = FakeEnvironment()
    # The dangerous direction: an arm that was supposed to have injection and
    # quietly lost it scores as evidence AGAINST the feature.
    environment.auto_rag_hook_files = 0

    with pytest.raises(StructuralPreflightError, match="requires it"):
        asyncio.run(adapter.setup(environment))


def test_injection_check_rejects_a_vanished_install(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), auto_rag_injection="off")
    environment = FakeEnvironment()
    # The strip itself succeeds; what's gone is everything else. Without the
    # sibling-hook check this would read as a perfectly good `faithful-norag`
    # arm, because "no auto-RAG wiring" is exactly what it looks like.
    environment.sibling_hook_files = 0

    with pytest.raises(StructuralPreflightError, match="does not wire any lifecycle hooks"):
        asyncio.run(adapter.setup(environment))


def test_adapter_rejects_an_unknown_injection_setting(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")

    with pytest.raises(ValueError, match="auto_rag_injection must be on or off"):
        _adapter(tmp_path, _artifact(binary), auto_rag_injection="maybe")


# ── create-mode provisioning (borrowed-task / overhead arm) ──────────────────
#
# The arm for tasks with no memory intent does not restore a frozen graph: it
# makes a project the way a new user does, starter nodes and all.


def test_create_mode_creates_a_project_and_binds_the_directory(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), project_provisioning="create")
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    commands = [command for command, _ in environment.commands]
    create_index = next(i for i, c in enumerate(commands) if "project create" in c)
    use_index = next(i for i, c in enumerate(commands) if "project use" in c)
    # `project create` does NOT write the directory binding; `project use` does.
    # Without the second step the agent runs unbound, which is indistinguishable
    # from having no Stele at all — the exact failure this arm must not have.
    assert create_index < use_index
    assert f"project use {FakeEnvironment.CREATED_SLUG}" in commands[use_index]


def test_create_mode_records_the_project_for_the_host_to_read(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), project_provisioning="create")
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    report = json.loads(
        (tmp_path / "logs" / "created-project.json").read_text(encoding="utf-8")
    )
    # The host reads the graph after the trial to see what the agent actually
    # persisted; it needs the slug the container chose to find it.
    assert report["slug"] == FakeEnvironment.CREATED_SLUG
    assert report["project_id"] == PROJECT_BINDING.project_id
    assert adapter.project_binding is not None
    assert report["binding"] == adapter.project_binding.as_cache_object()


def test_create_mode_takes_the_slug_the_cli_chose_not_the_one_we_guessed(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), project_provisioning="create")
    environment = FakeEnvironment()
    # Every task in a suite uses the same project name, so the CLI appends a
    # suffix on collision. Deriving the slug from the name would bind to
    # somebody else's project.
    environment.created_slug = "bound-project-7"

    asyncio.run(adapter.setup(environment))

    assert any("project use bound-project-7" in c for c, _ in environment.commands)


def test_create_mode_fails_closed_when_the_binding_disagrees(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), project_provisioning="create")
    environment = FakeEnvironment()
    environment.bound_slug = "some-other-project"

    with pytest.raises(StructuralPreflightError, match="different project"):
        asyncio.run(adapter.setup(environment))


def test_create_mode_fails_closed_when_no_slug_is_reported(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), project_provisioning="create")
    environment = FakeEnvironment()
    environment.created_slug = None

    with pytest.raises(StructuralPreflightError, match="did not report a slug"):
        asyncio.run(adapter.setup(environment))


def test_restore_mode_still_requires_a_binding(tmp_path: Path) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")

    with pytest.raises(ValueError, match="requires a binding"):
        _make_harbor_adapter(FakeClaudeCode)(
            tmp_path / "logs",
            artifact=_artifact(binary),
            attempt_state=AttemptState(b"{}", b"{}"),
            project_binding=None,
        )


def test_setup_follows_the_image_workspace_rather_than_assuming_ours(
    tmp_path: Path,
) -> None:
    """A borrowed image puts the project where its own Dockerfile says.

    `aider-polyglot` uses `WORKDIR /app`. Assuming `/workspace` made every
    command that runs inside the evaluated project fail with `chdir to cwd
    ("/workspace") ... no such file or directory` — and it failed at `stele
    project create`, i.e. *after* a successful install, so it read like a Stele
    bug rather than a wrong assumption about somebody else's image.
    """

    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), project_provisioning="create")
    environment = FakeEnvironment()
    environment.workspace = "/app"

    asyncio.run(adapter.setup(environment))

    assert adapter.evaluated_project_dir == "/app"
    inside = [cwd for _command, cwd in environment.command_cwds if cwd is not None]
    assert inside, "no command ran inside the evaluated project"
    assert set(inside) == {"/app"}
    assert not any(cwd == EVALUATED_PROJECT_DIR for cwd in inside)


def test_setup_still_uses_our_own_pack_workspace_when_that_is_what_the_image_says(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), project_provisioning="create")
    environment = FakeEnvironment()

    asyncio.run(adapter.setup(environment))

    assert adapter.evaluated_project_dir == EVALUATED_PROJECT_DIR


@pytest.mark.parametrize("reported", ["", "   ", "relative/path", "/two\nlines"])
def test_setup_refuses_a_workspace_it_cannot_trust(tmp_path: Path, reported: str) -> None:
    """The value becomes a shell word downstream, so it is validated here."""

    binary = tmp_path / "cli"
    binary.write_bytes(b"ready")
    adapter = _adapter(tmp_path, _artifact(binary), project_provisioning="create")
    environment = FakeEnvironment()
    environment.workspace = reported

    with pytest.raises(StructuralPreflightError, match="evaluated project directory"):
        asyncio.run(adapter.setup(environment))


def test_the_instructions_fences_match_the_stele_cli() -> None:
    # The fences are the CLI's contract. The public benchmark repository does not
    # carry the CLI source, so this check runs only inside the Stele monorepo.
    source = Path(__file__).resolve().parents[3] / "apps/cli/src/harness/_memory_block.ts"
    if not source.is_file():
        pytest.skip("Stele CLI source is not in this checkout")
    from stele_bench.harbor_adapter import _STELE_INSTRUCTIONS_MARKERS

    text = source.read_text(encoding="utf-8")
    for marker in _STELE_INSTRUCTIONS_MARKERS:
        assert f'"{marker}"' in text
