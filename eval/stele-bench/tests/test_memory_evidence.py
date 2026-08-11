from __future__ import annotations

import json
from pathlib import Path

import pytest

from stele_bench.memory_evidence import (
    TrajectoryShapeError,
    detect_injected_marker,
    detect_injection_in_session,
    detect_stele_delivery_in_session,
    write_memory_evidence,
)

MARKER = "must also get a matching bullet in CHANGELOG.md"
TITLE_MARKER = "Close out a new dotfile candidate with a CHANGELOG entry"


def _hook_event(hook_name: str, content: str) -> dict[str, object]:
    """One Claude Code session line carrying an injected-context hook attachment."""
    return {
        "type": "attachment",
        "attachment": {
            "type": "hook_additional_context",
            "hookName": hook_name,
            "content": [content],
        },
    }


def _trajectory(steps: list[dict[str, object]]) -> dict[str, object]:
    return {"schema_version": "ATIF-v1.7", "steps": steps}


def test_marker_in_a_user_step_counts_as_injected() -> None:
    trajectory = _trajectory(
        [
            {"source": "user", "message": "Add a dotfile candidate for .windsurf/rules."},
            {"source": "agent", "message": "Adding the entry now."},
            {
                "source": "user",
                "message": f"<system-reminder>Lesson: {MARKER}</system-reminder>Also wrap it up.",
            },
        ]
    )
    evidence = detect_injected_marker(trajectory, MARKER)
    assert evidence.injected is True
    assert evidence.injected_step_indices == (2,)


def test_marker_only_in_an_agent_step_does_not_count_as_injected() -> None:
    trajectory = _trajectory(
        [
            {"source": "user", "message": "Add a dotfile candidate for .windsurf/rules."},
            {
                "source": "agent",
                "message": f"I'll also check CHANGELOG.md: {MARKER}, so I'll add a bullet.",
            },
        ]
    )
    evidence = detect_injected_marker(trajectory, MARKER)
    assert evidence.injected is False
    assert evidence.injected_step_indices == ()


def test_system_sourced_step_also_counts_as_non_agent_injection() -> None:
    trajectory = _trajectory([{"source": "system", "message": MARKER}])
    evidence = detect_injected_marker(trajectory, MARKER)
    assert evidence.injected is True


def test_rejects_blank_marker_and_non_atif_trajectory() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        detect_injected_marker(_trajectory([]), "   ")
    with pytest.raises(TrajectoryShapeError):
        detect_injected_marker({"schema_version": "ATIF-v1.7"}, MARKER)
    with pytest.raises(TrajectoryShapeError):
        detect_injected_marker(["not", "a", "dict"], MARKER)


def test_session_injection_records_both_channels_that_carried_the_marker() -> None:
    # Mirrors a real transcript: the Auto-RAG walk and the anchor recall both
    # surface the lesson title, tagged by distinct stele provenance comments.
    rag = f"Stele context:\n  • [KNOW-1] lesson: '{TITLE_MARKER}'\n<!-- stele:rag KNOW-1 COMP-1 -->"
    nudge = (
        f'Stele — a lesson names the file: KNOW-1 — "{TITLE_MARKER}".\n'
        "<!-- stele:nudge recall-file -->"
    )
    events = [
        _hook_event("UserPromptSubmit", rag),
        {"type": "assistant", "message": {"role": "assistant", "content": "editing"}},
        _hook_event("PostToolUse:Edit", nudge),
    ]
    evidence = detect_injection_in_session(events, TITLE_MARKER)
    assert evidence.injected is True
    assert evidence.occurrences == 2
    assert evidence.channels == ("nudge:recall-file", "rag")


def test_session_injection_ignores_agent_authored_matches() -> None:
    # The agent typing the title in an ordinary assistant message is not evidence
    # Stele delivered it — only hook_additional_context attachments count.
    events = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": f"I recall {TITLE_MARKER}"},
        },
        {"type": "user", "message": {"role": "user", "content": TITLE_MARKER}},
    ]
    evidence = detect_injection_in_session(events, TITLE_MARKER)
    assert evidence.injected is False
    assert evidence.channels == ()
    assert evidence.occurrences == 0


def test_session_injection_falls_back_to_hook_name_without_a_stele_comment() -> None:
    events = [_hook_event("UserPromptSubmit", f"context: {TITLE_MARKER}")]
    evidence = detect_injection_in_session(events, TITLE_MARKER)
    assert evidence.injected is True
    assert evidence.channels == ("UserPromptSubmit",)


def test_session_injection_rejects_a_blank_marker() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        detect_injection_in_session([], "   ")


def test_generic_delivery_counts_node_bearing_context_without_a_fixture_marker() -> None:
    events = [
        _hook_event(
            "UserPromptSubmit",
            "Stele context:\n[KNOW-3] decision\n[TASK-2] open\n"
            "<!-- stele:rag KNOW-3 TASK-2 -->",
        ),
        _hook_event(
            "PostToolUse:Edit",
            "Stele recalled KNOW-7 for this file.\n<!-- stele:nudge recall-file -->",
        ),
        _hook_event(
            "PreToolUse:Edit",
            "Search before editing.\n<!-- stele:nudge change -->",
        ),
    ]

    evidence = detect_stele_delivery_in_session(events)

    assert evidence.injected is True
    assert evidence.occurrences == 2
    assert evidence.channels == ("nudge:recall-file", "rag")
    assert evidence.node_ids == ("KNOW-3", "KNOW-7", "TASK-2")


def test_generic_delivery_ignores_agent_text_and_non_node_reminders() -> None:
    events = [
        {"type": "assistant", "message": {"content": "I found KNOW-3"}},
        _hook_event("PreToolUse:Edit", "Reminder.\n<!-- stele:nudge change -->"),
    ]

    assert detect_stele_delivery_in_session(events).as_dict() == {
        "injected": False,
        "channels": [],
        "occurrences": 0,
        "node_ids": [],
    }


def test_write_memory_evidence_persists_verdict_without_the_raw_marker(tmp_path: Path) -> None:
    evidence = detect_injected_marker(
        _trajectory([{"source": "user", "message": MARKER}]), MARKER
    )
    descriptor = write_memory_evidence(evidence, tmp_path, "evidence/memory.json")
    assert descriptor.path == "evidence/memory.json"
    written = json.loads((tmp_path / "evidence/memory.json").read_text())
    assert written["injected"] is True
    assert written["injected_step_indices"] == [0]
    assert MARKER not in (tmp_path / "evidence/memory.json").read_text()
    # The descriptor's own digest is over the persisted payload, not the raw marker.
    import hashlib

    assert descriptor.sha256 == hashlib.sha256(
        (tmp_path / "evidence/memory.json").read_bytes()
    ).hexdigest()
