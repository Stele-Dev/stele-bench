"""Durable result records for scored runs, and what makes two of them comparable.

A run receipt lives in machine-local state (`~/.local/state/stele-bench/runs/…`)
alongside gigabytes of corpus trees and trajectories, and `clean --delete-work`
is expected to be run. That is fine for forensics and useless as a record: a
reimaged laptop takes every score with it, and nothing can be compared across
runs because each run is an island.

This module extracts the small, durable part — who ran what, against which
pinned inputs, and what came out — into records built from the contracts in
`contracts.py` rather than a third ad-hoc shape. The records are written into
the repository, so the scientific record travels with the code that produced it
and diffs like anything else.

**The comparability rule is the load-bearing idea here.** Regression detection
means plotting one question's score across runs, and that is only meaningful
when the runs asked the same question of the same model against the same graph.
Change the verifier and the score moves without memory having changed at all.
So a record carries an explicit comparability key, and the viewer refuses to put
two different keys on one line.

One input is deliberately **excluded** from that key: the artifact digest. The
artifact is the product under test — the thing whose effect on the score we are
trying to see. Including it would make every build its own incomparable island
and turn regression detection off exactly when it matters. It is recorded per
point instead, so a step in the trend can be attributed to the build that caused
it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field, model_validator

from . import metrics as metrics_module
from . import statistics
from .contracts import (
    AggregateRecord,
    AttemptResultEnvelope,
    ConditionKind,
    ContractModel,
    CostMetrics,
    GraphArm,
    Outcome,
    Sha256,
    TimingMetrics,
    TokenMetrics,
)

RESULT_RECORD_VERSION = 1

# Conditions that install Stele. Kept here rather than imported from the private
# runner so the published package can read a record without the provisioner
# present. `stele-fresh` installs it and creates its project in place rather than
# restoring a frozen graph — it is a different provisioning mode, not a fixture
# variant, which is why it has its own name.
GRAPH_CONDITIONS = ("faithful", "faithful-norag", "stele-fresh")

_RUN_SHAPES = ("pair", "quad", "repeat", "suite")
_RECEIPT_ID_KEYS = {
    "pair": "pair_id",
    "quad": "quad_id",
    "repeat": "repeat_id",
    "suite": "suite_id",
}

# Metrics whose per-arm values are worth keeping across runs. Everything else in
# a receipt is either provenance or forensic detail that belongs with the run.
TRACKED_METRICS = (
    "cited_decision",
    "reward",
    "investigative_tool_calls",
    "session_investigative_tool_calls",
    "tokens_input",
    "tokens_output",
    "claude_reported_usd",
    "trial_wall_seconds",
)


class ResultsError(RuntimeError):
    """A run receipt could not be turned into a durable record."""


class ComparabilityKey(ContractModel):
    """Everything that must match for two runs to be measuring the same thing.

    Note what is absent: the artifact digest. See the module docstring — the
    build under test is the independent variable, not part of the question's
    identity.
    """

    challenge_id: str = Field(min_length=1)
    fixture_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    control_condition: str = Field(min_length=1)
    treatment_condition: str = Field(min_length=1)
    challenge_manifest_sha256: Sha256
    verifier_sha256: Sha256
    source_commit: str = Field(min_length=1)

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def label(self) -> str:
        """A short human label for a trend line.

        The fixture is in here because it is often the only thing separating two
        lines — a quad's variants all run the same challenge with the same
        conditions and differ solely in the graph they were given.
        """

        return (
            f"{self.challenge_id} · {self.fixture_id} · {self.model} · "
            f"{self.treatment_condition} vs {self.control_condition}"
        )


class PublicationDecision(ContractModel):
    """Whether a completed run may contribute to a published result.

    Execution status and scientific admission are deliberately separate. A run
    can complete successfully and still be excluded because its prompt,
    verifier, resource envelope, or execution violated the preregistration.
    """

    decision: Literal["accepted", "excluded"]
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RunRecord(ContractModel):
    """The durable record of one scored run."""

    schema_version: Literal[1] = RESULT_RECORD_VERSION
    run_id: str = Field(min_length=1)
    run_shape: Literal["pair", "quad", "repeat", "suite"]
    status: str = Field(min_length=1)
    publication: PublicationDecision | None = None
    recorded_at: str = Field(min_length=1)
    comparability: ComparabilityKey
    comparability_digest: str = Field(min_length=64, max_length=64)
    # Recorded, not keyed on: which build produced this point.
    artifact_sha256: Sha256
    attempts_run: int = Field(ge=0)
    attempts_analysed: int = Field(ge=0)
    attempts: tuple[AttemptResultEnvelope, ...] = ()
    aggregates: tuple[AggregateRecord, ...] = ()
    # Per-metric treatment-vs-control verdict: the difference and, for binary
    # measures only, the exact one-sided p. `AggregateRecord` has no field for a
    # test result and is a published contract, so this rides alongside it.
    verdicts: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    # Per role: attempts, how many had the memory injected, how many scored the
    # marker. This is the three-layer reading — injected → observably used →
    # scored — and it is the difference between "the lesson never reached the
    # agent" and "it did and the agent ignored it". A reward alone cannot say
    # which, and across runs it is the first thing a regression needs to
    # distinguish.
    memory_uptake: dict[str, dict[str, int]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def digest_matches_key(self) -> RunRecord:
        if self.comparability_digest != self.comparability.digest():
            raise ValueError("comparability_digest does not match comparability")
        if self.attempts_analysed > self.attempts_run:
            raise ValueError("attempts_analysed must not exceed attempts_run")
        return self


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResultsError(f"receipt {label} is missing or malformed")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResultsError(f"receipt {label} is missing or malformed")
    return value


def detect_run_shape(receipt: Mapping[str, Any]) -> str:
    for shape, key in _RECEIPT_ID_KEYS.items():
        if isinstance(receipt.get(key), str):
            return shape
    raise ResultsError("receipt does not identify a known run shape")


def _condition_kind(condition: str) -> ConditionKind:
    if condition == "no-stele":
        return ConditionKind.NO_STELE
    if condition in GRAPH_CONDITIONS:
        return ConditionKind.GRAPH
    raise ResultsError(f"unknown condition: {condition}")


def _suite_verdicts(summary: Mapping[str, Any]) -> dict[str, dict[str, float | None]]:
    """The non-inferiority reading, kept alongside the per-measure verdicts.

    `difference` here is the paired difference in solve rate and the bounds are
    its interval — not either arm's own rate. The margin travels with it because
    a bound means nothing without the threshold it was compared against, and a
    record that dropped the margin would let a later reader pick one.
    """

    verdict = summary.get("non_inferiority")
    difference = summary.get("solve_rate")
    if not isinstance(verdict, Mapping) or not isinstance(difference, Mapping):
        return {}
    bounds = difference.get("difference_95")
    low = high = None
    if isinstance(bounds, Sequence) and len(bounds) == 2:
        low, high = float(bounds[0]), float(bounds[1])
    return {
        "solve_rate_non_inferiority": {
            "difference": _number(difference.get("difference")),
            "interval_low": low,
            "interval_high": high,
            "margin": _number(verdict.get("margin")),
            # Encoded rather than named because the field is typed as numeric:
            # 1 non-inferior, 0 inconclusive, -1 inferior. The verdict string
            # itself stays in the run receipt.
            "verdict": {"non_inferior": 1.0, "inconclusive": 0.0, "inferior": -1.0}.get(
                str(verdict.get("verdict"))
            ),
        }
    }


_OUTCOMES = {
    "passed": Outcome.PASSED,
    "failed": Outcome.FAILED,
    # The runner's "invalid" means the apparatus disowned the trial, which is an
    # infrastructure problem, not a score of zero.
    "invalid": Outcome.INFRASTRUCTURE_ERROR,
    "incomplete": Outcome.INCOMPLETE,
}


def _arm_outcome(arm: Any, fallback: Any) -> Outcome:
    """Read an arm's verdict from its own receipt, which is the authority.

    The attempt-level `outcomes` map is a later convenience and is simply absent
    from earlier receipts; taking it as the source would silently score every
    historical arm as `incomplete`.
    """

    if isinstance(arm, Mapping):
        # An arm whose command failed never produced a verdict, however the
        # trial was labelled — that is infrastructure, not a result.
        if arm.get("exit_code") not in (0, None):
            return Outcome.INFRASTRUCTURE_ERROR
        receipt = arm.get("receipt")
        if isinstance(receipt, Mapping):
            results = receipt.get("results")
            if isinstance(results, list) and results and isinstance(results[0], Mapping):
                raw = results[0].get("outcome")
                if isinstance(raw, str) and raw in _OUTCOMES:
                    return _OUTCOMES[raw]
            raw = receipt.get("outcome")
            if isinstance(raw, str) and raw in _OUTCOMES:
                return _OUTCOMES[raw]
    if isinstance(fallback, str) and fallback in _OUTCOMES:
        return _OUTCOMES[fallback]
    return Outcome.INCOMPLETE


def _int_metric(metrics: Mapping[str, Any], name: str) -> int:
    value = metrics.get(name)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return 0


def _float_metric(metrics: Mapping[str, Any], name: str) -> float:
    value = metrics.get(name)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return 0.0


def attempt_envelope(
    *,
    run_id: str,
    challenge_id: str,
    fixture_id: str,
    condition: str,
    role: str,
    attempt_index: int,
    outcome: Outcome,
    metrics: Mapping[str, Any],
    provenance_hashes: Mapping[str, str],
) -> AttemptResultEnvelope:
    """Build one attempt envelope from an arm's parsed metrics."""

    kind = _condition_kind(condition)
    return AttemptResultEnvelope(
        cohort_id=fixture_id,
        fixture_id=fixture_id,
        experiment_id=run_id,
        condition_id=condition,
        challenge_id=challenge_id,
        attempt_id=f"{run_id}-attempt-{attempt_index + 1:02d}-{role}",
        attempt_index=attempt_index,
        condition_kind=kind,
        graph_arm=GraphArm.FAITHFUL if kind is ConditionKind.GRAPH else None,
        outcome=outcome,
        token_metrics=TokenMetrics(
            input_tokens=_int_metric(metrics, "tokens_input"),
            output_tokens=_int_metric(metrics, "tokens_output"),
            cache_read_tokens=_int_metric(metrics, "tokens_cache_read"),
            cache_write_tokens=_int_metric(metrics, "tokens_cache_write"),
        ),
        cost_metrics=CostMetrics(
            # The model's self-report is an estimate, not a provider receipt —
            # it belongs in the cross-check field. `authoritative_receipt` stays
            # empty until the Gateway total is recorded against the run.
            calculated_cross_check_cost=round(_float_metric(metrics, "claude_reported_usd"), 6),
            calculated_cross_check_currency="USD",
        ),
        timings=TimingMetrics(
            queue_seconds=0.0,
            execution_seconds=_float_metric(metrics, "agent_execution_seconds"),
            active_seconds=_float_metric(metrics, "trial_wall_seconds"),
        ),
        provenance_hashes=dict(provenance_hashes),
    )


# Role keys as written by different generations of the oracle. The treatment
# role was called "faithful" until the control itself could be a graph condition
# made the name ambiguous.
_ROLE_ALIASES = {"control": ("control",), "treatment": ("treatment", "faithful")}


def _role_field(source: Mapping[str, Any] | None, role: str, default: Any = None) -> Any:
    if not isinstance(source, Mapping):
        return default
    for name in _ROLE_ALIASES[role]:
        if name in source:
            return source[name]
    return default


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _aggregates_from_summary(
    *,
    summary: Mapping[str, Any],
    key: ComparabilityKey,
    run_id: str,
) -> tuple[list[AggregateRecord], dict[str, dict[str, float | None]]]:
    """Turn a repeat summary into aggregate records plus per-metric verdicts.

    Binary measures become one record per arm carrying the arm's rate and its
    Wilson interval — never a bare point, because at this N the interval is most
    of the information. Continuous measures become a single record of the median
    *paired difference*, with no interval, matching the summary's own refusal to
    attach a test to them.

    The one-sided p has nowhere to live in `AggregateRecord`, and widening a
    published contract to carry it would be the tail wagging the dog, so it is
    returned alongside as a per-metric verdict.
    """

    records: list[AggregateRecord] = []
    verdicts: dict[str, dict[str, float | None]] = {}
    conditions = {"control": key.control_condition, "treatment": key.treatment_condition}

    binary = summary.get("binary_measures")
    if isinstance(binary, Mapping):
        for metric, payload in binary.items():
            if not isinstance(payload, Mapping):
                continue
            verdicts[metric] = {
                "difference": _number(payload.get("difference")),
                "fisher_one_sided_p": _number(payload.get("fisher_one_sided_p")),
            }
            for role, condition in conditions.items():
                arm = payload.get(role)
                if not isinstance(arm, Mapping):
                    continue
                trials = arm.get("trials")
                if not isinstance(trials, int) or trials < 1:
                    continue
                interval = arm.get("wilson_95")
                low = high = None
                if isinstance(interval, Sequence) and len(interval) == 2:
                    low, high = float(interval[0]), float(interval[1])
                records.append(
                    AggregateRecord(
                        cohort_id=key.fixture_id,
                        challenge_id=key.challenge_id,
                        fixture_id=key.fixture_id,
                        experiment_id=run_id,
                        condition_id=condition,
                        metric_name=metric,
                        n=trials,
                        value=_number(arm.get("rate")),
                        interval_low=low,
                        interval_high=high,
                    )
                )

    paired = summary.get("paired_measures")
    if isinstance(paired, Mapping):
        for metric, payload in paired.items():
            if not isinstance(payload, Mapping):
                continue
            n = payload.get("attempts")
            median = _number(payload.get("median_difference"))
            if not isinstance(n, int) or n < 1 or median is None:
                continue
            verdicts[metric] = {
                "difference": median,
                # Deliberately absent: the summary computes no test for a
                # continuous measure at this N, and inventing one here would
                # smuggle significance into a record that has none.
                "fisher_one_sided_p": None,
            }
            records.append(
                AggregateRecord(
                    cohort_id=key.fixture_id,
                    challenge_id=key.challenge_id,
                    fixture_id=key.fixture_id,
                    experiment_id=run_id,
                    # The value is treatment-minus-control, so it belongs to the
                    # treatment arm rather than to either arm's own level.
                    condition_id=key.treatment_condition,
                    metric_name=f"{metric}_paired_difference",
                    n=n,
                    value=median,
                )
            )
    return records, verdicts


def build_run_records(receipt: Mapping[str, Any]) -> list[RunRecord]:
    """Extract every durable record a run receipt contains.

    A pair, a repeat and a suite each describe one comparison and yield one
    record. A quad describes N comparisons — several graph variants against one
    shared control — and yields one record per variant, because each variant is
    a different question and belongs on its own trend line.

    A suite pools across tasks rather than repetitions, and that is deliberate:
    the claim is about the dataset as a whole ("installing this costs nothing
    measurable across these tasks"), so the dataset is the question and the
    tasks are the sample. Splitting it per task would put twenty lines of one
    attempt each on the chart, which is the least informative possible reading
    of the same data.
    """

    shape = detect_run_shape(receipt)
    status = _require_text(receipt.get("status"), "status")
    if status == "in_progress":
        raise ResultsError("cannot record a run that is still in progress")
    if shape == "quad":
        return _quad_records(receipt)
    return [_single_comparison_record(receipt, shape)]


def build_run_record(receipt: Mapping[str, Any]) -> RunRecord:
    """The sole record of a single-comparison run."""

    records = build_run_records(receipt)
    if len(records) != 1:
        raise ResultsError("this run describes more than one comparison")
    return records[0]


def _single_comparison_record(receipt: Mapping[str, Any], shape: str) -> RunRecord:
    configuration = _require_mapping(receipt.get("configuration"), "configuration")
    provenance = _require_mapping(receipt.get("provenance"), "provenance")
    run_id = _require_text(receipt.get(_RECEIPT_ID_KEYS[shape]), "run id")
    status = str(receipt.get("status"))

    key = ComparabilityKey(
        challenge_id=_require_text(configuration.get("challenge_id"), "challenge_id"),
        fixture_id=_require_text(configuration.get("fixture_id"), "fixture_id"),
        model=_require_text(configuration.get("model"), "model"),
        # Pair predates the condition-pair generalisation and always compared
        # no-stele against faithful.
        control_condition=str(configuration.get("control_condition") or "no-stele"),
        treatment_condition=str(configuration.get("treatment_condition") or "faithful"),
        challenge_manifest_sha256=_require_text(
            provenance.get("challenge_manifest_sha256"), "challenge_manifest_sha256"
        ),
        verifier_sha256=_require_text(provenance.get("verifier_sha256"), "verifier_sha256"),
        source_commit=_require_text(configuration.get("source_commit"), "source_commit"),
    )

    provenance_hashes = {
        "challenge_manifest": key.challenge_manifest_sha256,
        "verifier": key.verifier_sha256,
        "artifact": _require_text(provenance.get("artifact_sha256"), "artifact_sha256"),
    }

    attempts: list[AttemptResultEnvelope] = []
    arms = receipt.get("arms")
    arm_map: Mapping[str, Any] = arms if isinstance(arms, Mapping) else {}
    # A pair has no `attempts` block — it IS one attempt. Normalising it into the
    # same shape means one code path builds envelopes, aggregates and uptake for
    # every run shape, and an N=1 pair lands on the same trend line as the N=5
    # repeat of the same question. Which is the point: seeing the single run's
    # enormous interval next to the repeat's is the clearest possible statement
    # of what one attempt was worth.
    attempt_records = list(receipt.get("attempts") or ())
    if not attempt_records:
        attempt_records = _attempts_from_arms(arm_map)
    for index, record in enumerate(attempt_records):
        if not isinstance(record, Mapping):
            continue
        metrics = _require_mapping(record.get("metrics"), "attempt metrics")
        labels = record.get("labels") if isinstance(record.get("labels"), Mapping) else {}
        outcomes = record.get("outcomes") if isinstance(record.get("outcomes"), Mapping) else {}
        for role in ("control", "treatment"):
            # Receipts written before the condition-pair generalisation name the
            # treatment role "faithful". Reading them is the point of a durable
            # record — a store that can only read receipts written after it
            # exists starts with no history at all.
            arm_metrics = _role_field(metrics, role)
            if not isinstance(arm_metrics, Mapping) or not arm_metrics:
                continue
            arm = arm_map.get(str(_role_field(labels, role) or ""))
            attempts.append(
                attempt_envelope(
                    run_id=run_id,
                    challenge_id=key.challenge_id,
                    fixture_id=key.fixture_id,
                    condition=(
                        key.control_condition if role == "control" else key.treatment_condition
                    ),
                    role=role,
                    attempt_index=index,
                    outcome=_arm_outcome(arm, _role_field(outcomes, role)),
                    metrics=arm_metrics,
                    provenance_hashes=provenance_hashes,
                )
            )

    uptake = _memory_uptake(attempt_records)
    summary = receipt.get("summary")
    if not isinstance(summary, Mapping):
        summary = summarise_attempts(attempt_records)
    aggregates, verdicts = _aggregates_from_summary(summary=summary, key=key, run_id=run_id)
    if shape == "suite":
        verdicts.update(_suite_verdicts(summary))

    return RunRecord(
        run_id=run_id,
        run_shape=shape,  # type: ignore[arg-type]
        status=status,
        recorded_at=_require_text(
            receipt.get("completed_at") or receipt.get("created_at"), "completed_at"
        ),
        comparability=key,
        comparability_digest=key.digest(),
        artifact_sha256=provenance_hashes["artifact"],
        attempts_run=int(summary.get("attempts_run", len(attempt_records))),
        attempts_analysed=int(summary.get("attempts_analysed", 0)),
        attempts=tuple(attempts),
        aggregates=tuple(aggregates),
        verdicts=verdicts,
        memory_uptake=uptake,
    )


def _quad_records(receipt: Mapping[str, Any]) -> list[RunRecord]:
    """One record per graph variant, each paired against the shared control.

    A quad is not one comparison. It is N comparisons that happen to share a
    control arm for honesty and cost, and each variant is a different question —
    correct-anchor and wrong-anchor are asking opposite things. Collapsing them
    into one record would put them on one trend line, where a movement could not
    be attributed to either.
    """

    configuration = _require_mapping(receipt.get("configuration"), "configuration")
    provenance = _require_mapping(receipt.get("provenance"), "provenance")
    run_id = _require_text(receipt.get("quad_id"), "run id")
    status = str(receipt.get("status"))
    arms = _require_mapping(receipt.get("arms"), "arms")
    fixtures = _require_mapping(configuration.get("fixture_ids"), "fixture_ids")
    control_label = str(configuration.get("control_label") or "no-stele")

    challenge_id = _require_text(configuration.get("challenge_id"), "challenge_id")
    model = _require_text(configuration.get("model"), "model")
    source_commit = _require_text(configuration.get("source_commit"), "source_commit")
    artifact = _require_text(provenance.get("artifact_sha256"), "artifact_sha256")
    recorded_at = _require_text(
        receipt.get("completed_at") or receipt.get("created_at"), "completed_at"
    )
    # A quad certifies each variant separately, so its digests are recorded
    # per variant rather than once for the run.
    certification = provenance.get("certification")
    by_variant = (
        certification.get("by_variant") if isinstance(certification, Mapping) else None
    )
    if not isinstance(by_variant, Mapping):
        raise ResultsError("quad receipt has no per-variant certification provenance")

    records: list[RunRecord] = []
    for variant in sorted(label for label in arms if label != control_label):
        fixture_id = fixtures.get(variant)
        variant_provenance = by_variant.get(variant)
        if not isinstance(fixture_id, str) or not isinstance(variant_provenance, Mapping):
            continue
        challenge_digest = _require_text(
            variant_provenance.get("challenge_manifest_sha256"), "challenge_manifest_sha256"
        )
        verifier_digest = _require_text(
            variant_provenance.get("verifier_sha256"), "verifier_sha256"
        )
        provenance_hashes = {
            "challenge_manifest": challenge_digest,
            "verifier": verifier_digest,
            "artifact": artifact,
        }
        key = ComparabilityKey(
            challenge_id=challenge_id,
            fixture_id=fixture_id,
            model=model,
            control_condition=control_label,
            # Every variant runs the faithful condition; what differs between
            # them is the graph they were given, which the fixture id captures.
            treatment_condition="faithful",
            challenge_manifest_sha256=challenge_digest,
            verifier_sha256=verifier_digest,
            source_commit=source_commit,
        )
        attempt_records = _attempts_from_arms(
            arms, control_label=control_label, treatment_label=variant
        )
        if not attempt_records:
            continue
        summary = summarise_attempts(attempt_records)
        aggregates, verdicts = _aggregates_from_summary(
            summary=summary, key=key, run_id=f"{run_id}-{variant}"
        )
        envelopes = [
            attempt_envelope(
                run_id=f"{run_id}-{variant}",
                challenge_id=challenge_id,
                fixture_id=fixture_id,
                condition=key.control_condition if role == "control" else "faithful",
                role=role,
                attempt_index=0,
                outcome=_arm_outcome(
                    arms.get(control_label if role == "control" else variant), None
                ),
                metrics=_role_field(attempt_records[0].get("metrics"), role, {}),
                provenance_hashes=provenance_hashes,
            )
            for role in ("control", "treatment")
        ]
        records.append(
            RunRecord(
                run_id=f"{run_id}-{variant}",
                run_shape="quad",
                status=status,
                recorded_at=recorded_at,
                comparability=key,
                comparability_digest=key.digest(),
                artifact_sha256=artifact,
                attempts_run=summary["attempts_run"],
                attempts_analysed=summary["attempts_analysed"],
                attempts=tuple(envelopes),
                aggregates=tuple(aggregates),
                verdicts=verdicts,
                memory_uptake=_memory_uptake(attempt_records),
            )
        )
    return records


def _attempts_from_arms(
    arm_map: Mapping[str, Any],
    *,
    control_label: str = "control",
    treatment_label: str = "faithful",
) -> list[dict[str, Any]]:
    """Present a single-attempt run in the same shape as a repeat's attempts."""

    control, treatment = arm_map.get(control_label), arm_map.get(treatment_label)
    if not isinstance(control, Mapping) or not isinstance(treatment, Mapping):
        return []
    control_metrics = metrics_module.arm_metrics(control.get("receipt"))
    treatment_metrics = metrics_module.arm_metrics(treatment.get("receipt"))
    if not control_metrics or not treatment_metrics:
        return []
    return [
        {
            "metrics": {"control": control_metrics, "treatment": treatment_metrics},
            "labels": {"control": control_label, "treatment": treatment_label},
            "memory_evidence": {
                "treatment": metrics_module.memory_evidence(treatment.get("receipt"))
            },
            "analysable": control.get("exit_code") == 0 and treatment.get("exit_code") == 0,
        }
    ]


def summarise_attempts(attempt_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute the same summary a repeat reports, for run shapes that lack one.

    Deliberately the same statistics, including at n=1 — a single attempt gets a
    Wilson interval spanning most of the range, which is the honest depiction of
    what one attempt established.
    """

    analysable = [
        record
        for record in attempt_records
        if record.get("analysable", True) and not record.get("excluded_reason")
    ]
    binary: dict[str, Any] = {}
    for measure in metrics_module.BINARY_MEASURES:
        control_values: list[bool] = []
        treatment_values: list[bool] = []
        for record in analysable:
            control = _binary(_role_field(record.get("metrics"), "control"), measure)
            treatment = _binary(_role_field(record.get("metrics"), "treatment"), measure)
            if control is None or treatment is None:
                continue
            control_values.append(control)
            treatment_values.append(treatment)
        binary[measure] = (
            statistics.binary_measure_summary(
                treatment=treatment_values, control=control_values
            )
            if control_values
            else None
        )

    paired: dict[str, Any] = {}
    for measure in metrics_module.PAIRED_MEASURES:
        control_floats: list[float] = []
        treatment_floats: list[float] = []
        for record in analysable:
            control = _number(_role_field(record.get("metrics"), "control", {}).get(measure))
            treatment = _number(
                _role_field(record.get("metrics"), "treatment", {}).get(measure)
            )
            if control is None or treatment is None:
                continue
            control_floats.append(control)
            treatment_floats.append(treatment)
        paired[measure] = statistics.paired_measure_summary(
            treatment=treatment_floats, control=control_floats
        )

    return {
        "attempts_run": len(attempt_records),
        "attempts_analysed": len(analysable),
        "binary_measures": binary,
        "paired_measures": paired,
    }


def _binary(metrics: Any, measure: str) -> bool | None:
    """A binary measure counts as a success only at a full 1.0.

    `bool(0.6)` is `True`, so a partial reward would otherwise be scored as a
    pass — the exact mistake the repeat summary guards against.
    """

    if not isinstance(metrics, Mapping):
        return None
    value = _number(metrics.get(measure))
    return None if value is None else value >= 1.0


def _memory_uptake(
    attempt_records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    """Count, per role, attempts vs injection detected vs marker scored."""

    uptake: dict[str, dict[str, int]] = {}
    for record in attempt_records:
        if not isinstance(record, Mapping):
            continue
        evidence = record.get("memory_evidence")
        metrics = record.get("metrics")
        for role in ("control", "treatment"):
            arm_metrics = _role_field(metrics, role)
            if not isinstance(arm_metrics, Mapping) or not arm_metrics:
                continue
            # Earlier receipts stored a single evidence object for the treatment
            # arm rather than a per-role mapping.
            if isinstance(evidence, Mapping) and (
                set(evidence) & {"control", "treatment", "faithful"}
            ):
                arm_evidence = _role_field(evidence, role)
            else:
                arm_evidence = evidence if role == "treatment" else None
            counts = uptake.setdefault(role, {"attempts": 0, "injected": 0, "marker": 0})
            counts["attempts"] += 1
            if isinstance(arm_evidence, Mapping) and arm_evidence.get("injected") is True:
                counts["injected"] += 1
            if _number(arm_metrics.get("cited_decision")) == 1.0:
                counts["marker"] += 1
    return uptake


def record_filename(record: RunRecord) -> str:
    """Stable, sortable filename: the run is unique, the date makes it browseable."""

    return f"{record.recorded_at[:10]}-{record.run_id}.json"


def serialise_record(record: RunRecord) -> str:
    return json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def load_record(payload: str) -> RunRecord:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ResultsError("result record is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ResultsError("result record must be an object")
    return RunRecord.model_validate(parsed)


def group_by_comparability(
    records: Sequence[RunRecord],
) -> dict[str, list[RunRecord]]:
    """Group records into trend lines, oldest first within each line.

    Two runs land on the same line only if every keyed input matches. This is
    the guard the whole viewer rests on: a changed verifier moves the score
    without anything about memory changing, and plotting those together
    manufactures a regression that never happened.
    """

    grouped: dict[str, list[RunRecord]] = {}
    for record in records:
        grouped.setdefault(record.comparability_digest, []).append(record)
    for line in grouped.values():
        line.sort(key=lambda record: (record.recorded_at, record.run_id))
    return grouped
