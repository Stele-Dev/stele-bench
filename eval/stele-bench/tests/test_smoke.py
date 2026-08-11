from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from stele_bench.cli import app
from stele_bench.contracts import (
    EvidenceCorpusDescriptor,
    FixtureManifestV1,
    GraphArm,
    GraphDescriptor,
    RunIdentity,
)
from stele_bench.fixtures import dump_fixture
from stele_bench.harbor_adapter import LocalDirtyWorktreeArtifact
from stele_bench.harbor_entrypoint import (
    HARBOR_AGENT_IMPORT_PATH,
    HarborConfigurationError,
    _make_configured_agent,
)
from stele_bench.preflight import ActiveProjectBinding, BindingValidationError
from stele_bench.provisioning import FakeProvisioner
from stele_bench.smoke import (
    InstallOnlySetup,
    SmokeCondition,
    SmokeConfigurationError,
    SmokeDependencies,
    SmokeMode,
    SmokeRequest,
    prepare_smoke,
    select_smoke_conditions,
)

runner = CliRunner()


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    (repository / "src").mkdir()
    (repository / "src" / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "pinned source")
    return repository, _git(repository, "rev-parse", "HEAD")


def _fixture(tmp_path: Path) -> tuple[Path, FixtureManifestV1]:
    fixture_root = tmp_path / "fixture"
    (fixture_root / "corpora").mkdir(parents=True)
    (fixture_root / "graphs").mkdir()
    evidence = fixture_root / "corpora" / "evidence.json"
    graph = fixture_root / "graphs" / "faithful.json"
    evidence.write_text("[]\n", encoding="utf-8")
    graph.write_text('{"graph":"faithful"}\n', encoding="utf-8")
    manifest = FixtureManifestV1(
        fixture_id="fixture-v1",
        cohort_id="cohort-v1",
        evidence_corpus=EvidenceCorpusDescriptor(
            path="corpora/evidence.json",
            sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
            media_type="application/json",
            ordering="chronological",
        ),
        graphs={
            GraphArm.FAITHFUL: GraphDescriptor(
                path="graphs/faithful.json",
                sha256=hashlib.sha256(graph.read_bytes()).hexdigest(),
                media_type="application/json",
                graph_format_version="graph-v1",
                backend_compatibility_id="backend-v1",
                embedding_model_id="embedding-v1",
                embedding_dimensions=1,
            )
        },
    )
    (fixture_root / "fixture.json").write_text(dump_fixture(manifest), encoding="utf-8")
    return fixture_root, manifest


def _identity(condition: SmokeCondition) -> RunIdentity:
    return RunIdentity(
        run_id=f"run-{condition.value}",
        experiment_id="smoke",
        condition_id=condition.value,
        challenge_id="readiness",
        attempt_id=f"attempt-{condition.value}",
        attempt_index=0,
    )


def _request(
    tmp_path: Path,
    *,
    conditions: tuple[SmokeCondition, ...] = (
        SmokeCondition.NO_STELE,
        SmokeCondition.FAITHFUL,
    ),
    mode: SmokeMode = SmokeMode.DRY_RUN,
) -> SmokeRequest:
    repository, revision = _repository(tmp_path)
    fixture_root, fixture = _fixture(tmp_path)
    return SmokeRequest(
        repository=repository,
        revision=revision,
        evaluated_project=tmp_path / "work" / "project",
        fixture_root=fixture_root,
        fixture=fixture,
        identities={condition: _identity(condition) for condition in conditions},
        conditions=conditions,
        mode=mode,
    )


def test_condition_selection_and_neutral_path_are_strict(tmp_path: Path) -> None:
    assert select_smoke_conditions() == (SmokeCondition.NO_STELE, SmokeCondition.FAITHFUL)
    assert select_smoke_conditions(["faithful"]) == (SmokeCondition.FAITHFUL,)
    with pytest.raises(SmokeConfigurationError, match="unknown"):
        select_smoke_conditions(["unknown"])
    with pytest.raises(SmokeConfigurationError, match="repeated"):
        select_smoke_conditions(["faithful", "faithful"])

    request = _request(tmp_path)
    with pytest.raises(SmokeConfigurationError, match="neutral"):
        SmokeRequest(
            **{**request.__dict__, "evaluated_project": tmp_path / "work" / "named-output"}
        )


def test_dry_run_materializes_blind_project_and_emits_external_provenance(tmp_path: Path) -> None:
    request = _request(tmp_path)

    receipts = asyncio.run(prepare_smoke(request))

    assert [receipt.condition for receipt in receipts] == [
        SmokeCondition.NO_STELE,
        SmokeCondition.FAITHFUL,
    ]
    assert (request.evaluated_project / "src" / "lib.rs").is_file()
    assert all("benchmark" not in json.dumps(receipt.as_dict()) for receipt in receipts)
    no_stele, faithful = receipts
    assert no_stele.condition_kind.value == "no-stele"
    assert no_stele.provenance.graph_sha256 is None
    assert faithful.provenance.graph_sha256 == request.fixture.graphs[GraphArm.FAITHFUL].sha256
    assert faithful.provenance.agent_import_path == HARBOR_AGENT_IMPORT_PATH
    assert faithful.restore_observation is None
    assert faithful.access_binding_verified is False


class RecordingProvisioner(FakeProvisioner):
    def __init__(self) -> None:
        super().__init__()
        self.operations: list[str] = []

    async def restore_with_receipt(self, *args: Any, **kwargs: Any):
        self.operations.append("restore")
        return await super().restore_with_receipt(*args, **kwargs)

    async def snapshot_with_receipt(self, *args: Any, **kwargs: Any):
        self.operations.append("snapshot")
        return await super().snapshot_with_receipt(*args, **kwargs)

    async def destroy(self, *args: Any, **kwargs: Any) -> None:
        self.operations.append("destroy")
        await super().destroy(*args, **kwargs)


class SetupOnlyAgent:
    def __init__(self, *, fail: bool = False) -> None:
        self.setup_calls = 0
        self.run_calls = 0
        self.fail = fail

    async def setup(self, _environment: object) -> None:
        self.setup_calls += 1
        if self.fail:
            raise RuntimeError("setup failed")

    async def run(self, *_: Any) -> None:
        self.run_calls += 1
        raise AssertionError("model execution must not occur in smoke preparation")


def test_install_only_faithful_uses_setup_receipt_and_tears_down(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        conditions=(SmokeCondition.NO_STELE, SmokeCondition.FAITHFUL),
        mode=SmokeMode.INSTALL_ONLY,
    )
    binary = tmp_path / "prebuilt-cli"
    binary.write_bytes(b"prebuilt")
    request = SmokeRequest(
        **{
            **request.__dict__,
            "integration_provenance": LocalDirtyWorktreeArtifact(
                binary, hashlib.sha256(binary.read_bytes()).hexdigest()
            ).provenance,
        }
    )
    provisioner = RecordingProvisioner()
    control_agent = SetupOnlyAgent()
    agent = SetupOnlyAgent()

    receipts = asyncio.run(
        prepare_smoke(
            request,
            SmokeDependencies(
                provisioner=provisioner,
                no_stele_install=InstallOnlySetup(control_agent, object()),
                faithful_install=InstallOnlySetup(agent, object()),
            ),
        )
    )

    assert provisioner.operations == ["restore", "snapshot", "destroy"]
    assert provisioner.projects == {}
    assert control_agent.setup_calls == 1
    assert control_agent.run_calls == 0
    assert agent.setup_calls == 1
    assert agent.run_calls == 0
    control = receipts[0]
    assert control.condition is SmokeCondition.NO_STELE
    assert control.install_only_completed is True
    faithful = receipts[-1]
    assert faithful.install_only_completed is True
    assert faithful.cleanup_completed is True
    assert faithful.restore_observation is not None
    assert faithful.access_binding_verified is False
    assert faithful.provenance.integration_artifact is not None
    assert faithful.provenance.integration_artifact.source_kind == "local-dirty-worktree"


def test_no_stele_install_only_setup_failure_stops_before_faithful_setup(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        conditions=(SmokeCondition.NO_STELE, SmokeCondition.FAITHFUL),
        mode=SmokeMode.INSTALL_ONLY,
    )
    provisioner = RecordingProvisioner()
    control_agent = SetupOnlyAgent(fail=True)
    faithful_agent = SetupOnlyAgent()

    with pytest.raises(RuntimeError, match="setup failed"):
        asyncio.run(
            prepare_smoke(
                request,
                SmokeDependencies(
                    provisioner=provisioner,
                    no_stele_install=InstallOnlySetup(control_agent, object()),
                    faithful_install=InstallOnlySetup(faithful_agent, object()),
                ),
            )
        )

    assert control_agent.setup_calls == 1
    assert faithful_agent.setup_calls == 0
    assert provisioner.operations == []


class FakeConfiguredBase:
    def __init__(self, logs_dir: Path, **kwargs: Any) -> None:
        self.logs_dir = logs_dir
        self.kwargs = kwargs


def _binding_path(tmp_path: Path) -> Path:
    path = tmp_path / "project.json"
    path.write_text(
        json.dumps(
            ActiveProjectBinding(
                project_id="11111111-1111-4111-8111-111111111111",
                slug="bound-project",
                name="Bound Project",
                default_branch="main",
                active_workspace_ids=("22222222-2222-4222-8222-222222222222",),
            ).as_cache_object()
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_project_binding_rejects_extra_metadata(tmp_path: Path) -> None:
    binding = _binding_path(tmp_path)
    payload = json.loads(binding.read_text(encoding="utf-8"))
    payload["benchmarkMetadata"] = "forbidden"

    with pytest.raises(BindingValidationError, match="exactly"):
        ActiveProjectBinding.from_json_bytes(json.dumps(payload).encode("utf-8"))


def test_harbor_scalar_entrypoint_constructs_adapter_inputs_without_secret_repr(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "prebuilt"
    credentials = tmp_path / "credentials.json"
    preferences = tmp_path / "preferences.json"
    artifact.write_bytes(b"prebuilt")
    credentials.write_text('{"token":"do-not-print"}', encoding="utf-8")
    preferences.write_text("{}", encoding="utf-8")
    credentials.chmod(0o600)
    preferences.chmod(0o600)
    binding = _binding_path(tmp_path)
    configured_class = _make_configured_agent(FakeConfiguredBase)

    configured = configured_class(
        tmp_path / "logs",
        artifact_path=str(artifact),
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        artifact_source_kind="local-dirty-worktree",
        credentials_path=str(credentials),
        preferences_path=str(preferences),
        project_binding_path=str(binding),
    )

    assert configured.kwargs["artifact"].provenance.source_kind == "local-dirty-worktree"
    assert "do-not-print" not in repr(configured.kwargs["attempt_state"])
    assert configured.kwargs["project_binding"].slug == "bound-project"


def test_harbor_scalar_entrypoint_rejects_invalid_config_without_secret_contents(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "prebuilt"
    credentials = tmp_path / "credentials.json"
    preferences = tmp_path / "preferences.json"
    artifact.write_bytes(b"prebuilt")
    credentials.write_text('{"token":"do-not-print"}', encoding="utf-8")
    preferences.write_text("not-json", encoding="utf-8")
    credentials.chmod(0o600)
    preferences.chmod(0o600)
    binding = _binding_path(tmp_path)
    configured_class = _make_configured_agent(FakeConfiguredBase)

    with pytest.raises(HarborConfigurationError) as error:
        configured_class(
            tmp_path / "logs",
            artifact_path=str(artifact),
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            artifact_source_kind="release",
            artifact_release_version="1.2.3",
            credentials_path=str(credentials),
            preferences_path=str(preferences),
            project_binding_path=str(binding),
        )

    assert "do-not-print" not in str(error.value)


@pytest.mark.parametrize("kind", ["public", "symlink", "oversized"])
def test_harbor_scalar_entrypoint_rejects_unsafe_secret_files_without_contents(
    tmp_path: Path, kind: str
) -> None:
    artifact = tmp_path / "prebuilt"
    credentials = tmp_path / "credentials.json"
    preferences = tmp_path / "preferences.json"
    artifact.write_bytes(b"prebuilt")
    credentials.write_text('{"token":"do-not-print"}', encoding="utf-8")
    preferences.write_text("{}", encoding="utf-8")
    credentials.chmod(0o600)
    preferences.chmod(0o600)
    binding = _binding_path(tmp_path)
    if kind == "public":
        credentials.chmod(0o644)
    elif kind == "symlink":
        linked = tmp_path / "linked-credentials.json"
        linked.write_text('{"token":"do-not-print"}', encoding="utf-8")
        linked.chmod(0o600)
        credentials.unlink()
        credentials.symlink_to(linked)
    else:
        credentials.write_bytes(b"x" * (1_048_577))
        credentials.chmod(0o600)
    configured_class = _make_configured_agent(FakeConfiguredBase)

    with pytest.raises(HarborConfigurationError) as error:
        configured_class(
            tmp_path / "logs",
            artifact_path=str(artifact),
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            artifact_source_kind="local-dirty-worktree",
            credentials_path=str(credentials),
            preferences_path=str(preferences),
            project_binding_path=str(binding),
        )

    assert "do-not-print" not in str(error.value)


def test_harbor_scalar_entrypoint_rejects_non_private_project_binding(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "prebuilt"
    credentials = tmp_path / "credentials.json"
    preferences = tmp_path / "preferences.json"
    artifact.write_bytes(b"prebuilt")
    credentials.write_text("{}", encoding="utf-8")
    preferences.write_text("{}", encoding="utf-8")
    credentials.chmod(0o600)
    preferences.chmod(0o600)
    binding = _binding_path(tmp_path)
    binding.chmod(0o644)

    with pytest.raises(HarborConfigurationError, match="project binding"):
        _make_configured_agent(FakeConfiguredBase)(
            tmp_path / "logs",
            artifact_path=str(artifact),
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            artifact_source_kind="local-dirty-worktree",
            credentials_path=str(credentials),
            preferences_path=str(preferences),
            project_binding_path=str(binding),
        )


def test_install_only_failure_still_destroys_restored_project(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        conditions=(SmokeCondition.FAITHFUL,),
        mode=SmokeMode.INSTALL_ONLY,
    )
    provisioner = RecordingProvisioner()

    with pytest.raises(RuntimeError, match="setup failed"):
        asyncio.run(
            prepare_smoke(
                request,
                SmokeDependencies(
                    provisioner=provisioner,
                    faithful_install=InstallOnlySetup(SetupOnlyAgent(fail=True), object()),
                ),
            )
        )

    assert provisioner.operations == ["restore", "destroy"]
    assert provisioner.projects == {}


def test_cli_smoke_dry_run_never_requires_a_backend_or_agent(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    fixture_root, _ = _fixture(tmp_path)

    result = runner.invoke(
        app,
        [
            "smoke-dry-run",
            str(repository),
            revision,
            str(fixture_root / "fixture.json"),
            str(tmp_path / "evaluated" / "project"),
            "--condition",
            "no-stele",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [receipt["condition"] for receipt in payload] == ["no-stele"]
    assert (tmp_path / "evaluated" / "project" / "src" / "lib.rs").is_file()
