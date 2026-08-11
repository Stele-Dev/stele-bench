from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from stele_bench.contracts import (
    EvidenceCorpusDescriptor,
    FixtureManifestV1,
    GraphArm,
    GraphDescriptor,
    RunIdentity,
)
from stele_bench.provisioning import (
    ExternalCommandProvisioner,
    ProvisionerCommandError,
    ProvisionerExitError,
    ProvisionerMalformedResponseError,
    ProvisionerOutputTooLargeError,
    ProvisionerTimeoutError,
)

HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def manifest(graph_path: str = "graphs/faithful.json") -> FixtureManifestV1:
    graph = GraphDescriptor(
        path=graph_path,
        sha256=HASH,
        media_type="application/json",
        graph_format_version="v1",
        backend_compatibility_id="fake",
        embedding_model_id="fake",
        embedding_dimensions=1,
    )
    return FixtureManifestV1(
        fixture_id="fixture",
        cohort_id="cohort",
        evidence_corpus=EvidenceCorpusDescriptor(
            path="evidence.json", sha256=HASH, media_type="application/json", ordering="fixed"
        ),
        graphs={GraphArm.FAITHFUL: graph},
    )


def identity() -> RunIdentity:
    return RunIdentity(
        run_id="run",
        experiment_id="experiment",
        condition_id="condition",
        challenge_id="challenge",
        attempt_id="attempt",
        attempt_index=0,
    )


def fake_command(tmp_path: Path, mode: str) -> tuple[str, ...]:
    (tmp_path / "graphs").mkdir(exist_ok=True)
    (tmp_path / "graphs" / "faithful.json").touch()
    (tmp_path / "evidence.json").touch()
    script = tmp_path / "fake.py"
    script.write_text(
        """import json, sys, time
mode, operation = sys.argv[1:]
request = json.load(sys.stdin)
if mode == 'sleep': time.sleep(2)
if mode == 'closed-pipes': sys.stdout.close(); sys.stderr.close(); time.sleep(2)
if mode == 'exit': print('diagnostic', file=sys.stderr); raise SystemExit(7)
if mode == 'huge': print('x' * 10000); raise SystemExit()
if mode == 'extra': print('{} {}'); raise SystemExit()
if mode == 'malformed': print('not-json'); raise SystemExit()
project = {'project_id': 'project-' + request.get('run_identity', {}).get('run_id', ''), 'fixture_id': request.get('fixture', {}).get('fixture_id', 'fixture'), 'run_id': request.get('run_identity', {}).get('run_id', ''), 'graph_arm': request.get('graph_arm', 'faithful')}
state = {'fixture_id': 'fixture', 'project_id': request.get('project', project)['project_id'], 'graph_arm': 'faithful', 'canonical_state': {'path': 'snapshot.json', 'sha256': '"""
        + HASH
        + """', 'media_type': 'application/json', 'graph_format_version': 'v1', 'backend_compatibility_id': 'fake', 'embedding_model_id': 'fake', 'embedding_dimensions': 1}, 'declared_node_count': 1, 'declared_edge_count': 0}
if operation == 'restore': print(json.dumps({'project': project, 'queue_before_count': 0, 'restore_seconds': 0} if mode == 'receipt' else project))
elif operation == 'snapshot': print(json.dumps({'state': state, 'probes': [{'name': 'fts', 'kind': 'fts', 'stable_ids': []}, {'name': 'semantic', 'kind': 'semantic', 'stable_ids': [], 'scores': []}, {'name': 'graph', 'kind': 'graph', 'stable_ids': []}], 'queue_after_count': 0, 'snapshot_seconds': 0} if mode == 'receipt' else state))
else: print(json.dumps({'ok': True}))
""",
        encoding="utf-8",
    )
    return (sys.executable, str(script), mode)


def test_external_command_success_isolation_and_cleanup(tmp_path: Path) -> None:
    async def run() -> None:
        provisioner = ExternalCommandProvisioner(fake_command(tmp_path, "ok"), tmp_path)
        first = await provisioner.restore(manifest(), GraphArm.FAITHFUL, identity())
        second = await provisioner.restore(
            manifest(), GraphArm.FAITHFUL, identity().model_copy(update={"run_id": "run-2"})
        )
        assert first.project_id != second.project_id
        assert (await provisioner.snapshot(first)).project_id == first.project_id
        await provisioner.destroy(first)
        await provisioner.destroy(second)

    asyncio.run(run())


def test_external_command_receipts_form_a_complete_observation(tmp_path: Path) -> None:
    async def run() -> None:
        provisioner = ExternalCommandProvisioner(fake_command(tmp_path, "receipt"), tmp_path)
        restored = await provisioner.restore_with_receipt(
            manifest(), GraphArm.FAITHFUL, identity()
        )
        snapshot = await provisioner.snapshot_with_receipt(restored.project)
        observation = snapshot.observation(restored)
        assert observation.project == restored.project
        assert observation.state.project_id == restored.project.project_id
        # Minimal callers remain compatible with the richer command response.
        assert (
            await provisioner.restore(manifest(), GraphArm.FAITHFUL, identity())
        ).project_id == restored.project.project_id
        assert (await provisioner.snapshot(restored.project)).project_id == restored.project.project_id

    asyncio.run(run())


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("malformed", ProvisionerMalformedResponseError),
        ("extra", ProvisionerMalformedResponseError),
        ("exit", ProvisionerExitError),
        ("huge", ProvisionerOutputTooLargeError),
        ("sleep", ProvisionerTimeoutError),
        ("closed-pipes", ProvisionerTimeoutError),
    ],
)
def test_external_command_rejects_bad_process_responses(
    tmp_path: Path, mode: str, error: type[Exception]
) -> None:
    async def run() -> None:
        provisioner = ExternalCommandProvisioner(
            fake_command(tmp_path, mode), tmp_path, timeout_seconds=0.5, max_output_bytes=100
        )
        with pytest.raises(error):
            await provisioner.restore(manifest(), GraphArm.FAITHFUL, identity())

    asyncio.run(run())


def test_external_command_rejects_artifact_escape(tmp_path: Path) -> None:
    async def run() -> None:
        escaped_graph = GraphDescriptor.model_construct(
            **manifest().graphs[GraphArm.FAITHFUL].model_dump(exclude={"path"}),
            path="../outside.json",
        )
        unsafe = manifest().model_copy(update={"graphs": {GraphArm.FAITHFUL: escaped_graph}})
        provisioner = ExternalCommandProvisioner(fake_command(tmp_path, "ok"), tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            await provisioner.restore(unsafe, GraphArm.FAITHFUL, identity())

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["missing", "digest"])
def test_external_command_checks_artifact_integrity(tmp_path: Path, damage: str) -> None:
    async def run() -> None:
        command = fake_command(tmp_path, "ok")
        evidence = tmp_path / "evidence.json"
        if damage == "missing":
            evidence.unlink()
        else:
            evidence.write_text("changed", encoding="utf-8")
        provisioner = ExternalCommandProvisioner(command, tmp_path)
        with pytest.raises(ProvisionerCommandError, match="artifact"):
            await provisioner.restore(manifest(), GraphArm.FAITHFUL, identity())

    asyncio.run(run())
