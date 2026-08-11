"""Distinguish injection failure from task failure using a retained ATIF trajectory.

A session-boundary-recall attempt can fail for two very different reasons: the
fixture's memory never reached the agent (an injection failure, e.g. the
anchor mismatched, Auto-RAG stayed quiet, or the hook did not fire), or it did
reach the agent and the agent still did not act on it (a task failure). The
verifier's file-based reward alone cannot tell these apart. This module reads
the ATIF trajectory Harbor retains for a trial and checks whether a fixture's
declared marker text appears in a non-agent ("user" or "system", i.e.
auto-injected) step rather than one the agent wrote itself, which would only
prove the agent already knew or independently rediscovered the content.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .contracts import EvidenceDescriptor

_NON_AGENT_SOURCES = frozenset({"user", "system"})

# The Claude Code harness delivers Stele's injected context as hook attachments
# on the session transcript, not as ATIF trajectory steps (Harbor's normalizer
# drops them). Each carries a trailing provenance comment naming the channel:
# `<!-- stele:rag KNOW-1 -->` for the prompt-time Auto-RAG walk, or
# `<!-- stele:nudge recall-file -->` for the anchor-triggered file recall.
_INJECTION_ATTACHMENT_TYPE = "hook_additional_context"
_STELE_CHANNEL_TAG = re.compile(r"<!--\s*stele:([a-z0-9 _:-]+?)\s*-->", re.IGNORECASE)
_NODE_ID = re.compile(r"^[A-Z]+-\d+$")
_NODE_ID_IN_TEXT = re.compile(r"\b[A-Z]+-\d+\b")


class TrajectoryShapeError(ValueError):
    """Raised when a retained trajectory does not look like an ATIF envelope."""


@dataclass(frozen=True)
class MemoryInjectionEvidence:
    """Whether a fixture's marker text reached the agent as injected context."""

    marker_sha256: str
    injected: bool
    injected_step_indices: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "marker_sha256": self.marker_sha256,
            "injected": self.injected,
            "injected_step_indices": list(self.injected_step_indices),
        }


def detect_injected_marker(trajectory: object, marker: str) -> MemoryInjectionEvidence:
    """Scan an ATIF trajectory's non-agent steps for a fixture-declared marker.

    ``marker`` should be distinctive text drawn from the fixture's memory node
    (e.g. a sentence unlikely to appear by coincidence) so a match is credible
    evidence of injection rather than a generic phrase the agent might write
    on its own. A match on an "agent"-sourced step never counts: the agent
    authoring that exact phrase is not evidence Stele delivered it.
    """

    if not marker.strip():
        raise ValueError("marker must be non-empty")
    if not isinstance(trajectory, dict) or not isinstance(trajectory.get("steps"), list):
        raise TrajectoryShapeError("trajectory must be an ATIF envelope with a steps array")

    injected_indices: list[int] = []
    for index, step in enumerate(trajectory["steps"]):
        if not isinstance(step, dict) or step.get("source") not in _NON_AGENT_SOURCES:
            continue
        message = step.get("message")
        if isinstance(message, str) and marker in message:
            injected_indices.append(index)

    return MemoryInjectionEvidence(
        marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
        injected=bool(injected_indices),
        injected_step_indices=tuple(injected_indices),
    )


@dataclass(frozen=True)
class SessionInjectionEvidence:
    """Whether a fixture's marker reached the agent through Stele's injection channels."""

    marker_sha256: str
    injected: bool
    channels: tuple[str, ...]
    occurrences: int

    def as_dict(self) -> dict[str, object]:
        return {
            "marker_sha256": self.marker_sha256,
            "injected": self.injected,
            "channels": list(self.channels),
            "occurrences": self.occurrences,
        }


def _injection_channel(content: str, hook_name: str | None) -> str:
    """Name the Stele channel that carried an injection, from its provenance comment.

    ``<!-- stele:rag KNOW-1 COMP-1 -->`` → ``rag`` (node ids dropped);
    ``<!-- stele:nudge recall-file -->`` → ``nudge:recall-file``. Falls back to the
    hook name when no Stele comment is present.
    """

    match = _STELE_CHANNEL_TAG.search(content)
    if match is not None:
        tokens = [token for token in match.group(1).split() if not _NODE_ID.fullmatch(token)]
        if tokens:
            return ":".join(tokens)
    return hook_name or "unknown"


def detect_injection_in_session(
    events: Iterable[object], marker: str
) -> SessionInjectionEvidence:
    """Scan a Claude Code session transcript for a fixture marker in injected context.

    ``events`` is the parsed JSONL of a retained session transcript. Only
    ``hook_additional_context`` attachments count: these are authored by Stele's
    hooks and injected into the conversation, never written by the agent, so a
    match is credible evidence the memory was delivered rather than rediscovered.
    The channel each match arrived on (Auto-RAG walk vs anchor recall) is recorded
    so a later analysis can separate structural anchor recall from ordinary recall.
    """

    if not marker.strip():
        raise ValueError("marker must be non-empty")

    channels: set[str] = set()
    occurrences = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        attachment = event.get("attachment")
        if (
            not isinstance(attachment, dict)
            or attachment.get("type") != _INJECTION_ATTACHMENT_TYPE
        ):
            continue
        content = attachment.get("content")
        pieces = content if isinstance(content, list) else [content]
        hook_name = attachment.get("hookName")
        hook_name = hook_name if isinstance(hook_name, str) else None
        for piece in pieces:
            if isinstance(piece, str) and marker in piece:
                occurrences += 1
                channels.add(_injection_channel(piece, hook_name))

    return SessionInjectionEvidence(
        marker_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
        injected=occurrences > 0,
        channels=tuple(sorted(channels)),
        occurrences=occurrences,
    )


@dataclass(frozen=True)
class SessionDeliveryEvidence:
    """Any node-bearing Stele context delivered through a retained hook attachment."""

    injected: bool
    channels: tuple[str, ...]
    occurrences: int
    node_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "injected": self.injected,
            "channels": list(self.channels),
            "occurrences": self.occurrences,
            "node_ids": list(self.node_ids),
        }


def detect_stele_delivery_in_session(events: Iterable[object]) -> SessionDeliveryEvidence:
    """Detect node-bearing Stele delivery without requiring a fixture marker.

    Ordinary coding benchmarks have no benchmark-authored marker. They still
    need to distinguish "Stele injected graph context" from "no delivery".
    Count only hook attachments with Stele provenance and at least one node id;
    this excludes generic startup text and the first-edit reminder, neither of
    which delivered graph memory.
    """

    channels: set[str] = set()
    node_ids: set[str] = set()
    occurrences = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        attachment = event.get("attachment")
        if (
            not isinstance(attachment, dict)
            or attachment.get("type") != _INJECTION_ATTACHMENT_TYPE
        ):
            continue
        content = attachment.get("content")
        pieces = content if isinstance(content, list) else [content]
        hook_name = attachment.get("hookName")
        hook_name = hook_name if isinstance(hook_name, str) else None
        for piece in pieces:
            if not isinstance(piece, str) or _STELE_CHANNEL_TAG.search(piece) is None:
                continue
            delivered = set(_NODE_ID_IN_TEXT.findall(piece))
            if not delivered:
                continue
            occurrences += 1
            node_ids.update(delivered)
            channels.add(_injection_channel(piece, hook_name))

    return SessionDeliveryEvidence(
        injected=occurrences > 0,
        channels=tuple(sorted(channels)),
        occurrences=occurrences,
        node_ids=tuple(sorted(node_ids)),
    )


def write_memory_evidence(
    evidence: MemoryInjectionEvidence, root: Path, relative_path: str
) -> EvidenceDescriptor:
    """Persist injection evidence and describe it, without ever storing the raw marker."""

    payload = (json.dumps(evidence.as_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return EvidenceDescriptor(
        path=relative_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        media_type="application/json",
    )
