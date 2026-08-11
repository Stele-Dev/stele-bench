from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import run_compounding_chain as compounding

from stele_bench.preflight import ActiveProjectBinding

PROJECT_ID = "00000000-0000-4000-8000-000000000123"
BINDING = ActiveProjectBinding(
    project_id=PROJECT_ID,
    slug="workspace-ab12",
    name="Workspace",
    default_branch="main",
    active_workspace_ids=(),
)


def _inputs(tmp_path: Path, condition: compounding.Condition) -> compounding.CompoundingInputs:
    artifact = tmp_path / "stele"
    artifact.write_bytes(b"binary")
    artifact.chmod(0o755)
    provisioner = tmp_path / "provision.py"
    provisioner.write_text("#!/bin/sh\n", encoding="utf-8")
    return compounding.CompoundingInputs(
        repository=tmp_path / "source",
        revision="a" * 40,
        attempt_root=tmp_path / "attempts",
        work_root=tmp_path / "work",
        chain_id=f"chain-{condition}",
        condition=condition,
        provisioner_executable=provisioner if condition == "stele-fresh" else None,
        artifact_path=artifact if condition == "stele-fresh" else None,
        artifact_sha256="b" * 64 if condition == "stele-fresh" else None,
        backend_url="http://127.0.0.1:54321" if condition == "stele-fresh" else None,
        backend_anon_key="anon" if condition == "stele-fresh" else None,
    )


def _trajectory(session_id: str, *, stele_read: bool = False) -> dict[str, Any]:
    calls: list[dict[str, object]] = [
        {
            "tool_call_id": "read-1",
            "function_name": "Read",
            "arguments": {"file_path": "src/app.rs"},
        }
    ]
    if stele_read:
        calls.append(
            {
                "tool_call_id": "stele-1",
                "function_name": "mcp__stele__recall",
                "arguments": {"query": "profile picker"},
            }
        )
    calls.append(
        {
            "tool_call_id": "edit-1",
            "function_name": "Edit",
            "arguments": {
                "file_path": compounding.LIVE_IMPLEMENTATION_MARKER,
                "old_string": "old",
                "new_string": "new",
            },
        }
    )
    results = [{"source_call_id": call["tool_call_id"], "content": "ok"} for call in calls]
    if stele_read:
        results[1]["content"] = {
            "short_id": "KNOW-4",
            "body": compounding.LIVE_IMPLEMENTATION_MARKER,
        }
    return {
        "session_id": session_id,
        "steps": [
            {
                "source": "agent",
                "tool_calls": calls,
                "observation": {"results": results},
            }
        ],
    }


def _session(
    tmp_path: Path,
    number: int,
    *,
    session_id: str,
    stele_read: bool = False,
) -> compounding._SessionRun:
    step_root = tmp_path / f"session-{number}"
    (step_root / "agent" / "sessions").mkdir(parents=True)
    response = {
        "cited": number == 1,
        "markers_found": ["Live implementation: src/components/profile_selection_popup.rs"]
        if number == 1
        else [],
    }
    challenge = {
        "reward": 1.0,
        "tokens": {"input": 10, "output": 2, "cache_read": 3, "cache_write": 4},
        "cost": {
            "claude_reported_amount_usd": 0.25,
            "gateway_rate_cross_check_usd": 0.20,
        },
        "timings": {"invocation_wall": 5.0},
        "evidence": {"response_evidence": response},
    }
    return compounding._SessionRun(
        receipt={
            "challenge": challenge,
            "harbor_wall_seconds": 7.0,
            "resolved_model": compounding.DEFAULT_MODEL,
            "gateway_billing": {"delta_usd": "0.10"},
        },
        trial_dir=step_root,
        step_root=step_root,
        trajectory=_trajectory(session_id, stele_read=stele_read),
        source_commit="a" * 40,
        source_tree_sha256="c" * 64,
    )


def _snapshot(*, agent_nodes: list[dict[str, object]] | None = None) -> dict[str, object]:
    projects: list[dict[str, object]] = []
    if agent_nodes is not None:
        projects = [
            {
                "project_id": PROJECT_ID,
                "slug": BINDING.slug,
                "agent_nodes": agent_nodes,
            }
        ]
    return {
        "projects": projects,
        "project_count": len(projects),
        "sha256": compounding._sha256_json(projects),
    }


@pytest.mark.parametrize(
    "tool_name",
    (
        "mcp__stele__recall",
        "mcp__stele__search",
        "mcp__stele__search_nodes",
        "mcp__stele__get_node",
    ),
)
def test_stele_read_detection_covers_flat_and_dispatch_surfaces(tool_name: str) -> None:
    trajectory = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [{"function_name": tool_name, "arguments": {}}],
            }
        ]
    }

    assert compounding._stele_reads(trajectory) == {
        "count": 1,
        "tools": [tool_name],
        "matching_count": 0,
        "matching_tools": [],
        "matching_node_ids": [],
    }


def test_stele_read_detection_ignores_write_tools() -> None:
    trajectory = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [
                    {
                        "function_name": "mcp__stele__add_knowledge",
                        "arguments": {},
                    }
                ],
            }
        ]
    }

    assert compounding._stele_reads(trajectory) == {
        "count": 0,
        "tools": [],
        "matching_count": 0,
        "matching_tools": [],
        "matching_node_ids": [],
    }


def test_treatment_carries_only_the_graph_into_a_distinct_second_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path, "stele-fresh")
    relevant = {
        "short_id": "KNOW-4",
        "version": 1,
        "type": "knowledge",
        "status": "active",
        "title": "Profile selection live path",
        "body": (
            "ProfileSelectionPopup in src/components/profile_selection_popup.rs is live; "
            "ProfileSelectionScreen is the inactive alternative."
        ),
        "anchor_files": [compounding.LIVE_IMPLEMENTATION_MARKER],
        "anchor_symbols": ["ProfileSelectionPopup"],
    }
    empty = _snapshot()
    after_one = _snapshot(agent_nodes=[relevant])
    after_two = _snapshot(agent_nodes=[relevant, {"short_id": "TASK-5"}])
    inspections = iter((empty, after_one, after_one, after_two))
    monkeypatch.setattr(compounding, "_inspect_graph", lambda _inputs: next(inspections))
    monkeypatch.setattr(compounding, "_created_binding", lambda _session: BINDING)
    sessions = {
        1: _session(tmp_path, 1, session_id="session-one"),
        2: _session(tmp_path, 2, session_id="session-two", stele_read=True),
    }
    binding_paths: list[Path | None] = []

    def fake_run_session(
        _inputs: compounding.CompoundingInputs,
        *,
        session_number: int,
        binding_path: Path | None,
    ) -> compounding._SessionRun:
        binding_paths.append(binding_path)
        return sessions[session_number]

    monkeypatch.setattr(compounding, "_run_session", fake_run_session)

    def private_command(
        _path: Path, operation: str, _payload: object, *, timeout_seconds: float
    ) -> dict[str, object]:
        assert timeout_seconds == inputs.timeout_seconds
        assert operation in {"prepare", "cleanup"}
        return {"ok": True}

    monkeypatch.setattr(compounding.support, "_private_command", private_command)

    receipt = compounding.run_compounding_chain(inputs)

    assert receipt["outcome"] == "completed"
    assert receipt["boundary"]["only_graph_crossed_boundary"] is True
    assert receipt["boundary"]["distinct_conversations"] is True
    assert receipt["boundary"]["source_reset_from_pinned_commit"] is True
    assert receipt["graph_intake"]["relevant_node_ids"] == ["KNOW-4"]
    assert receipt["graph_intake"]["deliverable"] is True
    assert receipt["memory_delivery"]["explicit_reads"]["count"] == 1
    assert receipt["memory_delivery"]["explicit_reads"]["matching_count"] == 1
    assert receipt["first_live_edit"] == {
        "tool_call_index": 3,
        "investigative_calls_before": 1,
    }
    assert receipt["cumulative"]["claude_reported_usd"] == 0.5
    assert binding_paths[0] is None
    assert binding_paths[1] is not None
    assert json.loads(binding_paths[1].read_text(encoding="utf-8")) == BINDING.as_cache_object()
    assert receipt["cleanup_completed"] is True


def test_control_uses_two_fresh_conversations_and_no_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path, "no-stele")
    sessions = {
        1: _session(tmp_path, 1, session_id="control-one"),
        2: _session(tmp_path, 2, session_id="control-two"),
    }
    monkeypatch.setattr(
        compounding,
        "_run_session",
        lambda _inputs, *, session_number, binding_path: sessions[session_number],
    )

    receipt = compounding.run_compounding_chain(inputs)

    assert receipt["outcome"] == "completed"
    assert receipt["graph_intake"] is None
    assert receipt["memory_delivery"] is None
    assert receipt["boundary"]["only_graph_crossed_boundary"] is True


def test_reusing_the_same_conversation_invalidates_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path, "no-stele")
    sessions = {
        1: _session(tmp_path, 1, session_id="same-session"),
        2: _session(tmp_path, 2, session_id="same-session"),
    }
    monkeypatch.setattr(
        compounding,
        "_run_session",
        lambda _inputs, *, session_number, binding_path: sessions[session_number],
    )

    receipt = compounding.run_compounding_chain(inputs)

    assert receipt["outcome"] == "invalid"
    assert receipt["boundary"]["distinct_conversations"] is False
    assert receipt["boundary"]["only_graph_crossed_boundary"] is False


def test_created_binding_is_read_losslessly_from_the_agent_report(tmp_path: Path) -> None:
    session = _session(tmp_path, 1, session_id="session-one")
    report = session.step_root / "agent" / compounding.CREATED_PROJECT_REPORT_NAME
    report.write_text(
        json.dumps(
            {
                "project_id": BINDING.project_id,
                "slug": BINDING.slug,
                "binding": BINDING.as_cache_object(),
            }
        ),
        encoding="utf-8",
    )

    assert compounding._created_binding(session) == BINDING


def test_intake_requires_an_active_relevant_node() -> None:
    archived = {
        "short_id": "KNOW-4",
        "status": "archived",
        "body": (
            "ProfileSelectionPopup in src/components/profile_selection_popup.rs is live; "
            "ProfileSelectionScreen is the alternative."
        ),
    }

    intake = compounding._graph_intake(_snapshot(agent_nodes=[archived]))

    assert intake["written"] is True
    assert intake["relevant"] is False


def test_unrelated_stele_read_does_not_count_as_delivery() -> None:
    trajectory = _trajectory("session-two", stele_read=True)
    results = trajectory["steps"][0]["observation"]["results"]
    results[1]["content"] = {"short_id": "TASK-9", "title": "Welcome"}

    reads = compounding._stele_reads(trajectory, ["KNOW-4"])

    assert reads["count"] == 1
    assert reads["matching_count"] == 0
    assert reads["matching_node_ids"] == []


def test_budget_is_fixed_at_ten_dollars_or_less(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at most ten"):
        compounding.CompoundingInputs(
            repository=tmp_path,
            revision="a" * 40,
            attempt_root=tmp_path / "attempts",
            work_root=tmp_path / "work",
            chain_id="too-large",
            condition="no-stele",
            max_budget_usd="10.01",
        )


def test_checked_in_k5_manifest_authenticates() -> None:
    inputs = compounding.CompoundingInputs(
        repository=Path("/unused"),
        revision=None,
        attempt_root=Path("/unused-attempts"),
        work_root=Path("/unused-work"),
        chain_id="manifest-check",
        condition="no-stele",
        chain_manifest_path=compounding.DEFAULT_CHAIN_MANIFEST,
        max_budget_usd="3.00",
    )

    chain = compounding._chain_spec(inputs)

    assert chain.experiment_id == "click-clirunner-k5-v1"
    assert len(chain.sessions) == 5
    assert [stage.index for stage in chain.sessions] == [1, 2, 3, 4, 5]
    assert all(stage.history is not None for stage in chain.sessions)
    assert chain.sessions[-1].challenge_id == "click-clirunner-capture-modes"


def test_checked_in_fieldnote_q1_doctrine_manifest_authenticates() -> None:
    manifest = (
        Path(__file__).resolve().parents[2]
        / "eval"
        / "stele-bench"
        / "preregistrations"
        / "fieldnote-q1-doctrine-v1.json"
    )
    inputs = compounding.CompoundingInputs(
        repository=Path("/unused"),
        revision=None,
        attempt_root=Path("/unused-attempts"),
        work_root=Path("/unused-work"),
        chain_id="fieldnote-q1-doctrine-manifest-check",
        condition="no-stele",
        chain_manifest_path=manifest,
        max_budget_usd="3.00",
    )

    chain = compounding._chain_spec(inputs)

    assert chain.experiment_id == "fieldnote-q1-doctrine-v1"
    assert len(chain.sessions) == 1
    assert chain.sessions[0].challenge_id == "fieldnote-milestone-1"
    assert chain.sessions[0].history is not None


def test_k5_treatment_reports_per_session_graph_writes_and_exact_later_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = replace(
        _inputs(tmp_path, "stele-fresh"),
        revision=None,
        chain_manifest_path=compounding.DEFAULT_CHAIN_MANIFEST,
        max_budget_usd="3.00",
    )
    chain = compounding._chain_spec(inputs)
    node_v1 = {
        "short_id": "KNOW-4",
        "version": 1,
        "type": "knowledge",
        "status": "active",
        "title": "Stream ownership",
        "body": "NamedTextIOWrapper must not close the StreamMixer buffers.",
    }
    empty = _snapshot()
    with_node = _snapshot(agent_nodes=[node_v1])
    inspections = iter(
        (
            empty,
            with_node,
            with_node,
            with_node,
            with_node,
            with_node,
            with_node,
            with_node,
            with_node,
            with_node,
        )
    )
    monkeypatch.setattr(compounding, "_inspect_graph", lambda _inputs: next(inspections))
    monkeypatch.setattr(compounding, "_created_binding", lambda _session: BINDING)

    sessions: dict[int, compounding._SessionRun] = {}
    for stage in chain.sessions:
        original = _session(
            tmp_path,
            stage.index,
            session_id=f"k5-session-{stage.index}",
            stele_read=stage.index == 2,
        )
        sessions[stage.index] = replace(
            original,
            source_commit=stage.base_revision,
            source_base_tree=stage.base_tree,
            history=stage.history,
        )
    transcript = sessions[2].step_root / "agent/sessions/session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "attachment": {
                    "type": "hook_additional_context",
                    "hookName": "UserPromptSubmit",
                    "content": ["[KNOW-4] Stream ownership\n<!-- stele:rag KNOW-4 -->"],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        compounding,
        "_run_session",
        lambda _inputs, *, session_number, binding_path: sessions[session_number],
    )
    monkeypatch.setattr(
        compounding.support,
        "_private_command",
        lambda _path, operation, _payload, *, timeout_seconds: {"ok": True},
    )

    receipt = compounding.run_compounding_chain(inputs)

    assert receipt["outcome"] == "completed"
    assert receipt["session_count"] == 5
    assert len(receipt["sessions"]) == 5
    assert len(receipt["cumulative_prefixes"]) == 5
    assert receipt["cumulative_prefixes"][-1]["gateway_observed_usd"] == 0.5
    assert receipt["graph_activity"][0]["created_count"] == 1
    assert receipt["graph_activity"][1]["written"] is False
    assert receipt["memory_delivery"][1]["delivered_prior_node"] is True
    assert receipt["memory_delivery"][1]["exact_prior_versions"] == [
        {"short_id": "KNOW-4", "version": 1}
    ]
    assert receipt["boundary"]["source_boundary_checks"] == [True] * 5
    assert receipt["boundary"]["graph_boundary_checks"] == [True] * 4
    assert receipt["boundary"]["only_graph_crossed_boundary"] is True


def test_k5_external_source_access_invalidates_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = replace(
        _inputs(tmp_path, "no-stele"),
        revision=None,
        chain_manifest_path=compounding.DEFAULT_CHAIN_MANIFEST,
        max_budget_usd="3.00",
    )
    chain = compounding._chain_spec(inputs)
    sessions: dict[int, compounding._SessionRun] = {}
    for stage in chain.sessions:
        original = _session(tmp_path, stage.index, session_id=f"control-{stage.index}")
        original.receipt["challenge"].setdefault("evidence", {})[
            "external_source_access_detected"
        ] = stage.index == 3
        sessions[stage.index] = replace(
            original,
            source_commit=stage.base_revision,
            source_base_tree=stage.base_tree,
            history=stage.history,
        )
    monkeypatch.setattr(
        compounding,
        "_run_session",
        lambda _inputs, *, session_number, binding_path: sessions[session_number],
    )

    receipt = compounding.run_compounding_chain(inputs)

    assert receipt["outcome"] == "invalid"
    assert receipt["boundary"]["external_source_access_by_session"] == [
        False,
        False,
        True,
        False,
        False,
    ]
