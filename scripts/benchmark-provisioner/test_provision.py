from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import uuid
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("provision.py")
SPEC = importlib.util.spec_from_file_location("benchmark_provision", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


def _set_attempt_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "attempts"
    monkeypatch.setenv("STELE_BENCH_ATTEMPT_ROOT", str(root))
    return root


def _attempt_directory(root: Path, run_id: str) -> Path:
    return root / hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def test_inspect_counts_agent_authored_knowledge_and_tasks() -> None:
    normalized = " ".join(provision.INSPECT_SQL.split())

    predicate = (
        "( n.created_by_source='agent' or ( n.type='knowledge' "
        "and n.source_type='claude' and n.short_id <> 'KNOW-3' ) )"
    )
    assert normalized.count(predicate) == 2
    assert "n.created_by_source='agent' and n.type='task'" in normalized


def test_prepare_persists_recovery_state_before_signin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _set_attempt_root(monkeypatch, tmp_path)
    run_id = "interrupted-after-signup"
    user_id = str(uuid.uuid4())
    requests: list[tuple[str, dict[str, object]]] = []

    def interrupted_auth(path: str, payload: dict[str, object]) -> dict[str, object]:
        requests.append((path, payload))
        if path == "/auth/v1/signup":
            return {"user": {"id": user_id}}
        raise KeyboardInterrupt

    monkeypatch.setattr(provision, "_auth_post", interrupted_auth)

    with pytest.raises(KeyboardInterrupt):
        provision._prepare({"run_id": run_id, "harnesses": ["claude"]})

    state_path = _attempt_directory(root, run_id) / "state.json"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    state = json.loads(state_path.read_bytes())
    assert state["run_id"] == run_id
    assert state["user_id"] == user_id
    assert len(state["nonce"]) >= 32
    signup_data = requests[0][1]["data"]
    assert isinstance(signup_data, dict)
    recovery = signup_data[provision.ATTEMPT_RECOVERY_MARKER]
    assert recovery == {
        "run_id_sha256": hashlib.sha256(run_id.encode("utf-8")).hexdigest(),
        "nonce_sha256": hashlib.sha256(state["nonce"].encode("utf-8")).hexdigest(),
    }


def test_cleanup_is_idempotent_when_state_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_attempt_root(monkeypatch, tmp_path)
    run_id = "already-clean"
    statements: list[str] = []
    monkeypatch.setattr(
        provision, "_psql", lambda sql: statements.append(sql) or '{"ok":true}'
    )

    assert provision._cleanup({"run_id": run_id}) == {"ok": True}
    assert provision._cleanup({"run_id": run_id}) == {"ok": True}

    assert len(statements) == 2
    run_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    for sql in statements:
        assert run_hash in sql
        assert "local_graph_attempt_recovery_v1" in sql
        assert "owner.email like 'attempt-%@example.dev'" in sql
        assert "if candidate_count > 1" in sql


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b'{"schema_version":1,"run_id":"wrong"}'],
)
def test_cleanup_uses_narrow_recovery_lookup_for_malformed_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: bytes
) -> None:
    root = _set_attempt_root(monkeypatch, tmp_path)
    run_id = "malformed-state"
    directory = _attempt_directory(root, run_id)
    directory.mkdir(mode=0o700, parents=True)
    state_path = directory / "state.json"
    state_path.write_bytes(payload)
    state_path.chmod(0o600)
    statements: list[str] = []
    monkeypatch.setattr(
        provision, "_psql", lambda sql: statements.append(sql) or '{"ok":true}'
    )

    assert provision._cleanup({"run_id": run_id}) == {"ok": True}

    assert not directory.exists()
    assert len(statements) == 1
    assert hashlib.sha256(run_id.encode("utf-8")).hexdigest() in statements[0]
    assert "multiple recovery principals match this attempt" in statements[0]
    assert "delete from auth.users where id=candidate_user" in statements[0]


def test_cleanup_with_valid_state_keeps_nonce_bound_identity_protection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _set_attempt_root(monkeypatch, tmp_path)
    run_id = "nonce-bound"
    user_id = str(uuid.uuid4())
    nonce = "n" * 48
    directory = _attempt_directory(root, run_id)
    directory.mkdir(mode=0o700, parents=True)
    provision._write_private_json(
        directory / "state.json",
        {
            "schema_version": provision.ATTEMPT_STATE_VERSION,
            "run_id": run_id,
            "user_id": user_id,
            "nonce": nonce,
        },
    )
    statements: list[str] = []
    monkeypatch.setattr(
        provision, "_psql", lambda sql: statements.append(sql) or '{"users":1}'
    )

    assert provision._cleanup({"run_id": run_id}) == {"ok": True}

    assert len(statements) == 1
    assert user_id in statements[0]
    assert hashlib.sha256(nonce.encode("utf-8")).hexdigest() in statements[0]
    assert "recovery_candidates" not in statements[0]
    assert not directory.exists()


def test_cleanup_rejects_symlinked_attempt_without_touching_victim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _set_attempt_root(monkeypatch, tmp_path)
    root.mkdir(mode=0o700)
    run_id = "symlinked-attempt"
    victim = tmp_path / "victim"
    victim.mkdir()
    credentials = victim / "credentials.json"
    credentials.write_text('{"do_not_delete":true}', encoding="utf-8")
    attempt_entry = _attempt_directory(root, run_id)
    attempt_entry.symlink_to(victim, target_is_directory=True)

    def unexpected_database_call(_: str) -> str:
        raise AssertionError("unsafe attempt entries must fail before database cleanup")

    monkeypatch.setattr(provision, "_psql", unexpected_database_call)

    with pytest.raises(
        provision.ProvisionerError,
        match="attempt entry must be a directory, not a link",
    ):
        provision._cleanup({"run_id": run_id})

    assert attempt_entry.is_symlink()
    assert credentials.read_text(encoding="utf-8") == '{"do_not_delete":true}'


def test_cleanup_rejects_non_directory_attempt_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _set_attempt_root(monkeypatch, tmp_path)
    root.mkdir(mode=0o700)
    run_id = "file-attempt"
    attempt_entry = _attempt_directory(root, run_id)
    attempt_entry.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        provision,
        "_psql",
        lambda _: (_ for _ in ()).throw(
            AssertionError("unsafe attempt entries must fail before database cleanup")
        ),
    )

    with pytest.raises(
        provision.ProvisionerError,
        match="attempt entry must be a directory, not a link",
    ):
        provision._cleanup({"run_id": run_id})

    assert attempt_entry.read_text(encoding="utf-8") == "not a directory"
