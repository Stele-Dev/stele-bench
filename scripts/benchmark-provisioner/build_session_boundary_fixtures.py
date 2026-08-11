#!/usr/bin/env python3
"""Build the session-boundary-recall frozen graph fixture variants.

Unlike ``freeze_project_export.py`` (which converts a real Stele project
export), this cohort's content is authored directly: it is a small, synthetic
lesson node designed to isolate one mechanism (anchor-triggered recall keyed
on a touched file), not a real project's backfilled history. See
``eval/stele-bench/challenges/session-boundary-recall/challenge.json`` for
the paired challenge and KNOW-2471 for why this content is hand-authored
rather than produced by running backfill.

Three fixture variants share everything (project, component, the lesson
node's title/body/category) and differ only in the lesson node's
``anchor_files``:

- ``correct-anchor``: anchored to the file the challenge's first step
  actually touches (``src/dotfile_candidates.rs``) -- anchor-triggered recall
  should surface it when the follow-up turn arrives.
- ``wrong-anchor``: anchored to a plausible but different file -- a negative
  control proving the mechanism does not leak onto an untouched anchor.
- ``no-anchor``: no file anchor at all -- isolates whatever the ordinary
  FTS/semantic recall path does with only the (deliberately vague) follow-up
  text to go on.

Embeddings have two modes, chosen at build time:

- ``placeholder`` (default, offline): deterministic vectors seeded from the
  node text, NOT semantically meaningful. Schema-valid (binary16-quantized,
  1536-dimensional) so the fixture passes provisioner validation and
  restore/blindness plumbing can be exercised without any credential. This is
  what is checked in, so CI and offline rebuilds are reproducible. The two
  anchor conditions key on the structural ``anchor_files`` match, not
  similarity, so they do not depend on embedding fidelity.
- ``gateway`` (``--gateway-embeddings``, needs ``AI_GATEWAY_KEY_EVAL``): real
  ``openai/text-embedding-3-small`` vectors from the Vercel AI Gateway, over
  the exact ``build_input(title, body)`` text production embeds. Required
  before any live paid arm that depends on semantic retrieval quality --
  notably the ``no-anchor`` (ordinary recall) condition, whose whole point is
  what plain FTS/semantic recall does with only the vague follow-up to go on.
  Regenerate with ``--gateway-embeddings --replace`` and commit the re-frozen
  fixtures right before a paid run (see TASK-2473).

Both modes quantize to binary16 identically, because the provisioner rejects a
non-quantized embedding (``provision.py`` ``_quantized_embedding``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import stat
import struct
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

EMBEDDING_DIMENSIONS = 1536
PLACEHOLDER_MODEL_ID = "placeholder/deterministic-seeded-v1"
GATEWAY_MODEL_ID = "openai/text-embedding-3-small"
GRAPH_FORMAT_VERSION = "stele-canonical-graph-v2"
BACKEND_COMPATIBILITY_ID = "stele-local-schema-2026-07"
MEDIA_TYPE = "application/json"
COHORT_ID = "session-boundary-recall-v1"
CREATED_AT = "2026-07-23T00:00:00Z"

TOUCHED_FILE = "src/dotfile_candidates.rs"
PLAUSIBLE_WRONG_FILE = "src/utils/package_manager.rs"

LESSON_TITLE = "Close out a new dotfile candidate with a CHANGELOG entry"
LESSON_BODY = (
    "Adding a new `DotfileCandidate` under the AI-tools section of "
    "`src/dotfile_candidates.rs` is not the whole change: it must also get a "
    "matching bullet in `CHANGELOG.md`'s `[Unreleased]` section, in this "
    "project's `- **Component**: description` format (Keep a Changelog "
    "style). This step was missed for the two most recent OpenCode entries "
    "and had to be backfilled after release. Treat the CHANGELOG bullet as "
    "a mandatory last step whenever a new dotfile candidate is added, even "
    "when nobody restates it."
)

VARIANTS: dict[str, list[str]] = {
    "correct-anchor": [TOUCHED_FILE],
    "wrong-anchor": [PLAUSIBLE_WRONG_FILE],
    "no-anchor": [],
}


class FixtureBuildError(RuntimeError):
    """A safe, user-facing fixture construction failure."""


# text -> a binary16-quantized, 1536-d vector.
Embedder = Callable[[str], list[float]]


def _quantize(vector: list[float]) -> list[float]:
    """Round every component to binary16, the format the provisioner requires."""

    return [struct.unpack(">e", struct.pack(">e", value))[0] for value in vector]


def _placeholder_embedding(seed_text: str) -> list[float]:
    """A deterministic, schema-valid, but NOT semantically meaningful vector.

    Seeded from a sha256 stream so it is reproducible across runs without
    calling any embedding model. See the module docstring.
    """

    digest_stream = bytearray()
    counter = 0
    while len(digest_stream) < EMBEDDING_DIMENSIONS * 2:
        digest_stream.extend(hashlib.sha256(f"{seed_text}\0{counter}".encode()).digest())
        counter += 1
    raw = [
        (int.from_bytes(digest_stream[index : index + 2], "big") / 65535.0) - 0.5
        for index in range(0, EMBEDDING_DIMENSIONS * 2, 2)
    ]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return _quantize([value / norm for value in raw])


def _load_embedder_module() -> ModuleType:
    path = Path(__file__).with_name("embed_project_export.py")
    spec = importlib.util.spec_from_file_location("_fixture_embedder", path)
    if spec is None or spec.loader is None:
        raise FixtureBuildError("could not load the embedding gateway helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _node_input(title: str, body: str) -> str:
    """The exact text production embeds: ``build_input(title, body)``."""

    return _load_embedder_module().build_input(title, body)


def gateway_embedder(api_key: str, *, requester: Callable[..., object] | None = None) -> Embedder:
    """A real ``text-embedding-3-small`` embedder, memoized per unique text.

    The three variants share identical node text (only ``anchor_files``
    differ), so memoization collapses the whole cohort to one Gateway call per
    distinct node -- two calls, not six. ``requester`` is the test seam; it
    defaults to the module's real ``post_batch``.
    """

    if not api_key:
        raise FixtureBuildError("AI_GATEWAY_KEY_EVAL is required for --gateway-embeddings")
    module = _load_embedder_module()
    request = requester or module.post_batch
    cache: dict[str, list[float]] = {}

    def embed(text: str) -> list[float]:
        if text not in cache:
            try:
                vectors = module._validate_response(request([text], api_key), 1)
            except module.EmbedExportError as error:
                raise FixtureBuildError(f"embedding gateway call failed: {error}") from None
            cache[text] = _quantize([float(value) for value in vectors[0]])
        return cache[text]

    return embed


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


COMPONENT_TITLE = "Dotfile Candidates"
COMPONENT_BODY = "The default dotfile candidate scanner (src/dotfile_candidates.rs)."


def _lesson_node(anchor_files: list[str], embed: Embedder) -> dict[str, Any]:
    return {
        "short_id": "KNOW-1",
        "parent": "COMP-1",
        "workspace": None,
        "type": "knowledge",
        "status": "active",
        "title": LESSON_TITLE,
        "body": LESSON_BODY,
        "category": "lesson",
        "topics": ["dotfiles", "changelog"],
        "created_by_source": "human",
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "content_updated_at": CREATED_AT,
        "embedding_updated_at": CREATED_AT,
        "embedding": embed(_node_input(LESSON_TITLE, LESSON_BODY)),
        "completed_at": None,
        "priority": None,
        "kind": "task",
        "source_type": "human",
        "anchor_files": anchor_files,
        "anchor_symbols": [],
        "hidden": False,
    }


def _component_node(embed: Embedder) -> dict[str, Any]:
    return {
        "short_id": "COMP-1",
        "parent": None,
        "workspace": None,
        "type": "component",
        "status": "active",
        "title": COMPONENT_TITLE,
        "body": COMPONENT_BODY,
        "category": None,
        "topics": [],
        "created_by_source": "human",
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "content_updated_at": CREATED_AT,
        "embedding_updated_at": CREATED_AT,
        "embedding": embed(_node_input(COMPONENT_TITLE, COMPONENT_BODY)),
        "completed_at": None,
        "priority": None,
        "kind": "task",
        "source_type": "human",
        "anchor_files": [],
        "anchor_symbols": [],
        "hidden": False,
    }


def build_graph(anchor_files: list[str], embed: Embedder) -> dict[str, Any]:
    return {
        "schema_version": 1,
        # The project identity is what a restored graph shows the agent first,
        # so it must read like the real project it stands in for. Naming it a
        # "fixture" here told the agent it was inside an experiment; that is a
        # certification failure (see stele_bench.certification).
        "project": {
            "name": "DotState",
            "description": "Terminal-based dotfile manager built with Rust (ratatui + git2).",
            "repo_url": "https://github.com/serkanyersen/dotstate",
            "created_at": CREATED_AT,
            "settings": {},
            "default_branch": "main",
            "visibility": "private",
            "website": None,
        },
        "workspaces": [],
        "branches": [],
        "nodes": [_component_node(embed), _lesson_node(anchor_files, embed)],
        "edges": [
            {
                "src": "KNOW-1",
                "dst": "COMP-1",
                "relation": "relates-to",
                "created_at": CREATED_AT,
                "reason": None,
                "evidence": None,
            }
        ],
        "comments": [],
    }


def _load_provisioner() -> ModuleType:
    path = Path(__file__).with_name("provision.py")
    spec = importlib.util.spec_from_file_location("_fixture_provisioner", path)
    if spec is None or spec.loader is None:
        raise FixtureBuildError("could not load the local provisioner validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_with_provisioner(graph: dict[str, Any]) -> None:
    module = _load_provisioner()
    try:
        module._validate_graph(graph)
    except Exception as error:
        raise FixtureBuildError(
            f"generated graph is not provisioner-compatible: {error}"
        ) from error


def _artifacts(variant: str, graph: dict[str, Any], model_id: str) -> dict[str, bytes]:
    graph_bytes = _canonical_bytes(graph)
    evidence_bytes = b"[]"
    fixture_id = f"session-boundary-recall-{variant}-v1"
    manifest = {
        "schema_version": 1,
        "fixture_id": fixture_id,
        "cohort_id": COHORT_ID,
        # The lesson title is what the injection channels surface verbatim, so it
        # is the marker the runner scans the session transcript for post-run.
        "memory_marker": LESSON_TITLE,
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
                "embedding_model_id": model_id,
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


def _write_artifact(path: Path, payload: bytes, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise FixtureBuildError(f"output artifact is unsafe: {path.name}")
        if path.read_bytes() == payload:
            path.chmod(0o644)
            return
        if not replace:
            raise FixtureBuildError(f"output artifact differs: {path.name}; pass --replace")
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


def build(
    output_root: Path,
    *,
    replace: bool = False,
    embed: Embedder | None = None,
    model_id: str = PLACEHOLDER_MODEL_ID,
) -> dict[str, str]:
    """Build all three variants. ``embed`` defaults to the offline placeholder.

    The three variants share identical node text, so ``build_graph`` is called
    with the same ``embed`` each time and a memoized gateway embedder makes at
    most one real call per distinct node across the whole cohort.
    """

    resolved = embed or _placeholder_embedding
    digests: dict[str, str] = {}
    for variant, anchor_files in VARIANTS.items():
        graph = build_graph(anchor_files, resolved)
        _validate_with_provisioner(graph)
        artifacts = _artifacts(variant, graph, model_id)
        variant_root = output_root / variant
        for relative, payload in artifacts.items():
            _write_artifact(variant_root / relative, payload, replace=replace)
        digests[variant] = hashlib.sha256(artifacts["graphs/faithful.json"]).hexdigest()
    return digests


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="fixture cohort root directory")
    parser.add_argument(
        "--replace", action="store_true", help="replace differing regular artifacts"
    )
    parser.add_argument(
        "--gateway-embeddings",
        action="store_true",
        help=(
            "generate real openai/text-embedding-3-small vectors via the Vercel AI "
            "Gateway (requires AI_GATEWAY_KEY_EVAL) instead of offline placeholders"
        ),
    )
    args = parser.parse_args(argv)
    if args.gateway_embeddings:
        embed: Embedder | None = gateway_embedder(os.environ.get("AI_GATEWAY_KEY_EVAL", ""))
        model_id = GATEWAY_MODEL_ID
    else:
        embed, model_id = None, PLACEHOLDER_MODEL_ID
    try:
        digests = build(args.output, replace=args.replace, embed=embed, model_id=model_id)
    except FixtureBuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"model_id": model_id, "graph_sha256": digests}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
