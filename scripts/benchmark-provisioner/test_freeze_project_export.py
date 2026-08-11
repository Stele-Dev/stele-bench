from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("freeze_project_export.py")
SPEC = importlib.util.spec_from_file_location("freeze_project_export", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
freezer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = freezer
SPEC.loader.exec_module(freezer)


def _export() -> dict[str, object]:
    base_node = {
        "id": "source-uuid",
        "status": "active",
        "parent_short_id": None,
        "title": "Input routing",
        "body": "Focused inputs must consume key events before global navigation.",
        "category": "architecture",
        "topics": ["input", "tui"],
        "assignee_user_id": None,
        "kind": "task",
        "priority": None,
        "due_at": None,
        "target_date": None,
        "position": None,
        "expires_at": None,
        "created_at": "2026-01-02T03:04:05.123456+00:00",
        "updated_at": "2026-01-02T03:05:06.999999+00:00",
        "source": "config:src/app.rs",
        "source_url": None,
        "source_type": "import",
        "anchor_files": ["src/app.rs"],
        "anchor_symbols": ["sync_input_mode"],
    }
    component = {**base_node, "short_id": "COMP-1", "type": "component"}
    knowledge = {
        **base_node,
        "id": "another-source-uuid",
        "short_id": "KNOW-2",
        "type": "knowledge",
        "parent_short_id": "COMP-1",
    }
    return {
        "version": 2,
        "exported_at": "2026-07-23T09:37:09.632Z",
        "project": {
            "id": "project-source-uuid",
            "slug": "dotstate",
            "name": "DotState",
            "description": "Dotfile manager",
            "repo_url": "https://example.invalid/dotstate",
        },
        "components": [component],
        "knowledge": [knowledge],
        "tasks": [],
        "documents": [],
        "diagrams": [],
        "edges": [
            {
                "src_short_id": "KNOW-2",
                "dst_short_id": "COMP-1",
                "relation": "relates-to",
                "created_at": "2026-01-02T03:06:07.777777+00:00",
                "reason": None,
                "evidence": None,
            }
        ],
    }


def _source_sha(value: dict[str, object]) -> str:
    return hashlib.sha256(freezer._canonical_bytes(value)).hexdigest()


def _sidecar(
    value: dict[str, object] | None = None, *, source_sha256: str | None = None
) -> dict[str, object]:
    export = value or _export()
    nodes = [
        node
        for group in ("components", "knowledge", "tasks", "documents", "diagrams")
        for node in export[group]
    ]
    nodes.sort(key=lambda node: freezer._short_id_key(node["short_id"]))
    return {
        "schema_version": 1,
        "source_export_sha256": source_sha256 or _source_sha(export),
        "model": "openai/text-embedding-3-small",
        "dimensions": 1536,
        "embeddings": [
            {
                "short_id": node["short_id"],
                "input_sha256": hashlib.sha256(
                    f"{node['title']}\n\n{node['body']}".strip()[:28_000].encode("utf-8")
                ).hexdigest(),
                "embedding": [0.1] * 1536,
            }
            for node in nodes
        ],
    }


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source.json"
    source_bytes = json.dumps(_export()).encode("utf-8")
    source.write_bytes(source_bytes)
    sidecar = tmp_path / "embeddings.json"
    sidecar.write_text(
        json.dumps(
            _sidecar(source_sha256=hashlib.sha256(source_bytes).hexdigest())
        ),
        encoding="utf-8",
    )
    return source, sidecar


def test_converts_v2_export_deterministically_and_omits_source_uuids() -> None:
    export = _export()
    graph = freezer.convert_export(export, _sidecar(export), _source_sha(export))

    assert graph["schema_version"] == 1
    assert graph["project"]["created_at"] == "2026-01-02T03:04:05Z"
    assert graph["workspaces"] == []
    assert graph["branches"] == []
    assert graph["comments"] == []
    assert [node["short_id"] for node in graph["nodes"]] == ["COMP-1", "KNOW-2"]
    assert graph["nodes"][1]["parent"] == "COMP-1"
    assert graph["nodes"][1]["created_by_source"] == "import"
    assert graph["nodes"][1]["anchor_files"] == ["src/app.rs"]
    assert len(graph["nodes"][1]["embedding"]) == 1536
    assert graph["nodes"][1]["embedding"][0] != 0.1
    assert graph["nodes"][1]["content_updated_at"] == "2026-01-02T03:05:06Z"
    assert graph["edges"][0]["src"] == "KNOW-2"
    assert "source-uuid" not in freezer._canonical_bytes(graph).decode()
    assert freezer.convert_export(export, _sidecar(export), _source_sha(export)) == graph


def test_maps_agent_provenance_to_the_portable_write_channel() -> None:
    export = _export()
    export["knowledge"][0]["source_type"] = "claude"

    graph = freezer.convert_export(export, _sidecar(export), _source_sha(export))

    knowledge = next(node for node in graph["nodes"] if node["short_id"] == "KNOW-2")
    assert knowledge["source_type"] == "claude"
    assert knowledge["created_by_source"] == "agent"


def test_attaches_orphaned_knowledge_to_the_single_root_component() -> None:
    export = _export()
    export["knowledge"][0]["parent_short_id"] = None

    graph = freezer.convert_export(export, _sidecar(export), _source_sha(export))

    knowledge = next(node for node in graph["nodes"] if node["short_id"] == "KNOW-2")
    assert knowledge["parent"] == "COMP-1"


def test_reconstructs_the_exact_production_embedding_input() -> None:
    node = {"title": "\ufeff  Title ", "body": " Body \u3000"}
    assert freezer._embedding_input(node) == "Title \n\n Body"
    node["body"] = "x" * 30_000
    assert freezer._embedding_input(node) == ("Title \n\n" + "x" * 30_000)[:28_000]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update({"surprise": True}), "unexpected or missing"),
        (
            lambda value: value["knowledge"].append(copy.deepcopy(value["knowledge"][0])),
            "short IDs must be unique",
        ),
        (
            lambda value: value["knowledge"][0].update(
                {"parent_short_id": "COMP-99"}
            ),
            "missing parent",
        ),
        (
            lambda value: value["edges"][0].update({"dst_short_id": "COMP-99"}),
            "unknown node",
        ),
    ],
)
def test_fails_closed_on_invalid_export(mutation: object, message: str) -> None:
    value = _export()
    mutation(value)
    with pytest.raises(freezer.FreezeError, match=message):
        freezer.convert_export(value, _sidecar(value), _source_sha(value))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update({"surprise": True}), "unexpected or missing"),
        (
            lambda value: value.update({"source_export_sha256": "0" * 64}),
            "source export digest differs",
        ),
        (
            lambda value: value.update({"model": "wrong-model"}),
            "wrong model",
        ),
        (
            lambda value: value.update({"dimensions": 768}),
            "wrong dimensions",
        ),
        (
            lambda value: value["embeddings"].reverse(),
            "exactly cover nodes in canonical order",
        ),
        (
            lambda value: value["embeddings"].pop(),
            "exactly cover nodes in canonical order",
        ),
        (
            lambda value: value["embeddings"][0].update({"input_sha256": "0" * 64}),
            "input digest differs",
        ),
        (
            lambda value: value["embeddings"][0].update({"embedding": [True] * 1536}),
            "finite numbers",
        ),
        (
            lambda value: value["embeddings"][0].update({"embedding": [65_505] * 1536}),
            "binary16 range",
        ),
    ],
)
def test_fails_closed_on_invalid_embedding_sidecar(
    mutation: object, message: str
) -> None:
    sidecar = _sidecar()
    mutation(sidecar)
    with pytest.raises(freezer.FreezeError, match=message):
        freezer.convert_export(_export(), sidecar, _source_sha(_export()))


@pytest.mark.parametrize(
    ("embedding", "message"),
    [
        ([0.125] * 1535, "exactly 1536"),
        ([True] * 1536, "finite numbers"),
        ([float("inf")] * 1536, "finite numbers"),
        ([65_505.0] * 1536, "binary16 range"),
        ([0.1] * 1536, "binary16-quantized"),
    ],
)
def test_provisioner_strictly_rejects_invalid_graph_embeddings(
    embedding: list[object], message: str
) -> None:
    graph = freezer.convert_export(_export(), _sidecar(), _source_sha(_export()))
    graph["nodes"][0]["embedding"] = embedding
    provisioner = freezer._load_provisioner()
    with pytest.raises(provisioner.ProvisionerError, match=message):
        provisioner._validate_graph(graph)


def test_snapshot_embedding_text_normalizes_to_binary16() -> None:
    provisioner = freezer._load_provisioner()
    # pgvector's halfvec text uses a compact decimal representation that is
    # numerically close to, but not necessarily the exact Python float for,
    # the underlying binary16 value.
    values = ["0.09998", *(["0"] * 1535)]
    normalized = provisioner._embedding_vector_from_text(
        "[" + ",".join(values) + "]"
    )
    assert normalized[0] == freezer._embedding_vector(
        [0.09998, *([0.0] * 1535)], "embedding"
    )[0]


def test_generate_then_validate_and_detect_drift(tmp_path: Path) -> None:
    source, sidecar = _write_inputs(tmp_path)
    destination = tmp_path / "fixture"

    digest = freezer.freeze(
        source,
        destination,
        embedding_sidecar_path=sidecar,
        validate=False,
        replace=False,
    )
    manifest = json.loads((destination / "fixture.json").read_text())
    graph_bytes = (destination / "graphs/faithful.json").read_bytes()
    assert graph_bytes == freezer._canonical_bytes(json.loads(graph_bytes))
    assert manifest["fixture_id"] == "dotstate-faithful-20260723-v1"
    assert manifest["cohort_id"] == "dotstate-foundation"
    assert manifest["graphs"]["faithful"]["sha256"] == digest
    assert manifest["graphs"]["faithful"]["graph_format_version"] == "stele-canonical-graph-v2"
    assert manifest["graphs"]["faithful"]["embedding_model_id"] == "openai/text-embedding-3-small"
    assert (destination / "corpora/evidence.json").read_bytes() == b"[]"
    for artifact in (
        destination / "fixture.json",
        destination / "graphs/faithful.json",
        destination / "corpora/evidence.json",
    ):
        assert artifact.stat().st_mode & 0o777 == 0o644
    assert freezer.freeze(
        source,
        destination,
        embedding_sidecar_path=sidecar,
        validate=True,
        replace=False,
    ) == digest

    (destination / "corpora/evidence.json").write_bytes(b"[{}]")
    with pytest.raises(freezer.FreezeError, match="does not match export"):
        freezer.freeze(
            source,
            destination,
            embedding_sidecar_path=sidecar,
            validate=True,
            replace=False,
        )


def test_rejects_symlink_input_and_output_artifact(tmp_path: Path) -> None:
    source, sidecar = _write_inputs(tmp_path)
    linked_source = tmp_path / "linked.json"
    linked_source.symlink_to(source)
    with pytest.raises(freezer.FreezeError, match="not a symlink"):
        freezer.freeze(
            linked_source,
            tmp_path / "fixture",
            embedding_sidecar_path=sidecar,
            validate=False,
            replace=False,
        )

    destination = tmp_path / "fixture-two"
    freezer.freeze(
        source,
        destination,
        embedding_sidecar_path=sidecar,
        validate=False,
        replace=False,
    )
    graph = destination / "graphs/faithful.json"
    graph.unlink()
    graph.symlink_to(source)
    with pytest.raises(freezer.FreezeError, match="unsafe"):
        freezer.freeze(
            source,
            destination,
            embedding_sidecar_path=sidecar,
            validate=False,
            replace=True,
        )


def test_rejects_symlink_output_root(tmp_path: Path) -> None:
    source, sidecar = _write_inputs(tmp_path)
    real_destination = tmp_path / "real-fixture"
    real_destination.mkdir()
    linked_destination = tmp_path / "linked-fixture"
    linked_destination.symlink_to(real_destination, target_is_directory=True)

    with pytest.raises(freezer.FreezeError, match="must not traverse a symlink"):
        freezer.freeze(
            source,
            linked_destination,
            embedding_sidecar_path=sidecar,
            validate=False,
            replace=False,
        )


def test_rejects_symlink_and_duplicate_keys_in_sidecar(tmp_path: Path) -> None:
    source, sidecar = _write_inputs(tmp_path)
    linked_sidecar = tmp_path / "linked-embeddings.json"
    linked_sidecar.symlink_to(sidecar)
    with pytest.raises(freezer.FreezeError, match="not a symlink"):
        freezer.freeze(
            source,
            tmp_path / "fixture",
            embedding_sidecar_path=linked_sidecar,
            validate=False,
            replace=False,
        )

    sidecar.write_text(
        '{"schema_version":1,"schema_version":1,"source_export_sha256":"0",'
        '"model":"openai/text-embedding-3-small","dimensions":1536,"embeddings":[]}',
        encoding="utf-8",
    )
    with pytest.raises(freezer.FreezeError, match="repeats field"):
        freezer.freeze(
            source,
            tmp_path / "fixture-two",
            embedding_sidecar_path=sidecar,
            validate=False,
            replace=False,
        )


def test_rejects_embedding_sidecar_inside_output_root(tmp_path: Path) -> None:
    source, sidecar = _write_inputs(tmp_path)
    destination = tmp_path / "fixture"
    destination.mkdir()
    nested_sidecar = destination / "embeddings.json"
    nested_sidecar.write_bytes(sidecar.read_bytes())

    with pytest.raises(freezer.FreezeError, match="outside the output root"):
        freezer.freeze(
            source,
            destination,
            embedding_sidecar_path=nested_sidecar,
            validate=False,
            replace=True,
        )


def test_rejects_embedding_sidecar_hardlink_alias_of_artifact(tmp_path: Path) -> None:
    source, sidecar = _write_inputs(tmp_path)
    destination = tmp_path / "fixture"
    freezer.freeze(
        source,
        destination,
        embedding_sidecar_path=sidecar,
        validate=False,
        replace=False,
    )
    artifact = destination / "graphs/faithful.json"
    artifact.unlink()
    os.link(sidecar, artifact)

    with pytest.raises(freezer.FreezeError, match="must not alias"):
        freezer.freeze(
            source,
            destination,
            embedding_sidecar_path=sidecar,
            validate=False,
            replace=True,
        )
