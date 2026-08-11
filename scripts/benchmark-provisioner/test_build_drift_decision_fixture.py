from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("build_drift_decision_fixture.py")
SPEC = importlib.util.spec_from_file_location("build_drift_decision_fixture", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


def test_build_writes_one_fixture_carrying_the_seeded_decision(tmp_path: Path) -> None:
    digest = builder.build(tmp_path)

    fixture = json.loads((tmp_path / "fixture.json").read_text())
    assert fixture["cohort_id"] == builder.COHORT_ID
    assert fixture["fixture_id"] == builder.FIXTURE_ID
    assert fixture["graphs"]["faithful"]["sha256"] == digest
    # The decision's title is what the injection channels surface verbatim.
    assert fixture["memory_marker"] == builder.DECISION_TITLE

    graph = json.loads((tmp_path / "graphs" / "faithful.json").read_text())
    decision = next(node for node in graph["nodes"] if node["short_id"] == "KNOW-1")
    assert decision["category"] == "decision"
    assert decision["status"] == "active"
    # Anchored to the service a drift feature would touch, so the file-read
    # nudge is a second, independent path for recall to fire.
    assert decision["anchor_files"] == [builder.ANCHORED_FILE]


def test_the_decision_body_carries_both_unguessable_markers() -> None:
    """The verifier greps for these; if the body loses them the oracle breaks."""
    graph = builder.build_graph(builder._forge._placeholder_embedding)
    decision = next(node for node in graph["nodes"] if node["short_id"] == "KNOW-1")
    body = decision["body"].casefold()

    assert "watchdog-2" in body
    assert "8-12% cpu" in body
    # And the challenge's declared solution markers must actually live here --
    # this is the memory under test, so the graph is the one place they belong.
    assert "runs no resident process" in decision["title"].casefold()
    assert "watchdog-2 spike" in body


def test_build_is_idempotent_and_fails_closed_on_drift_without_replace(
    tmp_path: Path,
) -> None:
    first = builder.build(tmp_path)
    assert builder.build(tmp_path) == first

    drifted = tmp_path / "fixture.json"
    drifted.write_text(drifted.read_text() + "\n")
    with pytest.raises(builder.FixtureBuildError, match="differs"):
        builder.build(tmp_path)
    builder.build(tmp_path, replace=True)


def test_generated_graph_is_provisioner_compatible(tmp_path: Path) -> None:
    graph = builder.build_graph(builder._forge._placeholder_embedding)
    builder._forge._validate_with_provisioner(graph)
    for node in graph["nodes"]:
        assert len(node["embedding"]) == builder.EMBEDDING_DIMENSIONS
