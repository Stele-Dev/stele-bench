from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
from io import BytesIO
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("embed_project_export.py")
SPEC = importlib.util.spec_from_file_location("embed_project_export", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
embedder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = embedder
SPEC.loader.exec_module(embedder)


def _node(short_id: str, title: str = "Title", body: str = "Body") -> dict[str, object]:
    return {"short_id": short_id, "title": title, "body": body}


def _export(nodes: list[dict[str, object]] | None = None) -> dict[str, object]:
    groups: dict[str, list[dict[str, object]]] = {
        "components": [],
        "knowledge": [],
        "tasks": [],
        "documents": [],
        "diagrams": [],
    }
    for node in nodes or [_node("COMP-1")]:
        group = {
            "COMP": "components",
            "KNOW": "knowledge",
            "TASK": "tasks",
            "DOC": "documents",
            "DIAG": "diagrams",
        }[str(node["short_id"]).split("-", 1)[0]]
        groups[group].append(node)
    return {
        "version": 2,
        **groups,
    }


def _strict_export(node_count: int = 2) -> dict[str, object]:
    base = {
        "id": "source-uuid",
        "status": "active",
        "parent_short_id": None,
        "title": "Input routing",
        "body": "Focused inputs consume key events before global navigation.",
        "category": "architecture",
        "topics": ["input", "tui"],
        "assignee_user_id": None,
        "kind": "task",
        "priority": None,
        "due_at": None,
        "target_date": None,
        "position": None,
        "expires_at": None,
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-01-02T03:05:06Z",
        "source": "config:src/app.rs",
        "source_url": None,
        "source_type": "import",
        "anchor_files": ["src/app.rs"],
        "anchor_symbols": ["sync_input_mode"],
    }
    component = {**base, "short_id": "COMP-1", "type": "component"}
    knowledge = [
        {
            **base,
            "id": f"knowledge-{index}",
            "short_id": f"KNOW-{index}",
            "type": "knowledge",
            "parent_short_id": "COMP-1",
        }
        for index in range(1, node_count)
    ]
    return {
        "version": 2,
        "exported_at": "2026-07-23T09:37:09Z",
        "project": {
            "id": "project-source-uuid",
            "slug": "dotstate",
            "name": "DotState",
            "description": "Dotfile manager",
            "repo_url": "https://example.invalid/dotstate",
        },
        "components": [component],
        "knowledge": knowledge,
        "tasks": [],
        "documents": [],
        "diagrams": [],
        "edges": [
            {
                "src_short_id": "KNOW-1",
                "dst_short_id": "COMP-1",
                "relation": "relates-to",
                "created_at": "2026-01-02T03:06:07Z",
                "reason": None,
                "evidence": None,
            }
        ],
    }


def _response(count: int, *, reverse: bool = False) -> dict[str, object]:
    items = [
        {
            "object": "embedding",
            "index": index,
            "embedding": [index / 10] * embedder.DIMENSIONS,
        }
        for index in range(count)
    ]
    return {
        "object": "list",
        "data": list(reversed(items)) if reverse else items,
        "model": "text-embedding-3-small",
    }


def test_matches_production_trim_and_truncation_exactly() -> None:
    assert embedder.build_input("\ufeff  Title ", " Body \u3000") == "Title \n\n Body"
    value = embedder.build_input("T", "x" * 30_000)
    assert len(value) == 28_000
    assert value == ("T\n\n" + "x" * 30_000)[:28_000]


def test_generates_ordered_content_addressed_sidecar_in_bounded_batches() -> None:
    export = _strict_export(205)
    for node in export["knowledge"]:
        node["title"] = f"T{str(node['short_id']).split('-', 1)[1]}"
    seen: list[tuple[list[str], str]] = []

    def request(inputs: list[str], key: str) -> object:
        seen.append((inputs, key))
        return _response(len(inputs), reverse=True)

    sidecar = embedder.generate_sidecar(export, "a" * 64, "eval-secret", requester=request)

    assert [len(batch) for batch, _ in seen] == [200, 5]
    assert all(key == "eval-secret" for _, key in seen)
    assert sidecar["model"] == "openai/text-embedding-3-small"
    assert sidecar["dimensions"] == 1536
    assert [entry["short_id"] for entry in sidecar["embeddings"]] == [
        "COMP-1",
        *(f"KNOW-{index}" for index in range(1, 205)),
    ]
    first = sidecar["embeddings"][0]
    assert first["input_sha256"] == hashlib.sha256(
        b"Input routing\n\nFocused inputs consume key events before global navigation."
    ).hexdigest()
    assert first["embedding"] == [0.0] * 1536


def test_canonical_output_is_deterministic_and_key_is_never_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_strict_export()), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "never-write-this-key")
    monkeypatch.setattr(
        embedder,
        "post_batch",
        lambda inputs, _key: _response(len(inputs)),
    )

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    digest = embedder.embed_file(source, first)
    embedder.embed_file(source, second)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() == embedder.canonical_bytes(json.loads(first.read_bytes()))
    assert digest == hashlib.sha256(first.read_bytes()).hexdigest()
    assert b"never-write-this-key" not in first.read_bytes()


def test_post_batch_uses_openai_compatible_gateway_contract_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response(BytesIO):
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

    def urlopen(request: object, timeout: int) -> Response:
        captured.update(
            {
                "url": request.full_url,
                "authorization": request.get_header("Authorization"),
                "body": json.loads(request.data),
                "timeout": timeout,
            }
        )
        return Response(json.dumps(_response(1)).encode())

    monkeypatch.setattr(embedder.urllib.request, "urlopen", urlopen)
    result = embedder.post_batch(["Title\n\nBody"], "eval-secret")

    assert result == _response(1)
    assert captured == {
        "url": "https://ai-gateway.vercel.sh/v1/embeddings",
        "authorization": "Bearer eval-secret",
        "body": {
            "dimensions": 1536,
            "input": ["Title\n\nBody"],
            "model": "openai/text-embedding-3-small",
        },
        "timeout": 120,
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda response: response.update({"object": "embedding"}), "object list"),
        (lambda response: response.update({"model": "wrong"}), "unexpected model"),
        (lambda response: response["data"].append(response["data"][0]), "count"),
        (
            lambda response: response["data"][0].update({"embedding": [0.0]}),
            "dimensions",
        ),
        (
            lambda response: response["data"][0]["embedding"].__setitem__(0, float("nan")),
            "non-finite",
        ),
    ],
)
def test_fails_closed_on_invalid_gateway_response(mutate: object, message: str) -> None:
    response = _response(2)
    mutate(response)
    with pytest.raises(embedder.EmbedExportError, match=message):
        embedder.generate_sidecar(
            _strict_export(), "b" * 64, "secret", requester=lambda _inputs, _key: response
        )


def test_output_path_is_validated_before_gateway_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_strict_export()), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "secret")
    monkeypatch.setattr(
        embedder,
        "generate_sidecar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            embedder.EmbedExportError("gateway failed")
        ),
    )
    output = tmp_path / "missing" / "sidecar.json"
    with pytest.raises(embedder.EmbedExportError, match="parent directory"):
        embedder.embed_file(source, output)
    assert not output.exists()
    assert not output.parent.exists()

    output = tmp_path / "sidecar.json"
    with pytest.raises(embedder.EmbedExportError, match="gateway failed"):
        embedder.embed_file(source, output)
    assert not output.exists()


def test_rejects_duplicate_nodes_and_missing_eval_key() -> None:
    duplicate = _strict_export()
    duplicate["knowledge"].append(copy.deepcopy(duplicate["knowledge"][0]))
    with pytest.raises(embedder.EmbedExportError, match="unique"):
        embedder.generate_sidecar(
            duplicate,
            "c" * 64,
            "secret",
            requester=lambda inputs, _key: _response(len(inputs)),
        )
    with pytest.raises(embedder.EmbedExportError, match="AI_GATEWAY_KEY_EVAL"):
        embedder.generate_sidecar(_strict_export(), "c" * 64, "")


def test_generate_sidecar_rejects_malformed_export_without_requesting() -> None:
    malformed = _strict_export()
    malformed["project"].pop("name")
    calls = 0

    def request(_inputs: list[str], _key: str) -> object:
        nonlocal calls
        calls += 1
        return _response(1)

    with pytest.raises(embedder.EmbedExportError, match="unexpected or missing"):
        embedder.generate_sidecar(
            malformed, "c" * 64, "secret", requester=request
        )
    assert calls == 0


def test_rejects_oversized_source_before_gateway_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_strict_export()), encoding="utf-8")
    output = tmp_path / "sidecar.json"
    monkeypatch.setattr(embedder, "SOURCE_EXPORT_LIMIT", source.stat().st_size - 1)
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "secret")
    requested = False

    def request(_inputs: list[str], _key: str) -> object:
        nonlocal requested
        requested = True
        return _response(1)

    monkeypatch.setattr(embedder, "post_batch", request)
    with pytest.raises(embedder.EmbedExportError, match="size limit"):
        embedder.embed_file(source, output)
    assert requested is False
    assert not output.exists()


def test_dry_plan_reports_paid_batch_count_without_gateway_or_key(tmp_path: Path) -> None:
    source = tmp_path / "export.json"
    source.write_text(
        json.dumps(_strict_export(205)),
        encoding="utf-8",
    )
    output = tmp_path / "sidecar.json"

    plan = embedder.plan_file(source, output)

    assert plan == {
        "batch_count": 2,
        "node_count": 205,
        "source_export_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    assert not output.exists()


def test_rejects_source_output_alias_even_when_replace_is_enabled(tmp_path: Path) -> None:
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_strict_export()), encoding="utf-8")
    alias = tmp_path / "hardlink.json"
    os.link(source, alias)

    with pytest.raises(embedder.EmbedExportError, match="different files"):
        embedder.plan_file(source, source, replace=True)
    with pytest.raises(embedder.EmbedExportError, match="different files"):
        embedder.plan_file(source, alias, replace=True)


def test_atomic_write_preserves_existing_output_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_strict_export()), encoding="utf-8")
    output = tmp_path / "sidecar.json"
    output.write_bytes(b"original")
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "secret")
    monkeypatch.setattr(
        embedder, "post_batch", lambda inputs, _key: _response(len(inputs))
    )
    monkeypatch.setattr(
        embedder.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        embedder.embed_file(source, output, replace=True)

    assert output.read_bytes() == b"original"
    assert list(tmp_path.glob(".sidecar.json.*")) == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["project"].pop("name"), "unexpected or missing"),
        (
            lambda value: value["components"][0].update({"created_at": "not-a-date"}),
            "ISO-8601",
        ),
        (
            lambda value: value["knowledge"][0].update({"status": "invented"}),
            # Caught by the explicit status check. It used to be caught only as
            # a side effect of the active-knowledge tally, which stopped being a
            # check once a graph was allowed to legitimately have none.
            "not a recognised status",
        ),
        (
            lambda value: value["edges"].append(
                {
                    "src_short_id": "COMP-1",
                    "dst_short_id": "KNOW-999",
                    "relation": "relates-to",
                    "created_at": "2026-01-02T03:06:07Z",
                    "reason": None,
                    "evidence": None,
                }
            ),
            "unknown node",
        ),
    ],
)
def test_malformed_full_export_makes_zero_gateway_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
    message: str,
) -> None:
    value = copy.deepcopy(_strict_export())
    mutate(value)
    source = tmp_path / "export.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    output = tmp_path / "sidecar.json"
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "secret")
    calls = 0

    def request(_inputs: list[str], _key: str) -> object:
        nonlocal calls
        calls += 1
        return _response(1)

    monkeypatch.setattr(embedder, "post_batch", request)
    with pytest.raises(embedder.EmbedExportError, match=message):
        embedder.embed_file(source, output)
    assert calls == 0
    assert not output.exists()


def test_request_bound_is_checked_before_gateway_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_strict_export()), encoding="utf-8")
    output = tmp_path / "sidecar.json"
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "secret")
    monkeypatch.setattr(embedder, "MAX_GATEWAY_BATCHES", 0)
    calls = 0

    def request(_inputs: list[str], _key: str) -> object:
        nonlocal calls
        calls += 1
        return _response(1)

    monkeypatch.setattr(embedder, "post_batch", request)
    with pytest.raises(embedder.EmbedExportError, match="request limit"):
        embedder.embed_file(source, output)
    assert calls == 0
    assert not output.exists()
