"""Versioned, backend-neutral contracts for benchmark fixtures and results."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FIXTURE_MANIFEST_VERSION = 1
CHALLENGE_MANIFEST_VERSION = 1
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
ChallengeId = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]


class ContractModel(BaseModel):
    """Strict base class so an artifact does not silently change its meaning."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactDescriptor(ContractModel):
    """A named immutable input or evidence artifact."""

    path: str = Field(min_length=1)
    sha256: Sha256
    media_type: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def path_is_portable_relative_posix(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            value.startswith("/")
            or (len(value) >= 3 and value[0].isalpha() and value[1:3] == ":/")
            or "\\" in value
            or ".." in path.parts
        ):
            raise ValueError("path must be a portable relative POSIX path without '..'")
        if str(path) in {".", ""} or str(path) != value:
            raise ValueError("path must name an artifact")
        return value


class EvidenceCorpusDescriptor(ArtifactDescriptor):
    """Immutable, cohort-scoped evidence with its construction provenance."""

    ordering: str = Field(min_length=1)


class GraphDescriptor(ArtifactDescriptor):
    """A graph snapshot used for one benchmark arm."""

    graph_format_version: str = Field(min_length=1)
    backend_compatibility_id: str = Field(min_length=1)
    embedding_model_id: str = Field(min_length=1)
    embedding_dimensions: int = Field(gt=0)


class EvidenceDescriptor(ArtifactDescriptor):
    """An artifact supporting a reported outcome or quality claim."""


class GraphArm(StrEnum):
    FAITHFUL = "faithful"
    KNOW_IT_ALL = "know-it-all"
    ANTAGONIST = "antagonist"


class FixtureManifestV1(ContractModel):
    """Portable fixture manifest; version is deliberately pinned to v1."""

    schema_version: Literal[1] = FIXTURE_MANIFEST_VERSION
    fixture_id: str = Field(min_length=1)
    cohort_id: str = Field(min_length=1)
    evidence_corpus: EvidenceCorpusDescriptor
    graphs: dict[GraphArm, GraphDescriptor] = Field(min_length=1)
    memory_marker: str | None = Field(
        default=None,
        description=(
            "Distinctive verbatim text from the memory node under test (e.g. its "
            "title), used post-run to decide whether the fixture's memory actually "
            "reached the agent as injected context versus the agent rediscovering "
            "it. Absent for a fixture that carries no session-boundary memory "
            "probe. The fixture owns this, not the challenge, so a challenge stays "
            "ignorant of the specific memory it is paired with."
        ),
    )

    @model_validator(mode="after")
    def includes_faithful_graph(self) -> FixtureManifestV1:
        if GraphArm.FAITHFUL not in self.graphs:
            raise ValueError("fixtures must include a faithful graph")
        return self

    @field_validator("memory_marker")
    @classmethod
    def memory_marker_is_distinctive(cls, value: str | None) -> str | None:
        # A short marker matches by coincidence and would report injection where
        # none happened, which is exactly the false signal this probe must avoid.
        if value is not None and (len(value.strip()) < 12 or value != value.strip()):
            raise ValueError("memory_marker must be trimmed and at least 12 characters")
        return value


class ChallengeEnvironmentV1(ContractModel):
    """Resource and connectivity limits used to construct an isolated task."""

    network_mode: Literal["none", "public"] = "public"
    build_timeout_seconds: float = Field(default=300, gt=0)
    cpus: int = Field(default=1, gt=0)
    memory_mb: int = Field(default=1024, gt=0)
    storage_mb: int = Field(default=2048, gt=0)
    prebuild_argv: tuple[str, ...] = Field(
        default=(),
        description=(
            "Optional exec-form command run after the pinned corpus is copied into "
            "the task image. Use it only for behavior-neutral dependency or build "
            "warming that belongs outside the measured agent phase."
        ),
    )

    @field_validator("prebuild_argv")
    @classmethod
    def prebuild_argv_is_safe_exec_form(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or "\x00" in value or "\n" in value or "\r" in value for value in values):
            raise ValueError("prebuild_argv entries must be non-empty single-line strings")
        return values


class ScriptVerifierV1(ContractModel):
    """A hidden executable that directly inspects the completed workspace."""

    kind: Literal["script"]
    runtime: Literal["posix-shell"]
    asset: ArtifactDescriptor
    timeout_seconds: float = Field(default=30, gt=0)


class AnswerFileVerifierV1(ContractModel):
    """A hidden executable that grades one declared answer file."""

    kind: Literal["answer-file"]
    runtime: Literal["posix-shell"]
    answer_path: str
    asset: ArtifactDescriptor
    timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("answer_path")
    @classmethod
    def answer_is_a_normalized_workspace_path(cls, value: str) -> str:
        return _validate_workspace_output_path(value)


ChallengeVerifierV1 = Annotated[
    ScriptVerifierV1 | AnswerFileVerifierV1,
    Field(discriminator="kind"),
]


class ChallengeManifestV1(ContractModel):
    """A runnable challenge, deliberately independent from an evidence fixture."""

    schema_version: Literal[1] = CHALLENGE_MANIFEST_VERSION
    challenge_id: ChallengeId
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    follow_up_prompt: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "A deliberately separate second-turn prompt issued after the first "
            "prompt's work is established, resuming the same agent session. "
            "Absent for an ordinary single-turn challenge. Present, this turns "
            "the challenge into a two-step session-boundary probe: the fixture "
            "(not this manifest) supplies whatever prior-session memory should "
            "or should not surface when the follow-up is issued."
        ),
    )
    solution_markers: tuple[str, ...] = Field(
        default=(),
        description=(
            "Distinctive phrases that amount to knowing this challenge's answer. "
            "Certification requires each to be absent from the packaged corpus "
            "and from agent-visible challenge text, so a no-memory control is "
            "genuinely memory-free. A graph fixture may legitimately contain "
            "them: that is the memory under test."
        ),
    )
    response_markers: tuple[str, ...] = Field(
        default=(),
        description=(
            "Literal phrases that, if any appears in the agent's response to the "
            "final turn, evidence recall of the fixture's memory. For a question "
            "whose correct answer is spoken rather than written to a file, the "
            "graded artifact is the transcript, which lives outside the "
            "container -- so the runner matches these host-side. Matching is a "
            "case-folded literal substring test over whitespace- and "
            "dash-normalized text: deterministic, no model in the loop. Choose "
            "phrases that cannot be produced without the memory."
        ),
    )
    allowed_output_paths: tuple[str, ...] = Field(min_length=1)
    agent_timeout_seconds: float = Field(default=300, gt=0)
    environment: ChallengeEnvironmentV1 = Field(default_factory=ChallengeEnvironmentV1)
    verifier: ChallengeVerifierV1

    @property
    def is_multi_step(self) -> bool:
        return self.follow_up_prompt is not None

    @property
    def is_response_graded(self) -> bool:
        """True when the memory signal is read from the transcript, not a file."""
        return bool(self.response_markers)

    @field_validator("response_markers")
    @classmethod
    def response_markers_are_distinctive(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        # Short markers match by coincidence, which would score generic advice as
        # recall -- the exact false positive this probe exists to avoid.
        if any(len(value.strip()) < 4 or value != value.strip() for value in values):
            raise ValueError("each response marker must be trimmed and at least 4 characters")
        if len(values) != len(set(values)):
            raise ValueError("response_markers must be unique")
        return values

    @field_validator("allowed_output_paths")
    @classmethod
    def outputs_are_unique_workspace_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_workspace_output_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_output_paths must be unique")
        return normalized

    @field_validator("solution_markers")
    @classmethod
    def solution_markers_are_distinctive(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        # A short marker matches by coincidence, which turns certification into
        # noise and trains the operator to wave findings through.
        if any(len(value.strip()) < 12 or value != value.strip() for value in values):
            raise ValueError("each solution marker must be trimmed and at least 12 characters")
        if len(values) != len(set(values)):
            raise ValueError("solution_markers must be unique")
        return values

    @model_validator(mode="after")
    def answer_file_is_an_allowed_output(self) -> ChallengeManifestV1:
        if (
            isinstance(self.verifier, AnswerFileVerifierV1)
            and self.verifier.answer_path not in self.allowed_output_paths
        ):
            raise ValueError("answer_path must be declared in allowed_output_paths")
        return self

    @model_validator(mode="after")
    def follow_up_prompt_is_distinct(self) -> ChallengeManifestV1:
        if self.follow_up_prompt is not None and self.follow_up_prompt == self.prompt:
            raise ValueError("follow_up_prompt must differ from the first-turn prompt")
        return self


def _validate_workspace_output_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value.startswith("/workspace/")
        or "\\" in value
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise ValueError("output path must be a normalized absolute path below /workspace")
    return value


class ConditionKind(StrEnum):
    NO_STELE = "no-stele"
    FULL_CONTEXT = "full-context"
    GRAPH = "graph"
    EXTERNAL = "external"


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    INFRASTRUCTURE_ERROR = "infrastructure-error"


class TokenMetrics(ContractModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class TimingMetrics(ContractModel):
    queue_seconds: float = Field(ge=0)
    execution_seconds: float = Field(ge=0)
    active_seconds: float = Field(ge=0)


class CostReceipt(ContractModel):
    """An authoritative provider receipt; it is never derived from a local estimate."""

    provider: str = Field(min_length=1)
    receipt_id: str | None = Field(default=None, min_length=1)
    artifact: EvidenceDescriptor | None = None
    amount: Decimal = Field(ge=0)
    currency: CurrencyCode

    @model_validator(mode="after")
    def has_receipt_reference(self) -> CostReceipt:
        if self.receipt_id is None and self.artifact is None:
            raise ValueError("receipt_id or artifact is required")
        return self


class CostMetrics(ContractModel):
    """Keep provider receipts distinct from locally calculated cross-checks."""

    authoritative_receipt: CostReceipt | None = None
    calculated_cross_check_cost: Decimal | None = Field(default=None, ge=0)
    calculated_cross_check_currency: CurrencyCode | None = None

    @model_validator(mode="after")
    def calculated_cost_has_currency(self) -> CostMetrics:
        has_amount = self.calculated_cross_check_cost is not None
        has_currency = self.calculated_cross_check_currency is not None
        if has_amount != has_currency:
            raise ValueError("calculated cross-check amount and currency must be supplied together")
        return self


class AttemptResultEnvelope(ContractModel):
    cohort_id: str = Field(min_length=1)
    fixture_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    challenge_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    attempt_index: int = Field(ge=0)
    condition_kind: ConditionKind
    graph_arm: GraphArm | None = None
    outcome: Outcome
    quality_evidence: tuple[EvidenceDescriptor, ...] = ()
    memory_evidence: tuple[EvidenceDescriptor, ...] = ()
    token_metrics: TokenMetrics
    cost_metrics: CostMetrics
    timings: TimingMetrics
    provenance_hashes: dict[str, Sha256] = Field(min_length=1)

    @model_validator(mode="after")
    def graph_arm_matches_condition(self) -> AttemptResultEnvelope:
        has_graph = self.graph_arm is not None
        if (self.condition_kind is ConditionKind.GRAPH) != has_graph:
            raise ValueError("graph_arm must be present exactly when condition_kind is graph")
        return self


class AggregateRecord(ContractModel):
    """A reported aggregate; callers provide values, this contract computes none."""

    cohort_id: str = Field(min_length=1)
    challenge_id: str = Field(min_length=1)
    fixture_id: str | None = Field(default=None, min_length=1)
    experiment_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    metric_name: str = Field(min_length=1)
    n: int = Field(ge=1)
    value: float | None = None
    dispersion: float | None = Field(default=None, ge=0)
    interval_low: float | None = None
    interval_high: float | None = None
    comparative_delta: float | None = None

    @model_validator(mode="after")
    def interval_is_ordered(self) -> AggregateRecord:
        has_low = self.interval_low is not None
        has_high = self.interval_high is not None
        if has_low != has_high:
            raise ValueError("interval bounds must be supplied together")
        if has_low and self.interval_low > self.interval_high:
            raise ValueError("interval_low must not exceed interval_high")
        if self.comparative_delta is not None and (not has_low or self.n < 2):
            raise ValueError("comparative_delta requires an interval and n >= 2")
        return self


class RunIdentity(ContractModel):
    """Opaque orchestrator identity supplied outside an evaluated agent."""

    run_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    challenge_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    attempt_index: int = Field(ge=0)


class ProvisionedProject(ContractModel):
    project_id: str = Field(min_length=1)
    fixture_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    graph_arm: GraphArm


class CanonicalGraphState(ContractModel):
    fixture_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    graph_arm: GraphArm
    canonical_state: GraphDescriptor
    declared_node_count: int = Field(ge=0)
    declared_edge_count: int = Field(ge=0)


class RestoreResult(ContractModel):
    """Public restore receipt; lets a schema-blind runner construct an observation."""

    project: ProvisionedProject
    queue_before_count: int = Field(ge=0)
    restore_seconds: float = Field(ge=0)


class SnapshotResult(ContractModel):
    """Public snapshot receipt, including deterministic retrieval observations."""

    state: CanonicalGraphState
    probes: tuple[RetrievalProbe, ...]
    queue_after_count: int = Field(ge=0)
    snapshot_seconds: float = Field(ge=0)

    def observation(self, restore: RestoreResult) -> RestoreObservation:
        return RestoreObservation(
            project=restore.project,
            state=self.state,
            probes=self.probes,
            queue_before_count=restore.queue_before_count,
            queue_after_count=self.queue_after_count,
            restore_seconds=restore.restore_seconds,
            snapshot_seconds=self.snapshot_seconds,
        )


class RetrievalProbeKind(StrEnum):
    FTS = "fts"
    SEMANTIC = "semantic"
    GRAPH = "graph"


class RetrievalProbe(ContractModel):
    """One named, deterministic retrieval observation used to prove restoration."""

    name: str = Field(min_length=1)
    kind: RetrievalProbeKind
    stable_ids: tuple[str, ...]
    scores: tuple[float, ...] | None = None

    @model_validator(mode="after")
    def scores_match_results(self) -> RetrievalProbe:
        if self.scores is not None and len(self.scores) != len(self.stable_ids):
            raise ValueError("scores must have one entry for each stable_id")
        return self


class RestoreObservation(ContractModel):
    """Canonical snapshot and queue state from one independent restore."""

    project: ProvisionedProject
    state: CanonicalGraphState
    probes: tuple[RetrievalProbe, ...]
    queue_before_count: int = Field(ge=0)
    queue_after_count: int = Field(ge=0)
    restore_seconds: float = Field(ge=0)
    snapshot_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def is_self_consistent(self) -> RestoreObservation:
        names = [probe.name for probe in self.probes]
        if len(names) != len(set(names)):
            raise ValueError("probe names must be unique")
        if {probe.kind for probe in self.probes} != set(RetrievalProbeKind):
            raise ValueError("probes must cover FTS, semantic, and graph exactly")
        if self.queue_before_count != self.queue_after_count:
            raise ValueError("queue count changed during restore observation")
        if self.project.project_id != self.state.project_id:
            raise ValueError("project and canonical state project_id must match")
        return self


class RestorationMismatch(ContractModel):
    subject: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    actual: str = Field(min_length=1)


class RestorationReportV1(ContractModel):
    """Versioned exact-restore receipt. Scores are exact unless tolerance is nonzero."""

    schema_version: Literal[1] = 1
    fixture_id: str = Field(min_length=1)
    cohort_id: str = Field(min_length=1)
    graph_arm: GraphArm
    first: RestoreObservation
    second: RestoreObservation
    score_tolerance: float = Field(default=0, ge=0)
    passed: bool
    mismatches: tuple[RestorationMismatch, ...] = ()

    @model_validator(mode="after")
    def passed_requires_no_mismatches(self) -> RestorationReportV1:
        if self.passed != (not self.mismatches):
            raise ValueError("passed must exactly reflect mismatches")
        return self


def build_restoration_report(
    fixture: FixtureManifestV1,
    arm: GraphArm,
    first: RestoreObservation,
    second: RestoreObservation,
    *,
    score_tolerance: float = 0,
) -> RestorationReportV1:
    """Compare two restores without permitting a partial or ambiguous success."""
    mismatches: list[RestorationMismatch] = []

    def compare(subject: str, expected: object, actual: object) -> None:
        if expected != actual:
            mismatches.append(
                RestorationMismatch(subject=subject, expected=str(expected), actual=str(actual))
            )

    for label, observation in (("first", first), ("second", second)):
        compare(f"{label}.project.fixture_id", fixture.fixture_id, observation.project.fixture_id)
        compare(f"{label}.project.graph_arm", arm, observation.project.graph_arm)
        compare(f"{label}.state.fixture_id", fixture.fixture_id, observation.state.fixture_id)
        compare(f"{label}.state.graph_arm", arm, observation.state.graph_arm)
        expected_graph = fixture.graphs[arm]
        actual_graph = observation.state.canonical_state
        for attribute in (
            "sha256",
            "graph_format_version",
            "backend_compatibility_id",
            "embedding_model_id",
            "embedding_dimensions",
        ):
            compare(
                f"{label}.fixture_graph.{attribute}",
                getattr(expected_graph, attribute),
                getattr(actual_graph, attribute),
            )
    if first.project.project_id == second.project.project_id:
        mismatches.append(
            RestorationMismatch(
                subject="project_ids_distinct",
                expected="distinct project IDs",
                actual=first.project.project_id,
            )
        )
    for attribute in (
        "sha256",
        "graph_format_version",
        "backend_compatibility_id",
        "embedding_model_id",
        "embedding_dimensions",
    ):
        compare(
            f"canonical_state.{attribute}",
            getattr(first.state.canonical_state, attribute),
            getattr(second.state.canonical_state, attribute),
        )
    compare(
        "declared_node_count", first.state.declared_node_count, second.state.declared_node_count
    )
    compare(
        "declared_edge_count", first.state.declared_edge_count, second.state.declared_edge_count
    )
    compare("queue_before_count", first.queue_before_count, second.queue_before_count)
    compare("queue_after_count", first.queue_after_count, second.queue_after_count)
    first_probes = {probe.name: probe for probe in first.probes}
    second_probes = {probe.name: probe for probe in second.probes}
    compare("probe_names", tuple(first_probes), tuple(second_probes))
    for name in first_probes.keys() & second_probes.keys():
        expected, actual = first_probes[name], second_probes[name]
        compare(f"probe.{name}.kind", expected.kind, actual.kind)
        compare(f"probe.{name}.stable_ids", expected.stable_ids, actual.stable_ids)
        if expected.scores is None or actual.scores is None:
            compare(f"probe.{name}.scores", expected.scores, actual.scores)
        elif any(abs(a - b) > score_tolerance for a, b in zip(expected.scores, actual.scores)):
            mismatches.append(
                RestorationMismatch(
                    subject=f"probe.{name}.scores",
                    expected=str(expected.scores),
                    actual=str(actual.scores),
                )
            )
    return RestorationReportV1(
        fixture_id=fixture.fixture_id,
        cohort_id=fixture.cohort_id,
        graph_arm=arm,
        first=first,
        second=second,
        score_tolerance=score_tolerance,
        passed=not mismatches,
        mismatches=tuple(mismatches),
    )
