#!/usr/bin/env python3
"""Build the dotfile-drift-detection frozen graph fixture (the seeded decision).

Pairs with ``eval/stele-bench/challenges/dotfile-drift-detection/``. Where the
session-boundary cohort tested anchor-triggered recall of a *convention*, this
one tests the case KNOW-2484 identifies as the purest memory probe: **a decision
NOT to build something**. The correct answer to "can we do this in the
background?" is that this project runs no resident process, settled after a
named incident -- and a decision not to act leaves NO trace in the source. There
is no daemon in the repo and no ``thread::spawn`` under ``src/services/``, and
nothing distinguishes "we ruled this out" from "nobody got to it yet." An
unaided agent cannot derive it at any token budget; it guesses "sure, spawn a
worker," which is wrong.

Only ONE fixture is produced (no anchor variants): the comparison this cohort
exists for is memory vs no memory (``run_pair``), not anchor specificity. The
decision IS anchored to the service a drift feature would touch, which is both
realistic and gives recall two independent paths to fire (the file-read anchor
nudge in turn 1, and semantic recall on turn 2's "background").

Embedding modes are inherited from ``build_session_boundary_fixtures``:
placeholder (offline, default, checked in) or ``--gateway-embeddings`` (real
``openai/text-embedding-3-small``). Use the gateway mode before any paid run --
turn 2 is deliberately vague, so semantic retrieval quality is load-bearing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

COHORT_ID = "dotfile-drift-v1"
FIXTURE_ID = "dotfile-drift-decision-v1"
CREATED_AT = "2026-07-24T00:00:00Z"

# The service a drift/status feature would actually touch, so the anchor is
# realistic rather than contrived.
ANCHORED_FILE = "src/services/sync_service.rs"

COMPONENT_TITLE = "Sync & Status"
COMPONENT_BODY = (
    "Comparing tracked dotfiles against the versions committed to the repo and "
    "reporting their status (src/services/sync_service.rs)."
)

# The memory under test. Both phrases the paired verifier greps for --
# "watchdog-2" and the 8-12% idle figure -- live ONLY here; they are declared as
# the challenge's solution_markers so certification proves they are absent from
# the packaged corpus.
DECISION_TITLE = "DotState runs no resident process"
DECISION_BODY = (
    "We do not ship a daemon, a file watcher, or any other resident process "
    "that keeps running once the user has closed the TUI. This was settled "
    "after the watchdog-2 spike: the resident watcher we prototyped sat at "
    "8-12% CPU while completely idle and kept a laptop's fans audible, which "
    "is unacceptable for a tool people leave installed and forget about. "
    "Drift and status checks therefore run on demand -- when the user opens a "
    "screen or explicitly refreshes -- never on a timer and never from a "
    "background thread that outlives the foreground session. Reviving "
    "continuous detection would need a new decision, not just an "
    "implementation."
)


def _sibling(name: str, module_name: str) -> ModuleType:
    """Load a sibling build script so its content-agnostic machinery is reused."""

    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load sibling builder: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_forge = _sibling("build_session_boundary_fixtures.py", "_drift_forge")

FixtureBuildError = _forge.FixtureBuildError
Embedder = _forge.Embedder
gateway_embedder = _forge.gateway_embedder
PLACEHOLDER_MODEL_ID = _forge.PLACEHOLDER_MODEL_ID
GATEWAY_MODEL_ID = _forge.GATEWAY_MODEL_ID
EMBEDDING_DIMENSIONS = _forge.EMBEDDING_DIMENSIONS


def _node(short_id: str, **overrides: Any) -> dict[str, Any]:
    """One canonical graph node with this cohort's shared defaults."""

    node: dict[str, Any] = {
        "short_id": short_id,
        "parent": None,
        "workspace": None,
        "type": "knowledge",
        "status": "active",
        "title": "",
        "body": "",
        "category": None,
        "topics": [],
        "created_by_source": "human",
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "content_updated_at": CREATED_AT,
        "embedding_updated_at": CREATED_AT,
        "embedding": [],
        "completed_at": None,
        "priority": None,
        "kind": "task",
        "source_type": "human",
        "anchor_files": [],
        "anchor_symbols": [],
        "hidden": False,
    }
    node.update(overrides)
    return node


def build_graph(embed: Embedder) -> dict[str, Any]:
    """The frozen graph: one component plus the seeded decision."""

    return {
        "schema_version": 1,
        # Project identity is the first thing a restored graph shows the agent,
        # so it reads as the real project, never as an experiment.
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
        "nodes": [
            _node(
                "COMP-1",
                type="component",
                title=COMPONENT_TITLE,
                body=COMPONENT_BODY,
                embedding=embed(_forge._node_input(COMPONENT_TITLE, COMPONENT_BODY)),
            ),
            _node(
                "KNOW-1",
                parent="COMP-1",
                title=DECISION_TITLE,
                body=DECISION_BODY,
                category="decision",
                topics=["performance", "architecture"],
                anchor_files=[ANCHORED_FILE],
                embedding=embed(_forge._node_input(DECISION_TITLE, DECISION_BODY)),
            ),
        ],
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


def _artifacts(graph: dict[str, Any], model_id: str) -> dict[str, bytes]:
    graph_bytes = _forge._canonical_bytes(graph)
    evidence_bytes = b"[]"
    manifest = {
        "schema_version": 1,
        "fixture_id": FIXTURE_ID,
        "cohort_id": COHORT_ID,
        # The decision's title is what the injection channels surface verbatim,
        # so it is the marker the runner scans the session transcript for.
        "memory_marker": DECISION_TITLE,
        "evidence_corpus": {
            "path": "corpora/evidence.json",
            "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            "media_type": _forge.MEDIA_TYPE,
            "ordering": "chronological ascending; no truncation",
        },
        "graphs": {
            "faithful": {
                "path": "graphs/faithful.json",
                "sha256": hashlib.sha256(graph_bytes).hexdigest(),
                "media_type": _forge.MEDIA_TYPE,
                "graph_format_version": _forge.GRAPH_FORMAT_VERSION,
                "backend_compatibility_id": _forge.BACKEND_COMPATIBILITY_ID,
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


def build(
    output_root: Path,
    *,
    replace: bool = False,
    embed: Embedder | None = None,
    model_id: str = PLACEHOLDER_MODEL_ID,
) -> str:
    """Write the single fixture; returns the faithful graph digest."""

    graph = build_graph(embed or _forge._placeholder_embedding)
    _forge._validate_with_provisioner(graph)
    artifacts = _artifacts(graph, model_id)
    for relative, payload in artifacts.items():
        _forge._write_artifact(output_root / relative, payload, replace=replace)
    return hashlib.sha256(artifacts["graphs/faithful.json"]).hexdigest()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="fixture directory")
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
        digest = build(args.output, replace=args.replace, embed=embed, model_id=model_id)
    except FixtureBuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"model_id": model_id, "graph_sha256": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
