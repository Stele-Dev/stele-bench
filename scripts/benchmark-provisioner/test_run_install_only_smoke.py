from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stele_bench.challenges import LoadedChallenge
from stele_bench.contracts import GraphArm, ProvisionedProject
from stele_bench.corpus import CorpusEntry, MaterializedCorpus
from stele_bench.harbor_pack import FaithfulAgentConfig, HarborTaskPack
from stele_bench.preflight import ActiveProjectBinding

MODULE_PATH = Path(__file__).with_name("run_install_only_smoke.py")
SPEC = importlib.util.spec_from_file_location("install_only_smoke", MODULE_PATH)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


class _FakeProvisioner:
    def __init__(self, project: ProvisionedProject) -> None:
        self.project = project
        self.destroyed = False

    async def restore_with_receipt(self, *_: object) -> Any:
        self.fixture = _[0]
        return SimpleNamespace(project=self.project)

    async def snapshot_with_receipt(self, project: ProvisionedProject) -> Any:
        assert project == self.project
        descriptor = self.fixture.graphs[GraphArm.FAITHFUL]
        return SimpleNamespace(
            observation=lambda restore: SimpleNamespace(
                project=self.project,
                state=SimpleNamespace(
                    project_id=self.project.project_id,
                    canonical_state=descriptor,
                ),
                probes=(
                    SimpleNamespace(kind="fts"),
                    SimpleNamespace(kind="semantic"),
                    SimpleNamespace(kind="graph"),
                ),
                queue_before_count=0,
                queue_after_count=0,
            )
        )

    async def destroy(self, project: ProvisionedProject) -> None:
        assert project == self.project
        self.destroyed = True


def _inputs(
    tmp_path: Path,
) -> tuple[Any, ActiveProjectBinding, list[tuple[str, dict[str, object]]]]:
    fixture_root = Path(__file__).parents[2] / "eval/stele-bench/fixtures/smoke"
    artifact = tmp_path / "stele"
    artifact.write_bytes(b"prebuilt-executable")
    artifact.chmod(0o755)
    binding = ActiveProjectBinding(
        project_id="c0f0d109-7125-4da0-92dd-6dfd2f79c2b2",
        slug="plain-service",
        name="Plain service",
        default_branch="main",
        active_workspace_ids=(),
    )
    calls: list[tuple[str, dict[str, object]]] = []
    inputs = smoke.InstallOnlySmokeInputs(
        repository=tmp_path / "source",
        revision="a" * 40,
        fixture_path=fixture_root / "fixture.json",
        provisioner_executable=tmp_path / "provision.py",
        attempt_root=tmp_path / "attempts",
        work_root=tmp_path / "work",
        run_id="smoke-run",
        artifact_path=artifact,
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        artifact_source_kind="local-dirty-worktree",
        backend_url="http://host.docker.internal:54321",
        backend_anon_key="public-client-key",
        timeout_seconds=30,
    )
    return inputs, binding, calls


def _materialize(_: Path, revision: str, destination: Path) -> MaterializedCorpus:
    destination.mkdir(parents=True)
    source = destination / "app.py"
    source.write_text("print('ready')\n", encoding="utf-8")
    return MaterializedCorpus(
        revision,
        destination,
        (CorpusEntry("app.py", hashlib.sha256(source.read_bytes()).hexdigest(), "file", 0o644),),
        "b" * 64,
    )


def _pack(
    corpus: MaterializedCorpus,
    destination: Path,
    faithful: FaithfulAgentConfig | None = None,
    *,
    challenges: tuple[LoadedChallenge, ...],
) -> HarborTaskPack:
    destination.mkdir()
    tasks = tuple(destination / challenge.challenge_id for challenge in challenges)
    for task in tasks:
        task.mkdir()
    plain = destination / "route-one.json"
    plain.write_text("{}")
    configured = None
    configured_job_name = None
    if faithful is not None:
        configured = destination / "route-two.json"
        configured.write_text("{}")
        configured_job_name = "route-two"
    return HarborTaskPack(
        destination,
        tasks,
        tuple(corpus.destination for _ in tasks),
        plain,
        configured,
        "route-one",
        configured_job_name,
    )


def _write_preflight_reports(
    argv: tuple[str, ...], inputs: Any, challenge_ids: tuple[str, ...]
) -> None:
    jobs = Path(argv[argv.index("--jobs-dir") + 1])
    for challenge_id in challenge_ids:
        report = jobs / "route-two" / challenge_id / "trial" / "agent" / "preflight.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "passed": True,
                    "binding_verified": True,
                    "artifact_sha256": inputs.artifact_sha256,
                    "stele_command_resolves_to_artifact": True,
                    "account_authenticated": True,
                    "doctor_checked_with_explicit_slug": True,
                    "lifecycle_firing_verified": False,
                    "managed_permission_precedence_verified": False,
                }
            )
        )


def test_orchestrates_two_install_only_commands_and_redacts_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, binding, calls = _inputs(tmp_path)
    inputs = replace(
        inputs,
        challenge_ids=("input-keypress-architecture", "package-cache-is-empty"),
    )
    project = ProvisionedProject(
        project_id=binding.project_id,
        fixture_id="dotstate-smoke-v1",
        run_id=inputs.run_id,
        graph_arm=GraphArm.FAITHFUL,
    )
    provisioner = _FakeProvisioner(project)
    monkeypatch.setenv("STELE_BENCH_ATTEMPT_ROOT", "/preexisting/private-root")

    def private_call(operation: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((operation, payload))
        attempt = smoke._attempt_directory(inputs.attempt_root, inputs.run_id)
        if operation == "prepare":
            attempt.mkdir(parents=True)
            for name, contents in (
                ("credentials.json", b"{}"),
                ("preferences.json", b"{}"),
                ("project.json", json.dumps(binding.as_cache_object()).encode()),
            ):
                target = attempt / name
                target.write_bytes(contents)
                target.chmod(0o600)
        return {"ok": True}

    seen_argv: list[tuple[str, ...]] = []

    def harbor_call(argv: tuple[str, ...]) -> Any:
        seen_argv.append(argv)
        assert "--install-only" in argv
        assert "--model" not in argv
        assert "--agent" not in argv
        if "route-two.json" in argv[argv.index("--config") + 1]:
            _write_preflight_reports(argv, inputs, inputs.challenge_ids)
        return smoke.CommandObservation(exit_code=0)

    receipt = smoke.run_install_only_smoke(
        inputs,
        private_call=private_call,
        provisioner=provisioner,
        harbor_call=harbor_call,
        materialize=_materialize,
        pack=_pack,
    )

    assert [call[0] for call in calls] == ["prepare", "cleanup"]
    assert len(seen_argv) == 2
    assert [receipt.condition for receipt in receipt.conditions] == ["no-stele", "faithful"]
    assert all(item.challenge_ids == inputs.challenge_ids for item in receipt.conditions)
    assert provisioner.destroyed is True
    assert receipt.access_binding_verified is True
    assert receipt.snapshot_verified is True
    encoded = json.dumps(receipt.as_dict())
    assert str(inputs.work_root) not in encoded
    assert str(inputs.attempt_root) not in encoded
    assert inputs.backend_anon_key not in encoded
    assert smoke.os.environ["STELE_BENCH_ATTEMPT_ROOT"] == "/preexisting/private-root"


def test_faithful_only_skips_stock_control(tmp_path: Path) -> None:
    inputs, binding, calls = _inputs(tmp_path)
    inputs = replace(inputs, conditions=("faithful",))
    project = ProvisionedProject(
        project_id=binding.project_id,
        fixture_id="dotstate-smoke-v1",
        run_id=inputs.run_id,
        graph_arm=GraphArm.FAITHFUL,
    )
    provisioner = _FakeProvisioner(project)

    def private_call(operation: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((operation, payload))
        attempt = smoke._attempt_directory(inputs.attempt_root, inputs.run_id)
        if operation == "prepare":
            attempt.mkdir(parents=True)
            for name, contents in (
                ("credentials.json", b"{}"),
                ("preferences.json", b"{}"),
                ("project.json", json.dumps(binding.as_cache_object()).encode()),
            ):
                path = attempt / name
                path.write_bytes(contents)
                path.chmod(0o600)
        return {"ok": True}

    def harbor_call(argv: tuple[str, ...]) -> Any:
        config = argv[argv.index("--config") + 1]
        assert config.endswith("route-two.json")
        _write_preflight_reports(argv, inputs, inputs.challenge_ids)
        return smoke.CommandObservation(exit_code=0)

    receipt = smoke.run_install_only_smoke(
        inputs,
        private_call=private_call,
        provisioner=provisioner,
        harbor_call=harbor_call,
        materialize=_materialize,
        pack=_pack,
    )

    assert [item.condition for item in receipt.conditions] == ["faithful"]
    assert [operation for operation, _ in calls] == ["prepare", "cleanup"]
    assert provisioner.destroyed is True


def test_no_stele_only_never_touches_private_graph_state(tmp_path: Path) -> None:
    inputs, _, _ = _inputs(tmp_path)
    inputs = replace(inputs, conditions=("no-stele",))

    def private_call(_: str, __: dict[str, object]) -> dict[str, object]:
        raise AssertionError("no-stele must not invoke the private provisioner")

    def harbor_call(argv: tuple[str, ...]) -> Any:
        config = argv[argv.index("--config") + 1]
        assert config.endswith("route-one.json")
        return smoke.CommandObservation(exit_code=0)

    def stock_pack(
        corpus: MaterializedCorpus,
        destination: Path,
        faithful: FaithfulAgentConfig | None = None,
        *,
        challenges: tuple[LoadedChallenge, ...],
    ) -> HarborTaskPack:
        assert faithful is None
        return _pack(corpus, destination, faithful, challenges=challenges)

    receipt = smoke.run_install_only_smoke(
        inputs,
        private_call=private_call,
        provisioner=None,
        harbor_call=harbor_call,
        materialize=_materialize,
        pack=stock_pack,
    )

    assert [item.condition for item in receipt.conditions] == ["no-stele"]
    assert receipt.restore_verified is False
    assert receipt.snapshot_verified is False
    assert receipt.cleanup_completed is False
    assert not inputs.attempt_root.exists()


def test_duplicate_and_unknown_challenge_selectors_fail_closed(tmp_path: Path) -> None:
    inputs, _, _ = _inputs(tmp_path)
    with pytest.raises(ValueError, match="challenges must not repeat"):
        replace(inputs, challenge_ids=("service-readiness", "service-readiness"))

    unknown = replace(inputs, conditions=("no-stele",), challenge_ids=("not-a-challenge",))
    with pytest.raises(ValueError, match="unknown challenge ids"):
        smoke.run_install_only_smoke(
            unknown,
            private_call=lambda *_: pytest.fail("private state must not be touched"),
            provisioner=None,
            harbor_call=lambda _: pytest.fail("Harbor must not be called"),
            materialize=_materialize,
            pack=_pack,
        )


def test_parser_preserves_repeatable_challenge_order() -> None:
    args = smoke._parser().parse_args(
        [
            "repo",
            "revision",
            "fixture.json",
            "--attempt-root",
            "attempts",
            "--work-root",
            "work",
            "--run-id",
            "run",
            "--artifact",
            "artifact",
            "--artifact-sha256",
            "a" * 64,
            "--artifact-source-kind",
            "local-dirty-worktree",
            "--backend-url",
            "http://localhost:54321",
            "--backend-anon-key",
            "key",
            "--challenge",
            "input-keypress-architecture",
            "--challenge",
            "package-cache-is-empty",
        ]
    )

    assert args.challenges == ["input-keypress-architecture", "package-cache-is-empty"]


def test_no_stele_inputs_do_not_require_artifact_or_backend(tmp_path: Path) -> None:
    inputs = smoke.InstallOnlySmokeInputs(
        repository=tmp_path / "repo",
        revision="a" * 40,
        fixture_path=tmp_path / "fixture.json",
        provisioner_executable=tmp_path / "provision.py",
        attempt_root=tmp_path / "attempts",
        work_root=tmp_path / "work",
        run_id="control-only",
        conditions=("no-stele",),
    )

    assert inputs.artifact_path is None
    assert inputs.backend_url is None


def test_faithful_inputs_require_artifact_and_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="faithful requires artifact and backend inputs"):
        smoke.InstallOnlySmokeInputs(
            repository=tmp_path / "repo",
            revision="a" * 40,
            fixture_path=tmp_path / "fixture.json",
            provisioner_executable=tmp_path / "provision.py",
            attempt_root=tmp_path / "attempts",
            work_root=tmp_path / "work",
            run_id="faithful",
            conditions=("faithful",),
        )


def test_harbor_command_scopes_public_import_path_without_global_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class FakeProcess:
        returncode = 0
        pid = 12345

        def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
            seen["timeout"] = timeout
            return b"", b""

    def fake_popen(*_: object, **kwargs: object) -> FakeProcess:
        seen.update(kwargs)
        return FakeProcess()

    monkeypatch.setenv("PYTHONPATH", "/existing/path")
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-leak")
    monkeypatch.setattr(smoke.subprocess, "Popen", fake_popen)
    result = smoke._harbor_command(("harbor", "run", "--install-only"), timeout_seconds=1)

    assert result.exit_code == 0
    environment = seen["env"]
    assert isinstance(environment, dict)
    assert environment["PYTHONPATH"] == str(smoke._PUBLIC_SOURCE)
    assert "UNRELATED_HOST_SECRET" not in environment
    assert smoke.os.environ["PYTHONPATH"] == "/existing/path"
    assert seen["start_new_session"] is (os.name == "posix")


def test_harbor_timeout_terminates_group_and_writes_redacted_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminated: list[object] = []

    class FakeProcess:
        returncode = None
        pid = 12345

        def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
            raise subprocess.TimeoutExpired(
                "harbor", timeout, output=b"gateway-secret", stderr=b"Bearer gateway-secret"
            )

    process = FakeProcess()
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(smoke, "_terminate_process_group", terminated.append)
    diagnostic = tmp_path / "harbor.diagnostic.json"

    with pytest.raises(smoke.InstallOnlySmokeError, match="harbor_install_only_timeout"):
        smoke._harbor_command(
            ("harbor", "run", "--install-only"),
            timeout_seconds=1,
            diagnostic_path=diagnostic,
            include_gateway=True,
        )

    assert terminated == [process]
    assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    encoded = diagnostic.read_text()
    assert "gateway-secret" not in encoded
    assert "[REDACTED]" in encoded


def test_controlled_signal_handler_raises_and_restores_previous_handler() -> None:
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(smoke.RunnerInterrupted):
        with smoke._controlled_termination_signals():
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
    assert signal.getsignal(signal.SIGTERM) == previous


def test_scoped_child_environment_hides_unrelated_values_and_restores_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "do-not-inherit")
    monkeypatch.setenv("STELE_BENCH_ATTEMPT_ROOT", "/public/attempt-root")

    with smoke._scoped_child_environment():
        assert "UNRELATED_HOST_SECRET" not in os.environ
        assert os.environ["STELE_BENCH_ATTEMPT_ROOT"] == "/public/attempt-root"
        assert os.environ["PYTHONPATH"] == str(smoke._PUBLIC_SOURCE)

    assert os.environ["UNRELATED_HOST_SECRET"] == "do-not-inherit"


def _minimal_receipt() -> Any:
    return smoke.InstallOnlySmokeReceipt(
        schema_version=2,
        mode="install-only",
        source_commit="a" * 40,
        source_tree_sha256="b" * 64,
        fixture_id="fixture",
        graph_arm="faithful",
        integration_artifact={"source_kind": None, "sha256": None, "release_version": None},
        conditions=(),
        restore_verified=False,
        snapshot_verified=False,
        cleanup_completed=False,
        access_binding_verified=False,
    )


def test_main_atomically_persists_private_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(smoke, "run_install_only_smoke", lambda *_: _minimal_receipt())

    exit_code = smoke.main(
        [
            str(tmp_path),
            "a" * 40,
            str(tmp_path / "fixture.json"),
            "--attempt-root",
            str(tmp_path / "attempt"),
            "--work-root",
            str(tmp_path / "work"),
            "--run-id",
            "receipt-success",
            "--condition",
            "no-stele",
            "--receipt-out",
            str(receipt_path),
        ]
    )

    assert exit_code == 0
    assert json.loads(receipt_path.read_text())["ok"] is True
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600


def test_main_persists_retained_failure_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_path = tmp_path / "receipt.json"
    retained = _minimal_receipt()

    def fail(*_: object) -> Any:
        error = smoke.InstallOnlySmokeError("install_only_smoke_failed", receipt=retained)
        error.__cause__ = smoke.InstallOnlySmokeError("configured_install_only_failed")
        raise error

    monkeypatch.setattr(smoke, "run_install_only_smoke", fail)
    exit_code = smoke.main(
        [
            str(tmp_path),
            "a" * 40,
            str(tmp_path / "fixture.json"),
            "--attempt-root",
            str(tmp_path / "attempt"),
            "--work-root",
            str(tmp_path / "work"),
            "--run-id",
            "receipt-failure",
            "--condition",
            "no-stele",
            "--receipt-out",
            str(receipt_path),
        ]
    )

    payload = json.loads(receipt_path.read_text())
    assert exit_code == 1
    assert payload["failure"] == {
        "code": "configured_install_only_failed",
        "message": "configured install only failed",
    }
    assert payload["receipt"]["mode"] == "install-only"


def test_private_binding_reader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    link = tmp_path / "project.json"
    link.symlink_to(target)

    with pytest.raises(smoke.InstallOnlySmokeError, match="project_binding_is_symlink"):
        smoke._read_binding(link)


def test_cleanup_happens_when_configured_install_fails(tmp_path: Path) -> None:
    inputs, binding, calls = _inputs(tmp_path)
    project = ProvisionedProject(
        project_id=binding.project_id,
        fixture_id="dotstate-smoke-v1",
        run_id=inputs.run_id,
        graph_arm=GraphArm.FAITHFUL,
    )
    provisioner = _FakeProvisioner(project)

    def private_call(operation: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((operation, payload))
        attempt = smoke._attempt_directory(inputs.attempt_root, inputs.run_id)
        if operation == "prepare":
            attempt.mkdir(parents=True)
            for name, contents in (
                ("credentials.json", b"{}"),
                ("preferences.json", b"{}"),
                ("project.json", json.dumps(binding.as_cache_object()).encode()),
            ):
                target = attempt / name
                target.write_bytes(contents)
                target.chmod(0o600)
        return {"ok": True}

    def harbor_call(argv: tuple[str, ...]) -> Any:
        return smoke.CommandObservation(exit_code=1 if "route-two.json" in str(argv) else 0)

    with pytest.raises(smoke.InstallOnlySmokeError, match="install_only_smoke_failed"):
        smoke.run_install_only_smoke(
            inputs,
            private_call=private_call,
            provisioner=provisioner,
            harbor_call=harbor_call,
            materialize=_materialize,
            pack=_pack,
        )
    assert provisioner.destroyed is True
    assert [call[0] for call in calls] == ["prepare", "cleanup"]
