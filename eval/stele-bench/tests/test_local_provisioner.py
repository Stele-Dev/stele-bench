from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

from stele_bench.contracts import (
    GraphArm,
    ProvisionedProject,
    RunIdentity,
    build_restoration_report,
)
from stele_bench.fixtures import dump_fixture, load_fixture
from stele_bench.provisioning import ExternalCommandProvisioner, ProvisionerExitError

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PACKAGE_ROOT / "fixtures" / "smoke"
FIXTURE_PATH = FIXTURE_ROOT / "fixture.json"
SHORT_ID_PREFIX_RANK = {"COMP": 0, "KNOW": 1, "TASK": 2, "DOC": 3, "DIAG": 4}


def _short_id_key(short_id: str) -> tuple[int, int]:
    prefix, suffix = short_id.split("-", 1)
    return SHORT_ID_PREFIX_RANK[prefix], int(suffix)


def _identity(run_id: str, attempt_index: int) -> RunIdentity:
    return RunIdentity(
        run_id=run_id,
        experiment_id="local-round-trip",
        condition_id="faithful",
        challenge_id="restore-proof",
        attempt_id=run_id,
        attempt_index=attempt_index,
    )


def _local_stack_available() -> bool:
    container = os.environ.get("STELE_BENCH_DB_CONTAINER", "supabase_db_stele")
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _live_executable() -> Path:
    if not _local_stack_available():
        pytest.skip("local Supabase database container is unavailable")
    executable_value = os.environ.get("STELE_BENCH_PROVISIONER")
    if executable_value is None:
        pytest.skip("set STELE_BENCH_PROVISIONER to the private executable")
    executable = Path(executable_value).resolve()
    if not executable.is_file():
        pytest.skip("configured private provisioner is unavailable")
    return executable


def _provisioner(fixture_root: Path) -> ExternalCommandProvisioner:
    return ExternalCommandProvisioner(
        (str(_live_executable()),), fixture_root, timeout_seconds=60
    )


def _private_call(operation: str, payload: dict[str, object]) -> dict[str, object]:
    result = subprocess.run(
        [str(_live_executable()), operation],
        input=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"private {operation} operation failed"
    response = json.loads(result.stdout)
    assert isinstance(response, dict)
    assert not ({"access_token", "refresh_token", "password"} & set(response))
    return response


def _set_attempt_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "attempt-state"
    monkeypatch.setenv("STELE_BENCH_ATTEMPT_ROOT", str(root))
    url, anon = _local_api_configuration()
    monkeypatch.setenv("STELE_BENCH_SUPABASE_URL", url)
    monkeypatch.setenv("STELE_BENCH_SUPABASE_ANON_KEY", anon)
    return root


def _attempt_directory(root: Path, run_id: str) -> Path:
    return root / hashlib.sha256(run_id.encode()).hexdigest()


def _prepare_attempt(run_id: str) -> None:
    response = _private_call("prepare", {"run_id": run_id, "harnesses": ["claude"]})
    assert response["ok"] is True


def _cleanup_attempt(run_id: str) -> None:
    response = _private_call("cleanup", {"run_id": run_id})
    assert response == {"ok": True}


def _read_attempt_file(root: Path, run_id: str, name: str) -> dict[str, object]:
    path = _attempt_directory(root, run_id) / name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _local_api_configuration() -> tuple[str, str]:
    url = os.environ.get("STELE_BENCH_SUPABASE_URL") or os.environ.get("SUPABASE_TEST_URL")
    anon = os.environ.get("STELE_BENCH_SUPABASE_ANON_KEY") or os.environ.get(
        "SUPABASE_TEST_ANON_KEY"
    )
    if not url or not anon:
        result = subprocess.run(
            ["supabase", "status", "-o", "env"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value.strip().strip('"')
        url = url or values.get("API_URL")
        anon = anon or values.get("ANON_KEY") or values.get("PUBLISHABLE_KEY")
    assert url and anon
    return url.rstrip("/"), anon


def _api_nodes(access_token: str, project_id: str) -> list[dict[str, object]]:
    url, anon = _local_api_configuration()
    query = urllib.parse.urlencode(
        {
            "select": "short_id,title",
            "project_id": f"eq.{project_id}",
            "short_id": "eq.KNOW-3",
        }
    )
    request = urllib.request.Request(
        f"{url}/rest/v1/nodes?{query}",
        headers={"apikey": anon, "Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    assert isinstance(value, list)
    return value


def _db_psql(sql: str) -> str:
    container = os.environ.get("STELE_BENCH_DB_CONTAINER", "supabase_db_stele")
    user = os.environ.get("STELE_BENCH_DB_USER", "postgres")
    database = os.environ.get("STELE_BENCH_DB_NAME", "postgres")
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-U",
            user,
            "-d",
            database,
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=sql,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line]
    assert len(lines) == 1
    return lines[0]


def _write_variant_fixture(tmp_path: Path) -> Path:
    fixture = load_fixture(FIXTURE_PATH)
    descriptor = fixture.graphs[GraphArm.FAITHFUL]
    graph = json.loads((FIXTURE_ROOT / descriptor.path).read_bytes())
    graph["project"]["name"] = "Delta State Ledger"
    graph["workspaces"][0]["name"] = "delta-workspace"
    graph["branches"][0]["workspace"] = "delta-workspace"
    for node in graph["nodes"]:
        if node["workspace"] == "codec-refactor":
            node["workspace"] = "delta-workspace"
        if node["short_id"] == "KNOW-3":
            node["short_id"] = "KNOW-7"
            node["title"] = "Epoch boundary rule"
            node["body"] = "Canonical snapshots retain the epoch boundary sequence."
    component_10 = dict(next(node for node in graph["nodes"] if node["short_id"] == "COMP-2"))
    component_10.update(
        {
            "short_id": "COMP-10",
            "parent": "COMP-2",
            "title": "Delta snapshot codec",
            "body": "Serializes delta state snapshots.",
            "created_at": "2026-01-02T03:06:10Z",
            "updated_at": "2026-01-02T03:06:11Z",
            "content_updated_at": "2026-01-02T03:06:12Z",
            "embedding_updated_at": "2026-01-02T03:06:12Z",
            "anchor_files": ["src/delta_snapshot.rs"],
            "anchor_symbols": ["DeltaSnapshotCodec"],
        }
    )
    graph["nodes"].append(component_10)
    graph["nodes"].sort(key=lambda node: _short_id_key(node["short_id"]))
    for edge in graph["edges"]:
        if edge["src"] == "KNOW-3":
            edge["src"] = "KNOW-7"
        if edge["dst"] == "KNOW-3":
            edge["dst"] = "KNOW-7"
    graph["edges"].sort(
        key=lambda edge: (
            _short_id_key(edge["src"]),
            _short_id_key(edge["dst"]),
            edge["relation"],
        )
    )
    for comment in graph["comments"]:
        if comment["node"] == "KNOW-3":
            comment["node"] = "KNOW-7"
    raw = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    (tmp_path / "graphs").mkdir()
    (tmp_path / "corpora").mkdir()
    (tmp_path / "graphs" / "faithful.json").write_bytes(raw)
    (tmp_path / "corpora" / "evidence.json").write_bytes(
        (FIXTURE_ROOT / fixture.evidence_corpus.path).read_bytes()
    )
    variant_descriptor = descriptor.model_copy(
        update={"sha256": hashlib.sha256(raw).hexdigest()}
    )
    variant = fixture.model_copy(
        update={
            "fixture_id": "dotstate-local-variant-v1",
            "graphs": {GraphArm.FAITHFUL: variant_descriptor},
        }
    )
    path = tmp_path / "fixture.json"
    path.write_text(dump_fixture(variant), encoding="utf-8")
    return path


def _write_unordered_fixture(tmp_path: Path) -> Path:
    fixture = load_fixture(FIXTURE_PATH)
    descriptor = fixture.graphs[GraphArm.FAITHFUL]
    graph = json.loads((FIXTURE_ROOT / descriptor.path).read_bytes())
    graph["edges"].reverse()
    raw = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    (tmp_path / "graphs").mkdir()
    (tmp_path / "corpora").mkdir()
    (tmp_path / "graphs" / "faithful.json").write_bytes(raw)
    (tmp_path / "corpora" / "evidence.json").write_bytes(
        (FIXTURE_ROOT / fixture.evidence_corpus.path).read_bytes()
    )
    malformed = fixture.model_copy(
        update={
            "fixture_id": "dotstate-unordered-v1",
            "graphs": {
                GraphArm.FAITHFUL: descriptor.model_copy(
                    update={"sha256": hashlib.sha256(raw).hexdigest()}
                )
            },
        }
    )
    path = tmp_path / "fixture.json"
    path.write_text(dump_fixture(malformed), encoding="utf-8")
    return path


def test_smoke_graph_is_canonical_and_contains_no_harness_markers() -> None:
    fixture = load_fixture(FIXTURE_PATH)
    descriptor = fixture.graphs[GraphArm.FAITHFUL]
    raw = (FIXTURE_ROOT / descriptor.path).read_bytes()
    canonical = json.dumps(
        json.loads(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert raw == canonical
    assert hashlib.sha256(raw).hexdigest() == descriptor.sha256
    lowered = raw.lower()
    for marker in (b"benchmark", b"fixture", b"evaluation", b"stele"):
        assert marker not in lowered


@pytest.mark.skipif(
    os.environ.get("STELE_BENCH_LIVE") != "1",
    reason="set STELE_BENCH_LIVE=1 to run the local Supabase round trip",
)
def test_private_local_provisioner_round_trips_twice_through_public_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _set_attempt_root(monkeypatch, tmp_path)
    run_ids = ("local-restore-1", "local-restore-2")
    for run_id in run_ids:
        _prepare_attempt(run_id)

    async def run() -> None:
        fixture = load_fixture(FIXTURE_PATH)
        provisioner = _provisioner(FIXTURE_ROOT)
        restored = []
        try:
            for index, run_id in enumerate(run_ids):
                receipt = await provisioner.restore_with_receipt(
                    fixture,
                    GraphArm.FAITHFUL,
                    _identity(run_id, index),
                )
                active_project_path = (
                    _attempt_directory(root, run_id) / "project.json"
                )
                assert active_project_path.is_file()
                active_project = _read_attempt_file(root, run_id, "project.json")
                assert active_project == {
                    "projectId": receipt.project.project_id,
                    "slug": active_project["slug"],
                    "name": "Dotstate Memory",
                    "defaultBranch": "main",
                    "activeWorkspaceIds": [],
                }
                assert isinstance(active_project["slug"], str)
                assert active_project["slug"].startswith("dotstate-memory-")
                assert active_project_path.parent == _attempt_directory(root, run_id)
                assert {
                    path.name for path in active_project_path.parent.iterdir()
                } == {
                    "credentials.json",
                    "preferences.json",
                    "project.json",
                    "state.json",
                }
                restored.append(receipt.project)
                snapshot = await provisioner.snapshot_with_receipt(receipt.project)
                observation = snapshot.observation(receipt)
                if index == 0:
                    first = observation
                else:
                    second = observation
            report = build_restoration_report(
                fixture, GraphArm.FAITHFUL, first, second, score_tolerance=0
            )
            assert report.passed, report.mismatches
            assert first.state.canonical_state.sha256 == fixture.graphs[GraphArm.FAITHFUL].sha256
            assert first.state.declared_node_count == 5
            assert first.state.declared_edge_count == 2
            probes = {probe.kind.value: probe for probe in first.probes}
            assert probes["fts"].stable_ids
            assert probes["semantic"].stable_ids
            assert probes["graph"].stable_ids == ("KNOW-3", "KNOW-5", "TASK-4")
        finally:
            for project in reversed(restored):
                await provisioner.destroy(project)
            for run_id in run_ids:
                _cleanup_attempt(run_id)

    asyncio.run(run())
    assert not any(root.iterdir())


@pytest.mark.skipif(
    os.environ.get("STELE_BENCH_LIVE") != "1",
    reason="set STELE_BENCH_LIVE=1 to run the local Supabase round trip",
)
def test_probes_are_derived_from_a_different_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _set_attempt_root(monkeypatch, tmp_path)
    run_id = "variant-restore"
    _prepare_attempt(run_id)

    async def run() -> None:
        fixture_root = tmp_path / "variant-fixture"
        fixture_root.mkdir()
        fixture_path = _write_variant_fixture(fixture_root)
        fixture = load_fixture(fixture_path)
        provisioner = _provisioner(fixture_root)
        receipt = await provisioner.restore_with_receipt(
            fixture, GraphArm.FAITHFUL, _identity(run_id, 0)
        )
        try:
            assert (_attempt_directory(root, run_id) / "project.json").is_file()
            snapshot = await provisioner.snapshot_with_receipt(receipt.project)
            assert snapshot.state.canonical_state.sha256 == fixture.graphs[GraphArm.FAITHFUL].sha256
            assert snapshot.state.declared_node_count == 6
            probes = {probe.kind.value: probe for probe in snapshot.probes}
            assert "KNOW-7" in probes["fts"].stable_ids
            assert probes["semantic"].stable_ids[0] == "KNOW-7"
            assert probes["graph"].stable_ids == ("KNOW-7", "KNOW-5", "TASK-4")
        finally:
            _cleanup_attempt(run_id)
        assert _db_psql(
            f"select count(*) from projects where id='{receipt.project.project_id}'::uuid"
        ) == "0"

    asyncio.run(run())
    assert not _attempt_directory(root, run_id).exists()


@pytest.mark.skipif(
    os.environ.get("STELE_BENCH_LIVE") != "1",
    reason="set STELE_BENCH_LIVE=1 to run the local Supabase round trip",
)
def test_attempt_credentials_are_the_restored_project_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _set_attempt_root(monkeypatch, tmp_path)
    owner_run = "authenticated-owner"
    outsider_run = "authenticated-outsider"
    _prepare_attempt(owner_run)
    _prepare_attempt(outsider_run)
    owner_state = _read_attempt_file(root, owner_run, "state.json")
    outsider_state = _read_attempt_file(root, outsider_run, "state.json")
    owner_credentials = _read_attempt_file(root, owner_run, "credentials.json")
    outsider_credentials = _read_attempt_file(root, outsider_run, "credentials.json")
    preferences = _read_attempt_file(root, owner_run, "preferences.json")
    assert preferences == {"harnesses": ["claude"]}

    async def run() -> None:
        fixture = load_fixture(FIXTURE_PATH)
        provisioner = _provisioner(FIXTURE_ROOT)
        receipt = await provisioner.restore_with_receipt(
            fixture, GraphArm.FAITHFUL, _identity(owner_run, 0)
        )
        try:
            receipt_json = receipt.model_dump_json()
            assert "access_token" not in receipt_json
            assert "refresh_token" not in receipt_json
            assert "active_project" not in receipt_json
            assert "project.json" not in receipt_json
            owner_rows = _api_nodes(
                str(owner_credentials["access_token"]), receipt.project.project_id
            )
            outsider_rows = _api_nodes(
                str(outsider_credentials["access_token"]), receipt.project.project_id
            )
            assert owner_rows == [{"short_id": "KNOW-3", "title": "Snapshot ordering rule"}]
            assert outsider_rows == []
            snapshot = await provisioner.snapshot_with_receipt(receipt.project)
            assert snapshot.state.canonical_state.sha256 == fixture.graphs[GraphArm.FAITHFUL].sha256
            assert snapshot.queue_after_count == receipt.queue_before_count
        finally:
            await provisioner.destroy(receipt.project)
            _cleanup_attempt(owner_run)
            _cleanup_attempt(outsider_run)

    asyncio.run(run())
    assert not _attempt_directory(root, owner_run).exists()
    assert not _attempt_directory(root, outsider_run).exists()
    assert _db_psql(
        "select count(*) from auth.users where id in "
        f"('{owner_state['user_id']}'::uuid, '{outsider_state['user_id']}'::uuid)"
    ) == "0"


@pytest.mark.skipif(
    os.environ.get("STELE_BENCH_LIVE") != "1",
    reason="set STELE_BENCH_LIVE=1 to run the local Supabase round trip",
)
def test_unmarked_project_cannot_be_snapshotted_or_destroyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_attempt_root(monkeypatch, tmp_path)
    _prepare_attempt("not-owned")
    owner_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    slug = f"ordinary-project-{uuid.uuid4().hex[:12]}"
    _db_psql(
        f"""
        begin;
        insert into auth.users (
          id, aud, role, email, encrypted_password, email_confirmed_at,
          raw_app_meta_data, raw_user_meta_data, created_at, updated_at
        ) values (
          '{owner_id}'::uuid, 'authenticated', 'authenticated',
          '{owner_id}@example.invalid', 'not-a-login', now(), '{{}}'::jsonb, '{{}}'::jsonb,
          now(), now()
        );
        insert into projects (
          id, slug, name, created_by_user_id, created_at, settings, default_branch, visibility
        ) values (
          '{project_id}'::uuid, '{slug}', 'Ordinary local project', '{owner_id}'::uuid,
          now(), '{{}}'::jsonb, 'main', 'private'
        );
        insert into project_members(project_id, user_id, role)
        values ('{project_id}'::uuid, '{owner_id}'::uuid, 'owner');
        select '{project_id}';
        commit;
        """
    )

    async def run() -> None:
        provisioner = _provisioner(FIXTURE_ROOT)
        unrelated = ProvisionedProject(
            project_id=project_id,
            fixture_id="not-owned",
            run_id="not-owned",
            graph_arm=GraphArm.FAITHFUL,
        )
        with pytest.raises(ProvisionerExitError):
            await provisioner.snapshot(unrelated)
        with pytest.raises(ProvisionerExitError):
            await provisioner.destroy(unrelated)

    try:
        asyncio.run(run())
        assert _db_psql(
            f"select count(*) from projects where id='{project_id}'::uuid"
        ) == "1"
    finally:
        _db_psql(
            f"""
            begin;
            delete from projects where id='{project_id}'::uuid;
            delete from auth.users where id='{owner_id}'::uuid;
            select count(*) from projects where id='{project_id}'::uuid;
            commit;
            """
        )
        _cleanup_attempt("not-owned")


@pytest.mark.skipif(
    os.environ.get("STELE_BENCH_LIVE") != "1",
    reason="set STELE_BENCH_LIVE=1 to run the local Supabase round trip",
)
def test_public_project_identity_is_bound_to_the_private_owner_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_attempt_root(monkeypatch, tmp_path)
    _prepare_attempt("identity-bound")
    _prepare_attempt("altered-run")

    async def run() -> None:
        fixture = load_fixture(FIXTURE_PATH)
        provisioner = _provisioner(FIXTURE_ROOT)
        receipt = await provisioner.restore_with_receipt(
            fixture, GraphArm.FAITHFUL, _identity("identity-bound", 0)
        )
        valid = receipt.project
        altered = (
            valid.model_copy(update={"fixture_id": "altered-fixture"}),
            valid.model_copy(update={"run_id": "altered-run"}),
            valid.model_copy(update={"graph_arm": GraphArm.ANTAGONIST}),
        )
        try:
            for invalid in altered:
                with pytest.raises(ProvisionerExitError):
                    await provisioner.snapshot(invalid)
                with pytest.raises(ProvisionerExitError):
                    await provisioner.destroy(invalid)
            snapshot = await provisioner.snapshot(valid)
            assert snapshot.project_id == valid.project_id
        finally:
            await provisioner.destroy(valid)
            _cleanup_attempt("identity-bound")
            _cleanup_attempt("altered-run")

    asyncio.run(run())


@pytest.mark.skipif(
    os.environ.get("STELE_BENCH_LIVE") != "1",
    reason="set STELE_BENCH_LIVE=1 to run the local Supabase round trip",
)
def test_noncanonical_array_order_is_rejected_before_database_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _set_attempt_root(monkeypatch, tmp_path)
    run_id = "unordered-restore"
    _prepare_attempt(run_id)
    state = _read_attempt_file(root, run_id, "state.json")
    fixture_root = tmp_path / "unordered-fixture"
    fixture_root.mkdir()
    fixture = load_fixture(_write_unordered_fixture(fixture_root))
    provisioner = _provisioner(fixture_root)
    before = _db_psql(
        "select count(*)::text || ':' || (select count(*) from pgmq.q_embed_jobs)::text "
        "from projects"
    )

    async def run() -> None:
        with pytest.raises(ProvisionerExitError):
            await provisioner.restore_with_receipt(
                fixture, GraphArm.FAITHFUL, _identity(run_id, 0)
            )

    asyncio.run(run())
    after = _db_psql(
        "select count(*)::text || ':' || (select count(*) from pgmq.q_embed_jobs)::text "
        "from projects"
    )
    assert after == before
    assert not _attempt_directory(root, run_id).exists()
    assert _db_psql(
        f"select count(*) from auth.users where id='{state['user_id']}'::uuid"
    ) == "0"
