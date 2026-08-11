from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("build_session_boundary_fixtures.py")
SPEC = importlib.util.spec_from_file_location("build_session_boundary_fixtures", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


def test_build_writes_three_variants_with_distinct_anchors_and_shared_lesson(
    tmp_path: Path,
) -> None:
    digests = builder.build(tmp_path)
    assert set(digests) == {"correct-anchor", "wrong-anchor", "no-anchor"}

    anchors: dict[str, list[str]] = {}
    lessons: set[str] = set()
    for variant in digests:
        fixture = json.loads((tmp_path / variant / "fixture.json").read_text())
        assert fixture["cohort_id"] == builder.COHORT_ID
        assert fixture["fixture_id"] == f"session-boundary-recall-{variant}-v1"
        # The injection marker is the lesson title, so post-run detection scans
        # the transcript for exactly what the recall channels surface.
        assert fixture["memory_marker"] == builder.LESSON_TITLE
        graph = json.loads((tmp_path / variant / "graphs" / "faithful.json").read_text())
        lesson = next(node for node in graph["nodes"] if node["short_id"] == "KNOW-1")
        anchors[variant] = lesson["anchor_files"]
        lessons.add(lesson["body"])
        assert lesson["status"] == "active"
        assert lesson["category"] == "lesson"

    assert anchors == {
        "correct-anchor": [builder.TOUCHED_FILE],
        "wrong-anchor": [builder.PLAUSIBLE_WRONG_FILE],
        "no-anchor": [],
    }
    assert builder.TOUCHED_FILE != builder.PLAUSIBLE_WRONG_FILE
    # The lesson content itself is identical across variants; only the anchor differs.
    assert len(lessons) == 1


def test_build_is_idempotent_and_fails_closed_on_drift_without_replace(
    tmp_path: Path,
) -> None:
    first = builder.build(tmp_path)
    second = builder.build(tmp_path)
    assert first == second

    drifted = tmp_path / "correct-anchor" / "fixture.json"
    drifted.write_text(drifted.read_text() + "\n")
    with pytest.raises(builder.FixtureBuildError, match="differs"):
        builder.build(tmp_path)
    builder.build(tmp_path, replace=True)


def test_placeholder_embeddings_are_quantized_and_schema_valid() -> None:
    graph = builder.build_graph([builder.TOUCHED_FILE], builder._placeholder_embedding)
    for node in graph["nodes"]:
        embedding = node["embedding"]
        assert len(embedding) == builder.EMBEDDING_DIMENSIONS
        assert all(isinstance(value, float) for value in embedding)
        # Must already be binary16-quantized (round-trips through struct pack/unpack).
        import struct

        for value in embedding:
            assert struct.unpack(">e", struct.pack(">e", value))[0] == value
    builder._validate_with_provisioner(graph)


def test_build_rejects_an_unknown_variant_shape_via_provisioner() -> None:
    graph = builder.build_graph([], builder._placeholder_embedding)
    graph["nodes"][1]["anchor_files"] = "not-a-list"
    with pytest.raises(builder.FixtureBuildError, match="provisioner-compatible"):
        builder._validate_with_provisioner(graph)


def test_gateway_embedder_embeds_production_input_text_and_memoizes(tmp_path: Path) -> None:
    """The gateway path embeds build_input(title, body) and reuses vectors across variants."""

    calls: list[list[str]] = []

    def fake_requester(inputs: list[str], api_key: str) -> dict[str, object]:
        assert api_key == "eval-key"
        calls.append(inputs)
        # Mirror the Gateway's list-of-embeddings response shape.
        return {
            "object": "list",
            "model": "openai/text-embedding-3-small",
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    # A distinctive but valid vector; the builder quantizes it.
                    "embedding": [0.01 * ((index + 1) % 7)] * builder.EMBEDDING_DIMENSIONS,
                }
                for index, _ in enumerate(inputs)
            ],
        }

    embed = builder.gateway_embedder("eval-key", requester=fake_requester)
    digests = builder.build(tmp_path, embed=embed, model_id=builder.GATEWAY_MODEL_ID)

    # Two distinct node texts (component + lesson), memoized across all three
    # variants → exactly two Gateway calls, not six.
    assert len(calls) == 2
    lesson_input = builder._node_input(builder.LESSON_TITLE, builder.LESSON_BODY)
    component_input = builder._node_input(builder.COMPONENT_TITLE, builder.COMPONENT_BODY)
    assert {tuple(batch) for batch in calls} == {(lesson_input,), (component_input,)}

    for variant in digests:
        fixture = json.loads((tmp_path / variant / "fixture.json").read_text())
        assert fixture["graphs"]["faithful"]["embedding_model_id"] == builder.GATEWAY_MODEL_ID
        graph = json.loads((tmp_path / variant / "graphs" / "faithful.json").read_text())
        for node in graph["nodes"]:
            assert len(node["embedding"]) == builder.EMBEDDING_DIMENSIONS
        builder._validate_with_provisioner(graph)


def test_gateway_embeddings_require_a_key() -> None:
    with pytest.raises(builder.FixtureBuildError, match="AI_GATEWAY_KEY_EVAL is required"):
        builder.gateway_embedder("")
