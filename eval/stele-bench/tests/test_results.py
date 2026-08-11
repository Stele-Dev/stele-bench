"""Durable result records: what they keep, and what they refuse to conflate."""

from __future__ import annotations

import json
from typing import Any

import pytest

from stele_bench import report, results

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
ARTIFACT_1 = "1" * 64
ARTIFACT_2 = "2" * 64


def _arm_receipt(
    *,
    reward: float = 1.0,
    cited: bool = False,
    tokens_input: int = 1000,
    outcome: str = "passed",
    wall: float = 100.0,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "results": [
            {
                "outcome": outcome,
                "reward": reward,
                "tokens": {"input": tokens_input, "output": 200},
                "cost": {"claude_reported_amount_usd": 0.25},
                "timings": {"invocation_wall": wall, "agent_execution": wall - 5},
                "evidence": {
                    "response_evidence": {"cited": cited},
                    "investigation_depth": {
                        "investigative": 3,
                        "total": 5,
                        "session": {"investigative": 8, "total": 12},
                    },
                },
                "memory_evidence": {"injected": True, "channels": ["auto-rag"]},
            }
        ],
    }


def _repeat_receipt(
    *,
    run_id: str = "run-001",
    treatment_cited: tuple[bool, ...] = (True, True, False),
    control_cited: tuple[bool, ...] = (False, False, False),
    verifier: str = DIGEST_B,
    artifact: str = ARTIFACT_1,
    completed_at: str = "2026-07-24T10:00:00Z",
) -> dict[str, Any]:
    arms: dict[str, Any] = {}
    attempts: list[dict[str, Any]] = []
    for index, (treatment, control) in enumerate(
        zip(treatment_cited, control_cited, strict=True), start=1
    ):
        labels = {
            "control": f"attempt-{index:02d}-control",
            "treatment": f"attempt-{index:02d}-treatment",
        }
        arms[labels["control"]] = {
            "exit_code": 0,
            "receipt": _arm_receipt(cited=control, tokens_input=1000),
        }
        arms[labels["treatment"]] = {
            "exit_code": 0,
            "receipt": _arm_receipt(cited=treatment, tokens_input=1800),
        }
        attempts.append(
            {
                "attempt": index,
                "labels": labels,
                "analysable": True,
                "metrics": {
                    "control": {
                        "cited_decision": float(control),
                        "reward": 1.0,
                        "tokens_input": 1000.0,
                        "trial_wall_seconds": 100.0,
                        "investigative_tool_calls": 3.0,
                        "session_investigative_tool_calls": 8.0,
                    },
                    "treatment": {
                        "cited_decision": float(treatment),
                        "reward": 1.0,
                        "tokens_input": 1800.0,
                        "trial_wall_seconds": 140.0,
                        "investigative_tool_calls": 3.0,
                        "session_investigative_tool_calls": 10.0,
                    },
                },
                "outcomes": {"control": "passed", "treatment": "passed"},
                "memory_evidence": {
                    "treatment": {"injected": True},
                    "control": {"injected": False},
                },
            }
        )
    trials = len(treatment_cited)
    return {
        "schema_version": 1,
        "repeat_id": run_id,
        "status": "completed",
        "created_at": completed_at,
        "completed_at": completed_at,
        "configuration": {
            "challenge_id": "example-challenge",
            "fixture_id": "example-fixture-v1",
            "model": "anthropic/claude-haiku-4.5",
            "source_commit": "c" * 40,
            "control_condition": "no-stele",
            "treatment_condition": "faithful",
        },
        "provenance": {
            "artifact_sha256": artifact,
            "challenge_manifest_sha256": DIGEST_A,
            "verifier_sha256": verifier,
        },
        "arms": arms,
        "attempts": attempts,
        "summary": {
            "attempts_run": trials,
            "attempts_analysed": trials,
            "binary_measures": {
                "cited_decision": {
                    "treatment": {
                        "successes": sum(treatment_cited),
                        "trials": trials,
                        "rate": sum(treatment_cited) / trials,
                        "wilson_95": [0.1, 0.9],
                    },
                    "control": {
                        "successes": sum(control_cited),
                        "trials": trials,
                        "rate": sum(control_cited) / trials,
                        "wilson_95": [0.0, 0.5],
                    },
                    "difference": (sum(treatment_cited) - sum(control_cited)) / trials,
                    "fisher_one_sided_p": 0.1,
                }
            },
            "paired_measures": {
                "tokens_input": {
                    "attempts": trials,
                    "median_difference": 800.0,
                }
            },
        },
    }


def test_a_record_keeps_both_arms_of_every_attempt() -> None:
    record = results.build_run_record(_repeat_receipt())

    assert len(record.attempts) == 6
    assert {envelope.condition_id for envelope in record.attempts} == {
        "no-stele",
        "faithful",
    }
    # Condition kind and graph arm have to agree or the contract rejects it —
    # this is what keeps a no-Stele arm from being recorded as a graph arm.
    graph = [e for e in record.attempts if e.condition_id == "faithful"]
    assert all(e.graph_arm is not None for e in graph)
    assert all(e.graph_arm is None for e in record.attempts if e.condition_id == "no-stele")


def test_a_record_carries_the_interval_not_just_the_rate() -> None:
    record = results.build_run_record(_repeat_receipt())

    cited = [
        aggregate
        for aggregate in record.aggregates
        if aggregate.metric_name == "cited_decision"
    ]
    assert {aggregate.condition_id for aggregate in cited} == {"no-stele", "faithful"}
    assert all(
        aggregate.interval_low is not None and aggregate.interval_high is not None
        for aggregate in cited
    )
    assert record.verdicts["cited_decision"]["fisher_one_sided_p"] == 0.1


def test_a_record_separates_injection_from_uptake() -> None:
    # Two of three treatment attempts cited; all three had the memory injected.
    record = results.build_run_record(_repeat_receipt())

    assert record.memory_uptake["treatment"] == {
        "attempts": 3,
        "injected": 3,
        "marker": 2,
    }
    # Without this split, "3 injected, 2 scored" is indistinguishable from
    # "2 injected, 2 scored" — a retrieval regression looks like an uptake one.
    assert record.memory_uptake["control"]["injected"] == 0


def test_an_unfinished_run_is_not_a_result() -> None:
    receipt = _repeat_receipt()
    receipt["status"] = "in_progress"

    with pytest.raises(results.ResultsError, match="still in progress"):
        results.build_run_record(receipt)


def test_an_arm_whose_command_failed_is_infrastructure_not_a_score() -> None:
    receipt = _repeat_receipt()
    receipt["arms"]["attempt-01-control"]["exit_code"] = 1

    record = results.build_run_record(receipt)

    failed = next(
        envelope
        for envelope in record.attempts
        if envelope.attempt_id.endswith("attempt-01-control")
    )
    # Recording this as `failed` would put a container problem into the score.
    assert failed.outcome.value == "infrastructure-error"


def test_records_of_the_same_question_share_a_trend_line() -> None:
    first = results.build_run_record(_repeat_receipt(run_id="run-001"))
    second = results.build_run_record(
        _repeat_receipt(run_id="run-002", completed_at="2026-07-25T10:00:00Z")
    )

    assert first.comparability_digest == second.comparability_digest
    grouped = results.group_by_comparability([second, first])
    assert len(grouped) == 1
    # Oldest first, so a trend reads left to right.
    assert [record.run_id for record in next(iter(grouped.values()))] == [
        "run-001",
        "run-002",
    ]


def test_a_changed_verifier_breaks_the_trend_line() -> None:
    # The whole point of the key: a different verifier moves the score without
    # anything about memory changing, so plotting them together would
    # manufacture a regression.
    first = results.build_run_record(_repeat_receipt(run_id="run-001"))
    second = results.build_run_record(_repeat_receipt(run_id="run-002", verifier=DIGEST_A))

    assert first.comparability_digest != second.comparability_digest
    assert len(results.group_by_comparability([first, second])) == 2


def test_a_different_build_stays_on_the_same_trend_line() -> None:
    # The artifact is the thing under test. Keying on it would give every build
    # its own island and switch regression detection off exactly when a build
    # changed something.
    first = results.build_run_record(_repeat_receipt(run_id="run-001"))
    second = results.build_run_record(_repeat_receipt(run_id="run-002", artifact=ARTIFACT_2))

    assert first.comparability_digest == second.comparability_digest
    assert first.artifact_sha256 != second.artifact_sha256


def test_a_record_round_trips_through_disk() -> None:
    record = results.build_run_record(_repeat_receipt())

    assert results.load_record(results.serialise_record(record)) == record


def test_a_tampered_record_is_rejected() -> None:
    record = results.build_run_record(_repeat_receipt())
    payload = json.loads(results.serialise_record(record))
    payload["comparability"]["model"] = "some-other-model"

    # The digest is what the viewer groups on; letting the key and the digest
    # disagree would silently merge two different questions onto one line.
    with pytest.raises(ValueError, match="comparability_digest"):
        results.load_record(json.dumps(payload))


def test_a_single_attempt_run_gets_real_aggregates() -> None:
    """A pair has no summary block; it is still a measurement with an interval."""

    pair = {
        "schema_version": 1,
        "pair_id": "pair-001",
        "status": "completed",
        "completed_at": "2026-07-20T10:00:00Z",
        "configuration": {
            "challenge_id": "example-challenge",
            "fixture_id": "example-fixture-v1",
            "model": "anthropic/claude-haiku-4.5",
            "source_commit": "c" * 40,
        },
        "provenance": {
            "artifact_sha256": ARTIFACT_1,
            "challenge_manifest_sha256": DIGEST_A,
            "verifier_sha256": DIGEST_B,
        },
        "arms": {
            "control": {"exit_code": 0, "receipt": _arm_receipt(cited=False)},
            "faithful": {"exit_code": 0, "receipt": _arm_receipt(cited=True)},
        },
    }

    record = results.build_run_record(pair)

    assert record.run_shape == "pair"
    assert len(record.attempts) == 2
    cited = next(
        aggregate
        for aggregate in record.aggregates
        if aggregate.metric_name == "cited_decision"
        and aggregate.condition_id == "faithful"
    )
    assert cited.value == 1.0
    # 1/1 with a Wilson interval spanning most of the range — the honest
    # depiction of what a single attempt established, and the reason a pair and
    # its later repeat belong on the same line.
    assert cited.interval_low < 0.4
    assert cited.interval_high == pytest.approx(1.0)


def test_a_partial_reward_is_not_a_success() -> None:
    pair = {
        "schema_version": 1,
        "pair_id": "pair-002",
        "status": "completed",
        "completed_at": "2026-07-20T10:00:00Z",
        "configuration": {
            "challenge_id": "example-challenge",
            "fixture_id": "example-fixture-v1",
            "model": "anthropic/claude-haiku-4.5",
            "source_commit": "c" * 40,
        },
        "provenance": {
            "artifact_sha256": ARTIFACT_1,
            "challenge_manifest_sha256": DIGEST_A,
            "verifier_sha256": DIGEST_B,
        },
        "arms": {
            "control": {"exit_code": 0, "receipt": _arm_receipt(reward=0.0)},
            # `bool(0.6)` is True — a partial reward must not count as a pass.
            "faithful": {"exit_code": 0, "receipt": _arm_receipt(reward=0.6)},
        },
    }

    record = results.build_run_record(pair)

    reward = next(
        aggregate
        for aggregate in record.aggregates
        if aggregate.metric_name == "reward" and aggregate.condition_id == "faithful"
    )
    assert reward.value == 0.0


def test_a_quad_becomes_one_record_per_variant() -> None:
    """Variants ask opposite questions and must not share a trend line."""

    arms = {
        "no-stele": {"exit_code": 0, "receipt": _arm_receipt(cited=False)},
        "correct-anchor": {"exit_code": 0, "receipt": _arm_receipt(cited=True)},
        "wrong-anchor": {"exit_code": 0, "receipt": _arm_receipt(cited=False)},
    }
    quad = {
        "schema_version": 1,
        "quad_id": "quad-001",
        "status": "completed",
        "completed_at": "2026-07-21T10:00:00Z",
        "configuration": {
            "challenge_id": "example-challenge",
            "model": "anthropic/claude-haiku-4.5",
            "source_commit": "c" * 40,
            "control_label": "no-stele",
            "fixture_ids": {
                "correct-anchor": "correct-anchor-v1",
                "wrong-anchor": "wrong-anchor-v1",
            },
        },
        "provenance": {
            "artifact_sha256": ARTIFACT_1,
            "certification": {
                "by_variant": {
                    "correct-anchor": {
                        "challenge_manifest_sha256": DIGEST_A,
                        "verifier_sha256": DIGEST_B,
                    },
                    "wrong-anchor": {
                        "challenge_manifest_sha256": DIGEST_A,
                        "verifier_sha256": DIGEST_B,
                    },
                }
            },
        },
        "arms": arms,
    }

    records = results.build_run_records(quad)

    assert [record.run_id for record in records] == [
        "quad-001-correct-anchor",
        "quad-001-wrong-anchor",
    ]
    assert len({record.comparability_digest for record in records}) == 2
    # Each variant is compared against the SAME shared control arm.
    for record in records:
        assert {envelope.condition_id for envelope in record.attempts} == {
            "no-stele",
            "faithful",
        }


def test_the_view_refuses_to_plot_incomparable_runs_together() -> None:
    first = results.build_run_record(_repeat_receipt(run_id="run-001"))
    second = results.build_run_record(_repeat_receipt(run_id="run-002", verifier=DIGEST_A))

    page = report.render_report([first, second], generated_at="2026-07-25T00:00:00Z")

    assert "2 comparable line(s)" in page
    assert "Not comparable" in page
    # And it names the field that differs, or the split is just confusing.
    assert "verifier_sha256" in page


def test_the_view_renders_without_any_records() -> None:
    page = report.render_report([], generated_at="2026-07-25T00:00:00Z")

    assert "No result records yet" in page
    assert "<svg" not in page


def test_the_view_lists_excluded_runs_without_charting_them() -> None:
    payload = results.build_run_record(_repeat_receipt()).model_dump(mode="json")
    payload["publication"] = {
        "decision": "excluded",
        "reason_code": "apparatus-defect",
        "reason": "The verifier rejected an allowed output path.",
    }
    record = results.RunRecord.model_validate(payload)

    page = report.render_report([record], generated_at="2026-08-11T00:00:00Z")

    assert "0 admitted" in page
    assert "Excluded runs" in page
    assert "apparatus-defect" in page
    assert "The verifier rejected an allowed output path." in page
    assert '<section class="line">' not in page


def test_the_view_is_self_contained() -> None:
    record = results.build_run_record(_repeat_receipt())

    page = report.render_report([record], generated_at="2026-07-25T00:00:00Z")

    # No scripts, no network. It has to open from disk in five years.
    assert "<script" not in page
    assert "http://" not in page and "https://" not in page
    assert "<svg" in page


def test_the_view_marks_a_single_run_as_not_yet_a_trend() -> None:
    record = results.build_run_record(_repeat_receipt())

    page = report.render_report([record], generated_at="2026-07-25T00:00:00Z")

    assert "not a trend" in page


def test_the_view_picks_a_metric_the_challenge_actually_reports() -> None:
    receipt = _repeat_receipt()
    # A workspace-graded challenge never sets `cited_decision`.
    del receipt["summary"]["binary_measures"]["cited_decision"]
    receipt["summary"]["binary_measures"]["reward"] = {
        "treatment": {"successes": 3, "trials": 3, "rate": 1.0, "wilson_95": [0.4, 1.0]},
        "control": {"successes": 3, "trials": 3, "rate": 1.0, "wilson_95": [0.4, 1.0]},
        "difference": 0.0,
        "fisher_one_sided_p": 1.0,
    }
    record = results.build_run_record(receipt)

    page = report.render_report([record], generated_at="2026-07-25T00:00:00Z")

    assert "reward (treatment)" in page


def _suite_receipt(
    *,
    run_id: str = "suite-001",
    treatment_solved: tuple[bool, ...] = (True, True, False, False),
    control_solved: tuple[bool, ...] = (True, True, False, False),
    margin: float = 0.10,
    verdict: str = "inconclusive",
    difference_95: tuple[float, float] = (-0.35, 0.35),
) -> dict[str, Any]:
    """A borrowed-task suite receipt, as the oracle's `run-suite` writes it."""

    arms: dict[str, Any] = {}
    attempts: list[dict[str, Any]] = []
    for index, (treatment, control) in enumerate(
        zip(treatment_solved, control_solved, strict=True), start=1
    ):
        labels = {
            "control": f"task-{index:02d}-control",
            "treatment": f"task-{index:02d}-treatment",
        }
        arms[labels["control"]] = {
            "exit_code": 0,
            "receipt": _arm_receipt(reward=float(control), outcome="passed"),
        }
        arms[labels["treatment"]] = {
            "exit_code": 0,
            "receipt": _arm_receipt(reward=float(treatment), outcome="passed"),
        }
        attempts.append(
            {
                "attempt": index,
                "task": f"task-{index:02d}",
                "labels": labels,
                "analysable": True,
                "metrics": {
                    "control": {
                        "reward": float(control),
                        "tokens_input": 1000.0,
                        "trial_wall_seconds": 100.0,
                    },
                    "treatment": {
                        "reward": float(treatment),
                        "tokens_input": 1400.0,
                        "trial_wall_seconds": 130.0,
                    },
                },
                "outcomes": {"control": "passed", "treatment": "passed"},
            }
        )
    trials = len(treatment_solved)
    treatment_rate = sum(treatment_solved) / trials
    control_rate = sum(control_solved) / trials
    return {
        "schema_version": 1,
        "suite_id": run_id,
        "status": "completed",
        "created_at": "2026-07-25T10:00:00Z",
        "completed_at": "2026-07-25T10:00:00Z",
        "configuration": {
            "challenge_id": "aider-polyglot-clean",
            "fixture_id": "created-per-task",
            "model": "anthropic/claude-haiku-4.5",
            "source_commit": "c" * 40,
            "control_condition": "no-stele",
            "treatment_condition": "stele-fresh",
            "non_inferiority_margin": margin,
        },
        "provenance": {
            "artifact_sha256": ARTIFACT_1,
            "challenge_manifest_sha256": DIGEST_A,
            "verifier_sha256": DIGEST_B,
        },
        "arms": arms,
        "attempts": attempts,
        "summary": {
            "attempts_run": trials,
            "attempts_analysed": trials,
            "sample_unit": "task",
            "binary_measures": {
                "reward": {
                    "treatment": {
                        "successes": sum(treatment_solved),
                        "trials": trials,
                        "rate": treatment_rate,
                        "wilson_95": [0.2, 0.8],
                    },
                    "control": {
                        "successes": sum(control_solved),
                        "trials": trials,
                        "rate": control_rate,
                        "wilson_95": [0.2, 0.8],
                    },
                    "difference": treatment_rate - control_rate,
                    "fisher_one_sided_p": 1.0,
                }
            },
            "paired_measures": {},
            "solve_rate": {
                "pairs": trials,
                "treatment_rate": treatment_rate,
                "control_rate": control_rate,
                "difference": treatment_rate - control_rate,
                "difference_95": list(difference_95),
            },
            "non_inferiority": {
                "margin": margin,
                "verdict": verdict,
                "difference": treatment_rate - control_rate,
                "difference_95": list(difference_95),
                "pairs": trials,
            },
        },
    }


def test_a_suite_is_one_record_because_the_dataset_is_the_question() -> None:
    """Not one line per task — the tasks are the sample, not separate questions."""

    records = results.build_run_records(_suite_receipt())
    assert len(records) == 1
    record = records[0]
    assert record.run_shape == "suite"
    assert record.comparability.challenge_id == "aider-polyglot-clean"
    assert record.comparability.treatment_condition == "stele-fresh"
    assert record.attempts_run == 4
    assert record.attempts_analysed == 4
    assert len(record.attempts) == 8


def test_a_suite_record_carries_its_non_inferiority_verdict_with_the_margin() -> None:
    """A bound means nothing without the threshold it was compared against."""

    record = results.build_run_record(_suite_receipt(verdict="non_inferior"))
    verdict = record.verdicts["solve_rate_non_inferiority"]
    assert verdict["margin"] == 0.10
    assert verdict["verdict"] == 1.0
    assert verdict["interval_low"] == -0.35


def test_a_suite_that_regressed_records_a_negative_verdict() -> None:
    record = results.build_run_record(
        _suite_receipt(
            treatment_solved=(False, False, False, False),
            control_solved=(True, True, True, True),
            verdict="inferior",
            difference_95=(-1.0, -0.4),
        )
    )
    verdict = record.verdicts["solve_rate_non_inferiority"]
    assert verdict["verdict"] == -1.0
    assert verdict["difference"] == -1.0


def test_a_suite_stays_on_its_own_line_when_the_borrowed_verifier_changes() -> None:
    """The borrowed tests are upstream's; a change there must break the line."""

    first = results.build_run_record(_suite_receipt(run_id="suite-001"))
    changed = _suite_receipt(run_id="suite-002")
    changed["provenance"]["verifier_sha256"] = "f" * 64
    second = results.build_run_record(changed)

    assert first.comparability_digest != second.comparability_digest
    grouped = results.group_by_comparability([first, second])
    assert len(grouped) == 2


def test_a_suite_on_a_new_build_stays_on_the_same_line() -> None:
    """The artifact is the variable under test, not part of the question."""

    first = results.build_run_record(_suite_receipt(run_id="suite-001"))
    rebuilt = _suite_receipt(run_id="suite-002")
    rebuilt["provenance"]["artifact_sha256"] = ARTIFACT_2
    second = results.build_run_record(rebuilt)

    assert first.comparability_digest == second.comparability_digest
    assert results.group_by_comparability([first, second]).popitem()[1] == [first, second]


def test_a_suite_record_round_trips_and_renders() -> None:
    record = results.build_run_record(_suite_receipt())
    restored = results.load_record(results.serialise_record(record))
    assert restored == record
    page = report.render_report([record], generated_at="2026-07-25T00:00:00Z")
    assert "aider-polyglot-clean" in page
    assert "created-per-task" in page


def test_the_report_states_a_no_harm_claim_with_its_margin() -> None:
    record = results.build_run_record(_suite_receipt(verdict="non_inferior"))
    page = report.render_report([record], generated_at="2026-07-25T00:00:00Z")

    assert "non-inferior" in page
    # The bound and the threshold it cleared, not just the word.
    assert "[-0.35, +0.35]" in page or "[−0.35, +0.35]" in page
    assert "0.10" in page


def test_the_report_does_not_dress_an_inconclusive_run_as_a_pass() -> None:
    """The failure this styling exists to prevent."""

    record = results.build_run_record(_suite_receipt(verdict="inconclusive"))
    page = report.render_report([record], generated_at="2026-07-25T00:00:00Z")

    assert '<td class="verdict-inconclusive">inconclusive</td>' in page
    assert '<td class="verdict-non-inferior">' not in page
    assert "could not rule out" in page


def test_a_run_with_no_no_harm_claim_renders_no_verdict_table() -> None:
    """A repeat makes no non-inferiority claim, so none is shown."""

    record = results.build_run_record(_repeat_receipt())
    page = report.render_report([record], generated_at="2026-07-25T00:00:00Z")
    assert "<td class=\"verdict-" not in page
    assert "could not rule out" not in page
