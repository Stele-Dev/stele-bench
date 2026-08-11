#!/usr/bin/env python3
"""Freeze a Stele project export as a deterministic benchmark fixture."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import struct
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

EXPORT_LIMIT = 10_000_000
EMBEDDING_SIDECAR_LIMIT = 100_000_000
EMBEDDING_DIMENSIONS = 1536
FIXTURE_ID = "dotstate-faithful-20260723-v1"
COHORT_ID = "dotstate-foundation"
GRAPH_FORMAT_VERSION = "stele-canonical-graph-v2"
BACKEND_COMPATIBILITY_ID = "stele-local-schema-2026-07"
EMBEDDING_MODEL_ID = "openai/text-embedding-3-small"
MEDIA_TYPE = "application/json"

EXPORT_KEYS = {
    "version",
    "exported_at",
    "project",
    "components",
    "knowledge",
    "tasks",
    "documents",
    "diagrams",
    "edges",
}
PROJECT_KEYS = {"id", "slug", "name", "description", "repo_url"}
NODE_KEYS = {
    "id",
    "short_id",
    "type",
    "status",
    "parent_short_id",
    "title",
    "body",
    "category",
    "topics",
    "assignee_user_id",
    "kind",
    "priority",
    "due_at",
    "target_date",
    "position",
    "expires_at",
    "created_at",
    "updated_at",
    "source",
    "source_url",
    "source_type",
    "anchor_files",
    "anchor_symbols",
}
EDGE_KEYS = {
    "src_short_id",
    "dst_short_id",
    "relation",
    "created_at",
    "reason",
    "evidence",
}
GROUP_TYPES = {
    "components": "component",
    "knowledge": "knowledge",
    "tasks": "task",
    "documents": "document",
    "diagrams": "diagram",
}
CREATED_BY_SOURCE = {"human": "human", "claude": "agent", "import": "import"}
PREFIX_RANK = {"COMP": 0, "KNOW": 1, "TASK": 2, "DOC": 3, "DIAG": 4}
SHORT_ID = re.compile(r"^(COMP|KNOW|TASK|DOC|DIAG)-([1-9][0-9]*)$")
RELATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SIDECAR_KEYS = {
    "schema_version",
    "source_export_sha256",
    "model",
    "dimensions",
    "embeddings",
}
SIDECAR_ENTRY_KEYS = {"short_id", "input_sha256", "embedding"}
JS_TRIM_CHARS = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


class FreezeError(RuntimeError):
    """A safe, user-facing fixture construction failure."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _require_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FreezeError(f"{name} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise FreezeError(f"{name} has unexpected or missing fields")


def _require_string(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise FreezeError(f"{name} must be a string")
    return value


def _timestamp(value: object, name: str) -> str:
    raw = _require_string(value, name)
    assert raw is not None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise FreezeError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise FreezeError(f"{name} must use UTC")
    return parsed.astimezone(UTC).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _short_id_key(value: object, name: str = "short_id") -> tuple[int, int]:
    if not isinstance(value, str):
        raise FreezeError(f"{name} must be a canonical short ID")
    match = SHORT_ID.fullmatch(value)
    if match is None:
        raise FreezeError(f"{name} must be a canonical short ID")
    return PREFIX_RANK[match.group(1)], int(match.group(2))


def _string_array(value: object, name: str, *, nullable: bool = False) -> list[str]:
    if value is None and nullable:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise FreezeError(f"{name} must be an array of strings")
    return list(value)


def _embedding_input(node: dict[str, object]) -> str:
    """Reconstruct the production node text embedded by the runtime."""
    return f"{node['title']}\n\n{node['body']}".strip(JS_TRIM_CHARS)[:28_000]


def _embedding_vector(value: object, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != EMBEDDING_DIMENSIONS:
        raise FreezeError(f"{name} must contain exactly 1536 numbers")
    quantized: list[float] = []
    for item in value:
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
        ):
            raise FreezeError(f"{name} must contain only finite numbers")
        if abs(item) > 65_504:
            raise FreezeError(f"{name} contains a number outside the binary16 range")
        quantized.append(struct.unpack(">e", struct.pack(">e", float(item)))[0])
    return quantized


def _validate_sidecar(
    value: object,
    nodes: list[dict[str, object]],
    source_export_sha256: str,
) -> dict[str, list[float]]:
    sidecar = _require_object(value, "embedding sidecar")
    _require_exact_keys(sidecar, SIDECAR_KEYS, "embedding sidecar")
    if sidecar["schema_version"] != 1:
        raise FreezeError("unsupported embedding sidecar schema version")
    if sidecar["source_export_sha256"] != source_export_sha256:
        raise FreezeError("embedding sidecar source export digest differs")
    if sidecar["model"] != EMBEDDING_MODEL_ID:
        raise FreezeError("embedding sidecar uses the wrong model")
    if sidecar["dimensions"] != EMBEDDING_DIMENSIONS:
        raise FreezeError("embedding sidecar uses the wrong dimensions")
    entries = sidecar["embeddings"]
    if not isinstance(entries, list):
        raise FreezeError("embedding sidecar entries must be an array")

    expected_ids = [str(node["short_id"]) for node in nodes]
    actual_ids: list[str] = []
    vectors: dict[str, list[float]] = {}
    nodes_by_id = {str(node["short_id"]): node for node in nodes}
    for index, value in enumerate(entries):
        entry = _require_object(value, f"embedding sidecar entry {index + 1}")
        _require_exact_keys(
            entry, SIDECAR_ENTRY_KEYS, f"embedding sidecar entry {index + 1}"
        )
        short_id = _require_string(entry["short_id"], "embedding entry.short_id")
        assert short_id is not None
        _short_id_key(short_id, "embedding entry.short_id")
        input_sha256 = _require_string(
            entry["input_sha256"], "embedding entry.input_sha256"
        )
        assert input_sha256 is not None
        if SHA256.fullmatch(input_sha256) is None:
            raise FreezeError("embedding entry.input_sha256 must be lowercase SHA-256")
        node = nodes_by_id.get(short_id)
        if node is None:
            raise FreezeError("embedding sidecar references an unknown node")
        expected_sha256 = hashlib.sha256(
            _embedding_input(node).encode("utf-8")
        ).hexdigest()
        if input_sha256 != expected_sha256:
            raise FreezeError(f"embedding input digest differs for {short_id}")
        actual_ids.append(short_id)
        vectors[short_id] = _embedding_vector(
            entry["embedding"], f"embedding for {short_id}"
        )
    if actual_ids != expected_ids:
        raise FreezeError(
            "embedding sidecar entries must exactly cover nodes in canonical order"
        )
    return vectors


def _convert_node(value: object, group: str) -> dict[str, object]:
    node = _require_object(value, f"{group} node")
    _require_exact_keys(node, NODE_KEYS, f"{group} node")
    expected_type = GROUP_TYPES[group]
    if node["type"] != expected_type:
        raise FreezeError(f"{group} node has the wrong type")
    short_id = _require_string(node["short_id"], "node.short_id")
    assert short_id is not None
    prefix, _ = short_id.split("-", 1)
    if _short_id_key(short_id)[0] != PREFIX_RANK[prefix]:
        raise FreezeError("node short ID is invalid")
    expected_prefix = {
        "component": "COMP",
        "knowledge": "KNOW",
        "task": "TASK",
        "document": "DOC",
        "diagram": "DIAG",
    }[expected_type]
    if prefix != expected_prefix:
        raise FreezeError("node short ID prefix does not match its type")

    created_at = _timestamp(node["created_at"], "node.created_at")
    updated_at = _timestamp(node["updated_at"], "node.updated_at")
    title = _require_string(node["title"], "node.title")
    body = _require_string(node["body"], "node.body")
    status = _require_string(node["status"], "node.status")
    kind = _require_string(node["kind"], "node.kind")
    source_type = _require_string(node["source_type"], "node.source_type")
    category = _require_string(node["category"], "node.category", nullable=True)
    priority = _require_string(node["priority"], "node.priority", nullable=True)
    parent = _require_string(node["parent_short_id"], "node.parent_short_id", nullable=True)
    if parent is not None:
        _short_id_key(parent, "node.parent_short_id")
    assert title is not None and body is not None and status is not None
    assert kind is not None and source_type is not None
    created_by_source = CREATED_BY_SOURCE.get(source_type)
    if created_by_source is None:
        raise FreezeError("node.source_type is not portable")

    converted: dict[str, object] = {
        "short_id": short_id,
        "parent": parent,
        "workspace": None,
        "type": expected_type,
        "status": status,
        "title": title,
        "body": body,
        "category": category,
        "topics": _string_array(node["topics"], "node.topics"),
        "created_by_source": created_by_source,
        "created_at": created_at,
        "updated_at": updated_at,
        "content_updated_at": updated_at,
        "embedding_updated_at": updated_at,
        "completed_at": None,
        "priority": priority,
        "kind": kind,
        "source_type": source_type,
        "anchor_files": _string_array(
            node["anchor_files"], "node.anchor_files", nullable=True
        ),
        "anchor_symbols": _string_array(
            node["anchor_symbols"], "node.anchor_symbols", nullable=True
        ),
        "hidden": False,
    }
    return converted


def validate_export_structure(value: object) -> dict[str, object]:
    """Validate and convert the source-only portion of a strict v2 trunk export."""
    export = _require_object(value, "export")
    _require_exact_keys(export, EXPORT_KEYS, "export")
    if export["version"] != 2:
        raise FreezeError("only project export version 2 is supported")
    exported_at = _timestamp(export["exported_at"], "export.exported_at")
    project = _require_object(export["project"], "project")
    _require_exact_keys(project, PROJECT_KEYS, "project")

    nodes: list[dict[str, object]] = []
    for group in GROUP_TYPES:
        values = export[group]
        if not isinstance(values, list):
            raise FreezeError(f"export.{group} must be an array")
        nodes.extend(_convert_node(value, group) for value in values)
    nodes.sort(key=lambda node: _short_id_key(node["short_id"]))

    short_ids = [str(node["short_id"]) for node in nodes]
    if len(short_ids) != len(set(short_ids)):
        raise FreezeError("node short IDs must be unique")
    orphaned = [
        node
        for node in nodes
        if node["type"] in {"knowledge", "task"} and node["parent"] is None
    ]
    if orphaned:
        roots = [
            node
            for node in nodes
            if node["type"] == "component" and node["parent"] is None
        ]
        if len(roots) != 1:
            raise FreezeError(
                "orphaned knowledge and tasks require exactly one root component"
            )
        root_id = roots[0]["short_id"]
        for node in orphaned:
            node["parent"] = root_id
    known = set(short_ids)
    seen: set[str] = set()
    for node in nodes:
        parent = node["parent"]
        if parent is not None and parent not in known:
            raise FreezeError("node references a missing parent")
        if parent is not None and parent not in seen:
            raise FreezeError("parent nodes must sort before their children")
        seen.add(str(node["short_id"]))

    raw_edges = export["edges"]
    if not isinstance(raw_edges, list):
        raise FreezeError("export.edges must be an array")
    edges: list[dict[str, object]] = []
    edge_keys: set[tuple[str, str, str]] = set()
    for value in raw_edges:
        edge = _require_object(value, "edge")
        _require_exact_keys(edge, EDGE_KEYS, "edge")
        src = _require_string(edge["src_short_id"], "edge.src_short_id")
        dst = _require_string(edge["dst_short_id"], "edge.dst_short_id")
        relation = _require_string(edge["relation"], "edge.relation")
        assert src is not None and dst is not None and relation is not None
        _short_id_key(src, "edge.src_short_id")
        _short_id_key(dst, "edge.dst_short_id")
        if src not in known or dst not in known:
            raise FreezeError("edge references an unknown node")
        if RELATION.fullmatch(relation) is None:
            raise FreezeError("edge relation is not portable")
        identity = (src, dst, relation)
        if identity in edge_keys:
            raise FreezeError("edges must be unique by source, destination, and relation")
        edge_keys.add(identity)
        edges.append(
            {
                "src": src,
                "dst": dst,
                "relation": relation,
                "created_at": _timestamp(edge["created_at"], "edge.created_at"),
                "reason": _require_string(edge["reason"], "edge.reason", nullable=True),
                "evidence": _require_string(
                    edge["evidence"], "edge.evidence", nullable=True
                ),
            }
        )
    edges.sort(
        key=lambda edge: (
            _short_id_key(edge["src"]),
            _short_id_key(edge["dst"]),
            edge["relation"],
        )
    )

    created_at = min(
        [exported_at] + [str(node["created_at"]) for node in nodes]
    )
    name = _require_string(project["name"], "project.name")
    if name is None or not name.strip():
        raise FreezeError("project.name must not be empty")
    result: dict[str, object] = {
        "schema_version": 1,
        "project": {
            "name": name,
            "description": _require_string(
                project["description"], "project.description", nullable=True
            ),
            "repo_url": _require_string(
                project["repo_url"], "project.repo_url", nullable=True
            ),
            "created_at": created_at,
            "settings": {},
            "default_branch": "main",
            "visibility": "private",
            "website": None,
        },
        "workspaces": [],
        "branches": [],
        "nodes": nodes,
        "edges": edges,
        "comments": [],
    }
    validation_graph = {
        **result,
        "nodes": [
            {**node, "embedding": [0.0] * EMBEDDING_DIMENSIONS} for node in nodes
        ],
    }
    _validate_with_provisioner(validation_graph)
    return result


def convert_export(
    value: object, embedding_sidecar: object, source_export_sha256: str
) -> dict[str, object]:
    """Convert one strict v2 trunk export to the canonical graph v2 schema."""
    result = validate_export_structure(value)
    nodes = result["nodes"]
    assert isinstance(nodes, list)
    embeddings = _validate_sidecar(embedding_sidecar, nodes, source_export_sha256)
    for node in nodes:
        node["embedding"] = embeddings[str(node["short_id"])]
    _validate_with_provisioner(result)
    return result


def _load_provisioner() -> ModuleType:
    path = Path(__file__).with_name("provision.py")
    spec = importlib.util.spec_from_file_location("_fixture_provisioner", path)
    if spec is None or spec.loader is None:
        raise FreezeError("could not load the local provisioner validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_with_provisioner(graph: object) -> None:
    module = _load_provisioner()
    try:
        module._validate_graph(graph)
    except Exception as error:
        raise FreezeError(f"generated graph is not provisioner-compatible: {error}") from error


def _read_export(path: Path) -> tuple[object, str]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise FreezeError("input export is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise FreezeError("input export must be a regular file, not a symlink")
    if metadata.st_size > EXPORT_LIMIT:
        raise FreezeError("input export exceeds the size limit")
    try:
        raw = path.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FreezeError("input export is not valid JSON") from error


def _read_embedding_sidecar(path: Path) -> object:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise FreezeError("embedding sidecar is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise FreezeError("embedding sidecar must be a regular file, not a symlink")
    if metadata.st_size > EMBEDDING_SIDECAR_LIMIT:
        raise FreezeError("embedding sidecar exceeds the size limit")

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise FreezeError(f"embedding sidecar repeats field: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise FreezeError(f"embedding sidecar contains non-finite number: {value}")

    try:
        return json.loads(
            path.read_bytes(),
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except FreezeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FreezeError("embedding sidecar is not valid JSON") from error


def _safe_root(path: Path, input_path: Path, embedding_sidecar_path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    if absolute == Path(absolute.anchor):
        raise FreezeError("output root is too broad")
    resolved_root = absolute.resolve(strict=False)
    resolved_input = input_path.resolve(strict=True)
    resolved_sidecar = embedding_sidecar_path.resolve(strict=True)
    if resolved_input == resolved_root or resolved_input.is_relative_to(resolved_root):
        raise FreezeError("input export must be outside the output root")
    if resolved_sidecar == resolved_root or resolved_sidecar.is_relative_to(resolved_root):
        raise FreezeError("embedding sidecar must be outside the output root")
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise FreezeError("output path must not traverse a symlink")
    for relative in ("fixture.json", "graphs/faithful.json", "corpora/evidence.json"):
        artifact = absolute / relative
        if artifact.exists() or artifact.is_symlink():
            try:
                if os.path.samefile(embedding_sidecar_path, artifact):
                    raise FreezeError("embedding sidecar must not alias an output artifact")
            except OSError as error:
                raise FreezeError("could not validate embedding sidecar path") from error
    return absolute


def _write_artifact(path: Path, payload: bytes, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise FreezeError(f"output artifact is unsafe: {path.name}")
        if path.read_bytes() == payload:
            path.chmod(0o644)
            return
        if not replace:
            raise FreezeError(f"output artifact differs: {path.name}; pass --replace")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _artifacts(graph: dict[str, object]) -> dict[str, bytes]:
    graph_bytes = _canonical_bytes(graph)
    evidence_bytes = b"[]"
    manifest = {
        "schema_version": 1,
        "fixture_id": FIXTURE_ID,
        "cohort_id": COHORT_ID,
        "evidence_corpus": {
            "path": "corpora/evidence.json",
            "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            "media_type": MEDIA_TYPE,
            "ordering": "chronological ascending; no truncation",
        },
        "graphs": {
            "faithful": {
                "path": "graphs/faithful.json",
                "sha256": hashlib.sha256(graph_bytes).hexdigest(),
                "media_type": MEDIA_TYPE,
                "graph_format_version": GRAPH_FORMAT_VERSION,
                "backend_compatibility_id": BACKEND_COMPATIBILITY_ID,
                "embedding_model_id": EMBEDDING_MODEL_ID,
                "embedding_dimensions": EMBEDDING_DIMENSIONS,
            }
        },
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return {
        "graphs/faithful.json": graph_bytes,
        "corpora/evidence.json": evidence_bytes,
        "fixture.json": manifest_bytes,
    }


def freeze(
    input_path: Path,
    output_root: Path,
    *,
    embedding_sidecar_path: Path,
    validate: bool,
    replace: bool,
) -> str:
    try:
        root = _safe_root(output_root, input_path, embedding_sidecar_path)
    except OSError as error:
        raise FreezeError("input or embedding sidecar is unavailable") from error
    export, source_export_sha256 = _read_export(input_path)
    graph = convert_export(
        export,
        _read_embedding_sidecar(embedding_sidecar_path),
        source_export_sha256,
    )
    artifacts = _artifacts(graph)
    if validate:
        if replace:
            raise FreezeError("--replace cannot be used with --validate")
        for relative, expected in artifacts.items():
            target = root / relative
            if target.is_symlink() or not target.is_file():
                raise FreezeError(f"fixture artifact is unavailable or unsafe: {relative}")
            if target.read_bytes() != expected:
                raise FreezeError(f"fixture artifact does not match export: {relative}")
    else:
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise FreezeError("output root must be a directory, not a symlink")
        for relative, payload in artifacts.items():
            _write_artifact(root / relative, payload, replace=replace)
    return hashlib.sha256(artifacts["graphs/faithful.json"]).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Stele project export v2 JSON")
    parser.add_argument("output", type=Path, help="frozen fixture directory")
    parser.add_argument(
        "--embedding-sidecar",
        type=Path,
        required=True,
        help="strict schema-v1 node embedding sidecar",
    )
    parser.add_argument(
        "--validate", action="store_true", help="compare the fixture without writing"
    )
    parser.add_argument(
        "--replace", action="store_true", help="replace differing regular artifacts"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        digest = freeze(
            args.input,
            args.output,
            embedding_sidecar_path=args.embedding_sidecar,
            validate=args.validate,
            replace=args.replace,
        )
    except FreezeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"fixture_id": FIXTURE_ID, "graph_sha256": digest},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
