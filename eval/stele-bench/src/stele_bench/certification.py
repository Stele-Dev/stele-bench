"""Fail-closed certification of every packaged input an evaluated agent can see.

A benchmark comparison is only meaningful if the arms differ in exactly the
thing under test.  Two classes of contamination silently destroy that:

*Leakage* — the answer, or a description of the obligation the challenge is
grading, is already sitting in the evaluated project tree.  Then a "no memory"
control is not memory-free, and the measured effect of a graph shrinks toward
zero for a reason that has nothing to do with the graph.

*Benchmark awareness* — the packaged inputs tell the agent it is being
measured (harness names, arm names, the word "fixture").  A model that knows it
is on a benchmark does not behave like one doing ordinary work.

The most easily missed source of both is a **dynamically auto-loaded** context
file: a real repository ships ``CLAUDE.md``, ``AGENTS.md``, ``.cursorrules`` or
a ``.claude/skills`` directory, the harness reads it into the system prompt
before the first turn, and nothing in the task definition mentions it.  Those
files are hand-written project memory, so packaging one is packaging a
competing memory system into every arm at once.

This module therefore does two separable jobs, and keeps them separable on
purpose.  :func:`redact_packaged_context` is the one sanctioned way to remove
that class of file from a pinned corpus nobody controls, and it returns a
record of exactly what it removed so a receipt can state it.  Everything else
here only *inspects*, never repairs: certification runs after redaction and
fails closed, so an unredacted or hand-edited tree cannot reach an agent.

Every probe is static: no model call, no network, no backend.  That is what
makes it runnable in CI and runnable before a paid run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .contracts import ChallengeManifestV1, FixtureManifestV1
from .corpus import MaterializedCorpus, rescan_materialized_tree

SURFACE_CORPUS = "corpus"
SURFACE_GRAPH = "graph"
SURFACE_EVIDENCE = "evidence-corpus"
SURFACE_CHALLENGE = "challenge"

KIND_AUTO_LOADED_CONTEXT = "auto-loaded-context"
KIND_BENCHMARK_AWARENESS = "benchmark-awareness"
KIND_PATCH_OR_DIFF = "patch-or-diff"
KIND_SOLUTION_LEAK = "solution-leak"
KIND_SUSPICIOUS_METADATA = "suspicious-metadata"

# Files a coding harness reads into context on its own, with no instruction to
# do so.  Packaging one hands every arm a hand-written memory file.  A bare
# name matches that basename anywhere in the tree; a trailing slash matches any
# path component, so ``.claude/`` covers ``.claude/skills/x/SKILL.md``.
AUTO_LOADED_CONTEXT_PATTERNS: tuple[str, ...] = (
    "AGENT.md",
    "AGENTS.md",
    "CLAUDE.local.md",
    "CLAUDE.md",
    "GEMINI.md",
    "QWEN.md",
    "copilot-instructions.md",
    ".aider.conf.yml",
    ".clinerules",
    ".cursorrules",
    ".goosehints",
    ".mcp.json",
    ".windsurfrules",
    ".claude/",
    ".clinerules/",
    ".codex/",
    ".continue/",
    ".cursor/",
    ".gemini/",
    ".junie/",
    ".opencode/",
    ".windsurf/",
)

# Metadata that has no business inside an evaluated project: repository
# internals, runner-owned task wiring, a product binding pointing at a real
# project, or credentials.
SUSPICIOUS_METADATA_PATTERNS: tuple[str, ...] = (
    ".env",
    ".git/",
    ".harbor/",
    ".stele/",
    "challenge-verifier.sh",
    "credentials.json",
    "id_rsa",
    "mutation-baseline.tsv",
    "task.toml",
)

# Terms that would tell an agent it is inside an experiment.  Deliberately
# narrow: each one is a harness or arm identity, not ordinary engineering
# prose, so a real repository does not trip them.  Words like "condition",
# "arm", "trial", "synthetic" and "ground truth" are NOT here — they occur
# naturally in real project knowledge and would make certification cry wolf.
HARNESS_IDENTITY_MARKERS: tuple[str, ...] = (
    "answer artifact",
    "antagonist",
    "benchmark",
    "benchmarks",
    "expected answer",
    "full-context",
    "grading rubric",
    "harbor",
    "know-it-all",
    "no-stele",
    "oracle",
    "stele-bench",
)

# Additional terms applied only to a graph's *identity* fields — the project,
# workspace and branch names and descriptions a restored graph shows an agent.
# Those strings are authored by the benchmark, so they are held to a stricter
# standard.  They are deliberately NOT applied to node bodies: a graph
# backfilled from a real project says "spin up a temp directory with test
# fixtures" in genuine engineering prose, and flagging that is noise.
GRAPH_IDENTITY_MARKERS: tuple[str, ...] = (
    "control arm",
    "eval harness",
    "fixture",
    "fixtures",
    "negative control",
)

# Locator roots inside a canonical graph document that count as identity.
_GRAPH_IDENTITY_ROOTS = frozenset({"branches", "project", "workspaces"})

_PATCH_SUFFIXES: tuple[str, ...] = (".diff", ".orig", ".patch", ".rej")
_PATCH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("diff --git", re.compile(r"^diff --git ", re.MULTILINE)),
    ("unified hunk header", re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)),
    ("unified file header", re.compile(r"^--- a/.*\n\+\+\+ b/", re.MULTILINE)),
)

_MAX_BYTES_PER_FILE = 128 * 1024
_MAX_GRAPH_BYTES = 64 * 1024 * 1024


class CertificationInputError(ValueError):
    """A certification input is missing or unreadable; never a silent skip."""


@dataclass(frozen=True, order=True)
class CertificationFinding:
    """One reason a packaged input must not reach an evaluated agent."""

    surface: str
    arm: str
    locator: str
    kind: str
    marker: str
    line: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "arm": self.arm,
            "locator": self.locator,
            "kind": self.kind,
            "marker": self.marker,
            "line": self.line,
        }

    def describe(self) -> str:
        location = self.locator if self.line is None else f"{self.locator}:{self.line}"
        return f"{self.surface}[{self.arm}] {location} {self.kind}={self.marker!r}"


class CertificationError(RuntimeError):
    """Packaged inputs failed certification; the run must not proceed."""

    def __init__(self, findings: Iterable[CertificationFinding]) -> None:
        self.findings = tuple(sorted(findings))
        preview = "; ".join(finding.describe() for finding in self.findings[:8])
        suffix = "" if len(self.findings) <= 8 else f" (+{len(self.findings) - 8} more)"
        super().__init__(f"packaged inputs are not certifiable: {preview}{suffix}")


@dataclass(frozen=True)
class RedactionRecord:
    """One file removed from a packaged corpus, with why and what it was."""

    path: str
    sha256: str
    kind: str
    pattern: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "kind": self.kind,
            "pattern": self.pattern,
        }


@dataclass(frozen=True)
class CertificationPolicy:
    """The declared, inspectable contract for what may reach an agent."""

    auto_loaded_context_patterns: tuple[str, ...] = AUTO_LOADED_CONTEXT_PATTERNS
    suspicious_metadata_patterns: tuple[str, ...] = SUSPICIOUS_METADATA_PATTERNS
    harness_identity_markers: tuple[str, ...] = HARNESS_IDENTITY_MARKERS
    graph_identity_markers: tuple[str, ...] = GRAPH_IDENTITY_MARKERS
    patch_suffixes: tuple[str, ...] = _PATCH_SUFFIXES
    max_bytes_per_file: int = _MAX_BYTES_PER_FILE

    def __post_init__(self) -> None:
        if self.max_bytes_per_file < 1:
            raise ValueError("max_bytes_per_file must be positive")
        groups = (
            self.auto_loaded_context_patterns,
            self.suspicious_metadata_patterns,
            self.harness_identity_markers,
            self.graph_identity_markers,
            self.patch_suffixes,
        )
        if any(not value.strip() for group in groups for value in group):
            raise ValueError("certification policy entries must not be blank")

    @property
    def redactable_patterns(self) -> tuple[tuple[str, str], ...]:
        """Every pattern redaction may remove, paired with its finding kind."""

        return tuple(
            (pattern, kind)
            for kind, group in (
                (KIND_AUTO_LOADED_CONTEXT, self.auto_loaded_context_patterns),
                (KIND_SUSPICIOUS_METADATA, self.suspicious_metadata_patterns),
            )
            for pattern in group
        )

    def markers_for_graph_locator(self, locator: str) -> tuple[str, ...]:
        """Identity fields get the strict set; node content gets harness terms only."""

        root = locator.split(".", 1)[0].split("[", 1)[0]
        if root in _GRAPH_IDENTITY_ROOTS:
            return tuple(sorted({*self.harness_identity_markers, *self.graph_identity_markers}))
        return self.harness_identity_markers


@dataclass(frozen=True)
class CertificationReceipt:
    """A deterministic, zero-model record of what was probed and what passed."""

    corpus_tree_sha256: str | None
    fixture_id: str | None
    cohort_id: str | None
    arms: tuple[str, ...]
    challenge_ids: tuple[str, ...]
    surfaces_probed: tuple[str, ...]
    files_probed: int
    graph_values_probed: int
    redactions: tuple[RedactionRecord, ...] = ()
    findings: tuple[CertificationFinding, ...] = ()

    @property
    def certified(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "certified": self.certified,
            "zero_model": True,
            "corpus_tree_sha256": self.corpus_tree_sha256,
            "fixture_id": self.fixture_id,
            "cohort_id": self.cohort_id,
            "arms": list(self.arms),
            "challenge_ids": list(self.challenge_ids),
            "surfaces_probed": list(self.surfaces_probed),
            "files_probed": self.files_probed,
            "graph_values_probed": self.graph_values_probed,
            "redactions": [record.as_dict() for record in self.redactions],
            "findings": [finding.as_dict() for finding in self.findings],
        }


def _matched_pattern(relative: str, patterns: Sequence[str]) -> str | None:
    """Match a bare name against any basename, or ``dir/`` against any component."""

    parts = PurePosixPath(relative).parts
    if not parts:
        return None
    folded = tuple(part.casefold() for part in parts)
    for pattern in patterns:
        if pattern.endswith("/"):
            if pattern[:-1].casefold() in folded:
                return pattern
        elif folded[-1] == pattern.casefold():
            return pattern
    return None


def _token_pattern(marker: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(marker)}(?![A-Za-z0-9])", re.IGNORECASE)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _scan_text(
    text: str,
    *,
    surface: str,
    arm: str,
    locator: str,
    markers: Sequence[str],
    solution_markers: Sequence[str] = (),
    detect_patches: bool = True,
) -> list[CertificationFinding]:
    findings: list[CertificationFinding] = []
    for marker in markers:
        match = _token_pattern(marker).search(text)
        if match is not None:
            findings.append(
                CertificationFinding(
                    surface,
                    arm,
                    locator,
                    KIND_BENCHMARK_AWARENESS,
                    marker,
                    _line_of(text, match.start()),
                )
            )
    for marker in solution_markers:
        index = text.casefold().find(marker.casefold())
        if index >= 0:
            findings.append(
                CertificationFinding(
                    surface,
                    arm,
                    locator,
                    KIND_SOLUTION_LEAK,
                    marker,
                    _line_of(text, index),
                )
            )
    if detect_patches:
        for label, pattern in _PATCH_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                findings.append(
                    CertificationFinding(
                        surface,
                        arm,
                        locator,
                        KIND_PATCH_OR_DIFF,
                        label,
                        _line_of(text, match.start()),
                    )
                )
    return findings


def _read_bounded_text(path: Path, limit: int) -> str | None:
    with path.open("rb") as source:
        payload = source.read(limit)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def redact_packaged_context(
    corpus: MaterializedCorpus, policy: CertificationPolicy = CertificationPolicy()
) -> tuple[MaterializedCorpus, tuple[RedactionRecord, ...]]:
    """Remove auto-loaded harness context and stray metadata from a materialized tree.

    A pinned corpus belongs to someone else, so its ``CLAUDE.md`` cannot be
    edited away upstream; it is removed here instead, identically for every
    arm, and the returned records are what a receipt should publish.  The
    corpus manifest is recomputed so downstream copies still fail closed on any
    undeclared change.
    """

    root = corpus.destination.resolve(strict=True)
    records: list[RedactionRecord] = []
    for entry in sorted(corpus.entries, key=lambda item: item.path):
        pattern_and_kind = matched_redaction_pattern(entry.path, policy)
        if pattern_and_kind is None:
            continue
        pattern, kind = pattern_and_kind
        target = root.joinpath(*PurePosixPath(entry.path).parts)
        if target.is_symlink() or target.is_file():
            target.unlink()
        records.append(RedactionRecord(entry.path, entry.sha256, kind, pattern))
    _prune_empty_directories(root)
    return rescan_materialized_tree(corpus), tuple(records)


def matched_redaction_pattern(
    relative: str, policy: CertificationPolicy = CertificationPolicy()
) -> tuple[str, str] | None:
    """Return ``(pattern, kind)`` when a packaged path must not reach an agent."""

    for kind, patterns in (
        (KIND_AUTO_LOADED_CONTEXT, policy.auto_loaded_context_patterns),
        (KIND_SUSPICIOUS_METADATA, policy.suspicious_metadata_patterns),
    ):
        pattern = _matched_pattern(relative, patterns)
        if pattern is not None:
            return pattern, kind
    return None


def _prune_empty_directories(root: Path) -> None:
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()


def certify_project_tree(
    root: Path,
    *,
    solution_markers: Sequence[str] = (),
    policy: CertificationPolicy = CertificationPolicy(),
    arm: str = "shared",
    surface: str = SURFACE_CORPUS,
    ignored_top_level_components: Sequence[str] = (),
) -> tuple[tuple[CertificationFinding, ...], int]:
    """Probe an evaluated project tree; returns findings and files probed."""

    root = _require_directory(root, "project tree")
    findings: list[CertificationFinding] = []
    probed = 0
    # A matched directory is reported once, at its shallowest path; listing
    # every descendant of a `.claude/` tree buries the other findings.
    reported: set[str] = set()
    ignored = {component.casefold() for component in ignored_top_level_components}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        parts = PurePosixPath(relative).parts
        if parts and parts[0].casefold() in ignored:
            continue
        pattern_and_kind = matched_redaction_pattern(relative, policy)
        parent = PurePosixPath(relative).parent.as_posix()
        if pattern_and_kind is not None and parent not in reported:
            pattern, kind = pattern_and_kind
            findings.append(CertificationFinding(surface, arm, relative, kind, pattern))
            reported.add(relative)
        elif pattern_and_kind is not None:
            reported.add(relative)
        if path.is_symlink() or not path.is_file():
            continue
        probed += 1
        suffix = PurePosixPath(relative).suffix.casefold()
        if suffix in policy.patch_suffixes:
            findings.append(
                CertificationFinding(surface, arm, relative, KIND_PATCH_OR_DIFF, suffix)
            )
        text = _read_bounded_text(path, policy.max_bytes_per_file)
        if text is None:
            continue
        findings.extend(
            _scan_text(
                text,
                surface=surface,
                arm=arm,
                locator=relative,
                markers=policy.harness_identity_markers,
                solution_markers=solution_markers,
            )
        )
    return tuple(sorted(findings)), probed


def certify_graph_document(
    payload: object,
    *,
    arm: str,
    policy: CertificationPolicy = CertificationPolicy(),
) -> tuple[tuple[CertificationFinding, ...], int]:
    """Statically probe every string a restored graph would put in front of an agent."""

    findings: list[CertificationFinding] = []
    values = tuple(_graph_text_values(payload))
    for locator, text in values:
        findings.extend(
            _scan_text(
                text,
                surface=SURFACE_GRAPH,
                arm=arm,
                locator=locator,
                markers=policy.markers_for_graph_locator(locator),
            )
        )
    return tuple(sorted(findings)), len(values)


def _graph_text_values(payload: object, prefix: str = "") -> Iterable[tuple[str, str]]:
    """Walk a canonical graph document and yield every readable string.

    The walk is generic on purpose.  A field-by-field allowlist silently stops
    covering a field the graph format gains later, which is the exact way a
    leak reaches an agent unnoticed.
    """

    if isinstance(payload, str):
        if payload:
            yield prefix or "<root>", payload
        return
    if isinstance(payload, Mapping):
        for key in sorted(payload, key=str):
            yield from _graph_text_values(payload[key], f"{prefix}.{key}" if prefix else str(key))
        return
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            label = _graph_item_label(item, index)
            yield from _graph_text_values(item, f"{prefix}[{label}]" if prefix else f"[{label}]")


def _graph_item_label(item: object, index: int) -> str:
    if isinstance(item, Mapping):
        for key in ("short_id", "name", "key", "branch_name"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return str(index)


def certify_challenge_manifest(
    manifest: ChallengeManifestV1,
    *,
    policy: CertificationPolicy = CertificationPolicy(),
) -> tuple[CertificationFinding, ...]:
    """Probe the challenge text an agent actually reads, including its own answer."""

    visible = {
        "challenge_id": manifest.challenge_id,
        "title": manifest.title,
        "description": manifest.description,
        "prompt": manifest.prompt,
        "follow_up_prompt": manifest.follow_up_prompt or "",
    }
    findings: list[CertificationFinding] = []
    for locator, text in visible.items():
        findings.extend(
            _scan_text(
                text,
                surface=SURFACE_CHALLENGE,
                arm=manifest.challenge_id,
                locator=locator,
                markers=policy.harness_identity_markers,
                solution_markers=manifest.solution_markers,
                detect_patches=False,
            )
        )
    return tuple(sorted(findings))


def certify_packaged_inputs(
    *,
    corpus_root: Path | None,
    fixture_root: Path | None = None,
    fixture: FixtureManifestV1 | None = None,
    challenges: Sequence[ChallengeManifestV1] = (),
    corpus_tree_sha256: str | None = None,
    redactions: Sequence[RedactionRecord] = (),
    policy: CertificationPolicy = CertificationPolicy(),
    raise_on_finding: bool = True,
) -> CertificationReceipt:
    """Certify the corpus, every declared graph arm, and every selected challenge.

    ``fixture_root`` and ``fixture`` are supplied together or not at all.  A
    missing corpus, fixture artifact, or unreadable graph is an error, never a
    skipped check: a certification that quietly covers nothing is worse than
    none, because it still reports success.
    """

    if (fixture_root is None) != (fixture is None):
        raise CertificationInputError("fixture_root and fixture must be supplied together")

    findings: list[CertificationFinding] = []
    surfaces: list[str] = []
    solution_markers = tuple(
        marker for challenge in challenges for marker in challenge.solution_markers
    )
    files_probed = 0
    graph_values = 0
    arms: tuple[str, ...] = ()

    if corpus_root is not None:
        corpus_findings, files_probed = certify_project_tree(
            corpus_root, solution_markers=solution_markers, policy=policy
        )
        findings.extend(corpus_findings)
        surfaces.append(SURFACE_CORPUS)

    if fixture_root is not None and fixture is not None:
        fixture_root = _require_directory(fixture_root, "fixture root")
        arms = tuple(str(arm) for arm in sorted(fixture.graphs))
        evidence_findings, evidence_probed = _certify_evidence_corpus(
            fixture_root, fixture, solution_markers=solution_markers, policy=policy
        )
        findings.extend(evidence_findings)
        files_probed += evidence_probed
        surfaces.append(SURFACE_EVIDENCE)
        for arm in arms:
            payload = _load_graph(fixture_root, fixture, arm, policy)
            arm_findings, probed = certify_graph_document(payload, arm=arm, policy=policy)
            findings.extend(arm_findings)
            graph_values += probed
        surfaces.append(SURFACE_GRAPH)

    for challenge in challenges:
        findings.extend(certify_challenge_manifest(challenge, policy=policy))
    if challenges:
        surfaces.append(SURFACE_CHALLENGE)

    receipt = CertificationReceipt(
        corpus_tree_sha256=corpus_tree_sha256,
        fixture_id=fixture.fixture_id if fixture is not None else None,
        cohort_id=fixture.cohort_id if fixture is not None else None,
        arms=arms,
        challenge_ids=tuple(challenge.challenge_id for challenge in challenges),
        surfaces_probed=tuple(surfaces),
        files_probed=files_probed,
        graph_values_probed=graph_values,
        redactions=tuple(redactions),
        findings=tuple(sorted(findings)),
    )
    if raise_on_finding and receipt.findings:
        raise CertificationError(receipt.findings)
    return receipt


def _certify_evidence_corpus(
    fixture_root: Path,
    fixture: FixtureManifestV1,
    *,
    solution_markers: Sequence[str],
    policy: CertificationPolicy,
) -> tuple[tuple[CertificationFinding, ...], int]:
    descriptor = fixture.evidence_corpus
    path = _resolve_artifact(fixture_root, descriptor.path, "evidence corpus")
    text = _read_bounded_text(path, _MAX_GRAPH_BYTES)
    if text is None:
        raise CertificationInputError("evidence corpus is not readable UTF-8 text")
    findings = _scan_text(
        text,
        surface=SURFACE_EVIDENCE,
        arm="full-context",
        locator=descriptor.path,
        markers=policy.harness_identity_markers,
        solution_markers=solution_markers,
    )
    return tuple(sorted(findings)), 1


def _load_graph(
    fixture_root: Path, fixture: FixtureManifestV1, arm: str, policy: CertificationPolicy
) -> object:
    descriptor = fixture.graphs[arm]  # type: ignore[index]
    path = _resolve_artifact(fixture_root, descriptor.path, f"graph arm {arm!r}")
    if path.stat().st_size > _MAX_GRAPH_BYTES:
        raise CertificationInputError(f"graph arm {arm!r} exceeds the certification size limit")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != descriptor.sha256:
        raise CertificationInputError(f"graph arm {arm!r} does not match its declared digest")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CertificationInputError(f"graph arm {arm!r} is not readable JSON") from error


def _resolve_artifact(fixture_root: Path, relative: str, label: str) -> Path:
    candidate = PurePosixPath(relative)
    if relative.startswith("/") or ".." in candidate.parts or candidate.as_posix() != relative:
        raise CertificationInputError(f"{label} path is not a contained relative path")
    path = fixture_root.joinpath(*candidate.parts)
    if path.is_symlink() or not path.is_file():
        raise CertificationInputError(f"{label} is missing from the fixture: {relative}")
    return path


def _require_directory(path: Path, label: str) -> Path:
    if not path.exists():
        raise CertificationInputError(
            f"{label} is unavailable at {path}; certification is never skipped"
        )
    if not path.is_dir():
        raise CertificationInputError(f"{label} is not a directory: {path}")
    return path.resolve(strict=True)


def write_certification_receipt(receipt: CertificationReceipt, path: Path) -> str:
    """Persist a receipt deterministically and return its digest."""

    payload = (
        json.dumps(receipt.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "AUTO_LOADED_CONTEXT_PATTERNS",
    "GRAPH_IDENTITY_MARKERS",
    "HARNESS_IDENTITY_MARKERS",
    "SUSPICIOUS_METADATA_PATTERNS",
    "CertificationError",
    "CertificationFinding",
    "CertificationInputError",
    "CertificationPolicy",
    "CertificationReceipt",
    "RedactionRecord",
    "certify_challenge_manifest",
    "certify_graph_document",
    "certify_packaged_inputs",
    "certify_project_tree",
    "matched_redaction_pattern",
    "redact_packaged_context",
    "write_certification_receipt",
]
