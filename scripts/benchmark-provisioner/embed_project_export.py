#!/usr/bin/env python3
"""Generate a deterministic embedding sidecar for a Stele v2 project export."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

MODEL = "openai/text-embedding-3-small"
DIMENSIONS = 1536
MAX_INPUT_CHARS = 28_000
MAX_BATCH = 200
MAX_GATEWAY_BATCHES = 10
SOURCE_EXPORT_LIMIT = 10_000_000
GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/embeddings"
MEDIA_TYPE = "application/json"
NODE_GROUPS = ("components", "knowledge", "tasks", "documents", "diagrams")
PREFIX_RANK = {"COMP": 0, "KNOW": 1, "TASK": 2, "DOC": 3, "DIAG": 4}
GROUP_PREFIX = {
    "components": "COMP",
    "knowledge": "KNOW",
    "tasks": "TASK",
    "documents": "DOC",
    "diagrams": "DIAG",
}
SHORT_ID = re.compile(r"^(COMP|KNOW|TASK|DOC|DIAG)-([1-9][0-9]*)$")

# ECMAScript WhiteSpace + LineTerminator code points used by String.prototype.trim.
JS_TRIM_CHARS = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


class EmbedExportError(RuntimeError):
    """A safe failure whose message never includes credentials or response bodies."""


def canonical_bytes(value: object) -> bytes:
    """Return the repository's canonical UTF-8 JSON representation."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def build_input(title: str | None, body: str | None) -> str:
    """Match production's `${title}\n\n${body}`.trim(), then truncate."""
    text = f"{title or ''}\n\n{body or ''}".strip(JS_TRIM_CHARS)
    return text[:MAX_INPUT_CHARS]


def _short_id_key(value: object) -> tuple[int, int]:
    if not isinstance(value, str):
        raise EmbedExportError("node.short_id must be a canonical short ID")
    match = SHORT_ID.fullmatch(value)
    if match is None:
        raise EmbedExportError("node.short_id must be a canonical short ID")
    return PREFIX_RANK[match.group(1)], int(match.group(2))


def _ordered_nodes(export: object) -> list[dict[str, Any]]:
    if not isinstance(export, dict):
        raise EmbedExportError("export must be an object")
    if export.get("version") != 2:
        raise EmbedExportError("only project export version 2 is supported")

    nodes: list[dict[str, Any]] = []
    for group in NODE_GROUPS:
        values = export.get(group)
        if not isinstance(values, list):
            raise EmbedExportError(f"export.{group} must be an array")
        for value in values:
            if not isinstance(value, dict):
                raise EmbedExportError(f"export.{group} nodes must be objects")
            short_id = value.get("short_id")
            _short_id_key(short_id)
            if not str(short_id).startswith(f"{GROUP_PREFIX[group]}-"):
                raise EmbedExportError(f"export.{group} node has the wrong short ID prefix")
            title = value.get("title")
            body = value.get("body")
            if title is not None and not isinstance(title, str):
                raise EmbedExportError("node.title must be a string or null")
            if body is not None and not isinstance(body, str):
                raise EmbedExportError("node.body must be a string or null")
            nodes.append(value)

    nodes.sort(key=lambda node: _short_id_key(node["short_id"]))
    short_ids = [node["short_id"] for node in nodes]
    if len(short_ids) != len(set(short_ids)):
        raise EmbedExportError("node short IDs must be unique")
    return nodes


def _validate_response(value: object, expected_count: int) -> list[list[int | float]]:
    if not isinstance(value, dict) or value.get("object") != "list":
        raise EmbedExportError("embedding response must be an object list")
    response_model = value.get("model")
    if response_model not in (MODEL, MODEL.split("/", 1)[1]):
        raise EmbedExportError("embedding response used an unexpected model")
    data = value.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise EmbedExportError("embedding response count does not match the request")

    ordered: list[list[int | float] | None] = [None] * expected_count
    for item in data:
        if not isinstance(item, dict) or item.get("object") != "embedding":
            raise EmbedExportError("embedding response contains an invalid item")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise EmbedExportError("embedding response contains an invalid index")
        if index < 0 or index >= expected_count or ordered[index] is not None:
            raise EmbedExportError("embedding response indices are not a permutation")
        vector = item.get("embedding")
        if not isinstance(vector, list) or len(vector) != DIMENSIONS:
            raise EmbedExportError("embedding response has the wrong dimensions")
        for number in vector:
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise EmbedExportError("embedding response contains a non-numeric value")
            if not math.isfinite(number):
                raise EmbedExportError("embedding response contains a non-finite value")
        ordered[index] = vector

    if any(vector is None for vector in ordered):
        raise EmbedExportError("embedding response indices are incomplete")
    return [vector for vector in ordered if vector is not None]


def post_batch(inputs: list[str], api_key: str) -> object:
    """Call the Gateway once without ever exposing the supplied key."""
    request = urllib.request.Request(
        GATEWAY_URL,
        data=canonical_bytes({"dimensions": DIMENSIONS, "input": inputs, "model": MODEL}),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": MEDIA_TYPE,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 200:
                raise EmbedExportError(f"embedding gateway returned HTTP {response.status}")
            payload = response.read()
    except urllib.error.HTTPError as error:
        raise EmbedExportError(f"embedding gateway returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError):
        raise EmbedExportError("embedding gateway request failed") from None
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EmbedExportError("embedding gateway returned invalid JSON") from None


BatchRequester = Callable[[list[str], str], object]


def _load_freezer() -> ModuleType:
    path = Path(__file__).with_name("freeze_project_export.py")
    spec = importlib.util.spec_from_file_location("_benchmark_export_freezer", path)
    if spec is None or spec.loader is None:
        raise EmbedExportError("could not load the source export validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_source_structure(export: object) -> None:
    freezer = _load_freezer()
    try:
        freezer.validate_export_structure(export)
    except freezer.FreezeError as error:
        raise EmbedExportError(f"source export is invalid: {error}") from None


def _batch_count(node_count: int) -> int:
    count = (node_count + MAX_BATCH - 1) // MAX_BATCH
    if count > MAX_GATEWAY_BATCHES:
        raise EmbedExportError(
            f"embedding plan exceeds the {MAX_GATEWAY_BATCHES}-request limit"
        )
    return count


def _validate_output_path(source: Path, output: Path, *, replace: bool) -> None:
    current = output
    while current != current.parent:
        if current.is_symlink():
            raise EmbedExportError("output path must not traverse a symlink")
        current = current.parent
    if not output.parent.is_dir():
        raise EmbedExportError("output parent directory must already exist")
    if output.exists():
        try:
            metadata = output.lstat()
        except OSError as error:
            raise EmbedExportError("output is unavailable") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise EmbedExportError("output must be a regular file")
        try:
            aliases_source = os.path.samefile(source, output)
        except OSError as error:
            raise EmbedExportError("could not validate source and output paths") from error
        if aliases_source:
            raise EmbedExportError("source and output must be different files")
        if not replace:
            raise EmbedExportError("output already exists; pass --replace to overwrite it")
    elif source.resolve(strict=True) == output.resolve(strict=False):
        raise EmbedExportError("source and output must be different files")


def _read_source(source: Path) -> tuple[object, bytes]:
    try:
        metadata = source.lstat()
    except OSError as error:
        raise EmbedExportError("source export is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise EmbedExportError("source must be a regular file and not a symlink")
    if metadata.st_size > SOURCE_EXPORT_LIMIT:
        raise EmbedExportError("source export exceeds the size limit")
    try:
        source_bytes = source.read_bytes()
    except OSError as error:
        raise EmbedExportError("source export is unavailable") from error
    if len(source_bytes) > SOURCE_EXPORT_LIMIT:
        raise EmbedExportError("source export exceeds the size limit")
    try:
        export = json.loads(source_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EmbedExportError("source export must be valid UTF-8 JSON") from None
    return export, source_bytes


def plan_file(source: Path, output: Path, *, replace: bool = False) -> dict[str, object]:
    """Validate local inputs and report the paid work without making a request."""
    export, source_bytes = _read_source(source)
    _validate_output_path(source, output, replace=replace)
    _validate_source_structure(export)
    node_count = len(_ordered_nodes(export))
    return {
        "batch_count": _batch_count(node_count),
        "node_count": node_count,
        "source_export_sha256": hashlib.sha256(source_bytes).hexdigest(),
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def generate_sidecar(
    export: object,
    source_export_sha256: str,
    api_key: str,
    *,
    requester: BatchRequester | None = None,
) -> dict[str, object]:
    """Embed every exported node, preserving canonical short-ID order."""
    if not api_key:
        raise EmbedExportError("AI_GATEWAY_KEY_EVAL is required")
    if not re.fullmatch(r"[0-9a-f]{64}", source_export_sha256):
        raise EmbedExportError("source export digest must be lowercase SHA-256")
    _validate_source_structure(export)

    requester = requester or post_batch
    nodes = _ordered_nodes(export)
    _batch_count(len(nodes))
    entries: list[dict[str, object]] = []
    for offset in range(0, len(nodes), MAX_BATCH):
        batch = nodes[offset : offset + MAX_BATCH]
        inputs = [build_input(node.get("title"), node.get("body")) for node in batch]
        vectors = _validate_response(requester(inputs, api_key), len(inputs))
        for node, input_text, vector in zip(batch, inputs, vectors, strict=True):
            entries.append(
                {
                    "embedding": vector,
                    "input_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
                    "short_id": node["short_id"],
                }
            )

    return {
        "dimensions": DIMENSIONS,
        "embeddings": entries,
        "model": MODEL,
        "schema_version": 1,
        "source_export_sha256": source_export_sha256,
    }


def embed_file(source: Path, output: Path, *, replace: bool = False) -> str:
    """Read one export and write only the explicitly named output path."""
    export, source_bytes = _read_source(source)
    _validate_output_path(source, output, replace=replace)

    api_key = os.environ.get("AI_GATEWAY_KEY_EVAL", "")
    sidecar = generate_sidecar(export, hashlib.sha256(source_bytes).hexdigest(), api_key)
    output_bytes = canonical_bytes(sidecar)

    _atomic_write(output, output_bytes)
    return hashlib.sha256(output_bytes).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Stele v2 project export JSON")
    parser.add_argument("output", type=Path, help="explicit sidecar JSON output path")
    parser.add_argument(
        "--replace", action="store_true", help="replace an existing regular output file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate paths and print planned request counts without using the gateway",
    )
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            print(
                json.dumps(
                    plan_file(args.source, args.output, replace=args.replace),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        digest = embed_file(args.source, args.output, replace=args.replace)
    except EmbedExportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
