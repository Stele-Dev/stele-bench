from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any

import oracle
import pytest


def _session(model: str = "claude-sonnet-4-6") -> dict[str, object]:
    return {
        "resolved_model": model,
        "challenge": {"outcome": "passed"},
        "gateway_billing": {"delta_usd": "0.25"},
    }


def _chain(
    *,
    gateway_cost: float,
    reward: float = 1.0,
    session_1_correct: bool = True,
    relevant: bool = True,
) -> dict[str, object]:
    return {
        "outcome": "completed",
        "cleanup_completed": True,
        "boundary": {"only_graph_crossed_boundary": True},
        "session_1": _session(),
        "session_2": _session(),
        "session_1_correct": session_1_correct,
        "session_2_reward": reward,
        "cumulative": {
            "gateway_observed_usd": gateway_cost,
            "claude_reported_usd": gateway_cost + 0.1,
            "outer_harbor_wall_seconds": 20.0,
        },
        "first_live_edit": {
            "tool_call_index": 3,
            "investigative_calls_before": 1,
        },
        "graph_intake": {
            "written": relevant,
            "relevant": relevant,
            "deliverable": relevant,
        },
    }


def test_compounding_budget_is_exactly_four_sessions_per_pair() -> None:
    budget, cap = oracle._validate_compounding_budget("10.00", "40.00", 1)
    assert str(budget) == "10.00"
    assert str(cap) == "40.00"

    with pytest.raises(oracle.OracleError, match="exact requested cap"):
        oracle._validate_compounding_budget("10.00", "20.00", 1)
    with pytest.raises(oracle.OracleError, match=r"exactly \$10.00"):
        oracle._validate_compounding_budget("10.01", "40.04", 1)
    with pytest.raises(oracle.OracleError, match=r"exactly \$10.00"):
        oracle._validate_compounding_budget("9.99", "39.96", 1)


def test_admission_requires_correct_intake_answers_and_settled_gateway_cost() -> None:
    control = _chain(gateway_cost=0.5)
    treatment = _chain(gateway_cost=0.6)
    assert oracle._compounding_pair_valid(control, treatment, admission=True) is None

    wrong = _chain(gateway_cost=0.5, session_1_correct=False)
    assert (
        oracle._compounding_pair_valid(wrong, treatment, admission=True)
        == "control_session_1_answer_incorrect"
    )
    unsettled = _chain(gateway_cost=0.0)
    assert (
        oracle._compounding_pair_valid(unsettled, treatment, admission=True)
        == "control_gateway_cost_did_not_settle"
    )


def test_summary_applies_the_registered_first_reuse_payback_rule() -> None:
    attempts = [
        {
            "attempt": index,
            "analysable": True,
            "receipts": {
                "control": _chain(gateway_cost=1.0),
                "treatment": _chain(gateway_cost=0.8),
            },
        }
        for index in range(1, 11)
    ]

    summary = oracle._compounding_summary(attempts)

    assert summary["attempts_analysed"] == 10
    assert summary["session_2_reward"]["control"]["rate"] == 1.0
    assert summary["session_2_reward"]["treatment"]["rate"] == 1.0
    payback = summary["payback_by_first_reuse"]
    assert payback == {
        "observed": True,
        "established": True,
        "treatment_reward_not_lower": True,
        "verdict": "established",
    }
    gateway = summary["paired_measures"]["cumulative_gateway_observed_usd"]
    assert gateway["mean_difference"] == pytest.approx(-0.2)
    assert gateway["paired_bootstrap_mean_difference_95"] == pytest.approx([-0.2, -0.2])
    assert summary["graph_intake"]["relevant"] == {"successes": 10, "trials": 10}


def test_summary_does_not_call_higher_cumulative_cost_payback() -> None:
    attempts = [
        {
            "attempt": 1,
            "analysable": True,
            "receipts": {
                "control": _chain(gateway_cost=1.0),
                "treatment": _chain(gateway_cost=1.2),
            },
        }
    ]

    assert oracle._compounding_summary(attempts)["payback_by_first_reuse"] == {
        "observed": False,
        "established": False,
        "treatment_reward_not_lower": True,
        "verdict": "not_established_by_first_reuse",
    }


def test_chain_invocation_passes_stele_inputs_only_to_the_treatment(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: Any, **_kwargs: Any) -> oracle.CommandResult:
        args = list(argv)
        calls.append(args)
        receipt_out = Path(args[args.index("--receipt-out") + 1])
        receipt_out.parent.mkdir(parents=True, exist_ok=True)
        receipt_out.write_text(
            json.dumps({"ok": True, "receipt": {"outcome": "completed"}}),
            encoding="utf-8",
        )
        return oracle.CommandResult(0, "", "")

    common = {
        "run_id": "chain-1",
        "source": tmp_path,
        "revision": "a" * 40,
        "artifact": {"path": "/artifact", "sha256": "b" * 64},
        "backend_url": "http://127.0.0.1:54321",
        "backend_key": "anon",
        "model": oracle.DEFAULT_COMPOUNDING_MODEL,
        "max_turns": 45,
        "max_budget_usd": "10.00",
        "timeout_seconds": 900.0,
        "attempts": tmp_path / "attempts",
        "work": tmp_path / "work",
        "gateway_reserve_config": tmp_path / "reserve.json",
        "runner": runner,
    }
    oracle._invoke_compounding_chain(
        condition="no-stele",
        receipt_path=tmp_path / "control.json",
        **common,
    )
    oracle._invoke_compounding_chain(
        condition="stele-fresh",
        receipt_path=tmp_path / "treatment.json",
        **common,
    )

    assert "--artifact" not in calls[0]
    assert "--backend-url" not in calls[0]
    assert "--artifact" in calls[1]
    assert "--backend-url" in calls[1]


def test_registered_attempt_counts_cannot_be_tuned(tmp_path: Path) -> None:
    with pytest.raises(oracle.OracleError, match="exactly one pair"):
        oracle.run_compounding(
            run_id="bad-admission",
            admission=True,
            attempts=2,
            acknowledge_paid_cap_usd="80.00",
            state_root=tmp_path,
        )
    with pytest.raises(oracle.OracleError, match="exactly ten pairs"):
        oracle.run_compounding(
            run_id="bad-cohort",
            admission=False,
            attempts=9,
            acknowledge_paid_cap_usd="360.00",
            state_root=tmp_path,
        )


def _fake_batch_runner(
    *, invalid_attempt: int | None = None, gateway_cost: float = 1.8
):
    def run(**kwargs: Any) -> dict[str, Any]:
        index = kwargs["cohort_attempt_index"]
        state_root = Path(kwargs["state_root"])
        run_id = kwargs["run_id"]
        control = _chain(gateway_cost=gateway_cost / 2)
        treatment = _chain(gateway_cost=gateway_cost / 2)
        excluded = "chain_did_not_complete" if index == invalid_attempt else None
        if excluded is not None:
            treatment = _chain(gateway_cost=gateway_cost / 2)
            treatment["outcome"] = "invalid"
        attempt = {
            "attempt": index,
            "arm_order": kwargs["cohort_arm_order"],
            "receipts": {"control": control, "treatment": treatment},
            "analysable": excluded is None,
            "excluded_reason": excluded,
        }
        receipt = {
            "schema_version": 1,
            "run_id": run_id,
            "classification": "registered-cohort-batch",
            "status": "completed",
            "configuration": {"cohort_attempt_index": index},
            "arms": {
                f"attempt-{index:02d}-control": {"receipt": control},
                f"attempt-{index:02d}-treatment": {"receipt": treatment},
            },
            "attempts": [attempt],
        }
        path = state_root / "runs" / run_id / "compounding.json"
        oracle._atomic_json(path, receipt)
        return {"ok": True, "compounding_receipt": str(path), "receipt": receipt}

    return run


def _run_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index: int,
    *,
    invalid_attempt: int | None = None,
    gateway_cost: float = 1.8,
    **overrides: Any,
) -> dict[str, Any]:
    monkeypatch.setattr(
        oracle,
        "run_compounding",
        _fake_batch_runner(
            invalid_attempt=invalid_attempt,
            gateway_cost=gateway_cost,
        ),
    )
    preregistration = tmp_path / "preregistration.md"
    preregistration.write_text("frozen v4", encoding="utf-8")
    monkeypatch.setattr(oracle, "DEFAULT_COMPOUNDING_PREREGISTRATION", preregistration)
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}", encoding="utf-8")
    artifact_manifest = tmp_path / "artifact.json"
    artifact_manifest.write_text("{}", encoding="utf-8")
    return oracle.run_compounding_batch(
        cohort_id="cohort-v4",
        attempt_index=index,
        certification_fixture=fixture,
        artifact_manifest=artifact_manifest,
        acknowledge_paid_cap_usd="40.00",
        state_root=tmp_path / "state",
        **overrides,
    )


def test_retired_v4_is_blocked_before_a_paid_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preregistration = tmp_path / "profile-selection-compounding-v4.md"
    preregistration.write_text("retired", encoding="utf-8")
    monkeypatch.setattr(oracle, "DEFAULT_COMPOUNDING_PREREGISTRATION", preregistration)
    with pytest.raises(oracle.OracleError, match="retired after admission 003 audit"):
        oracle.run_compounding_batch(
            cohort_id="retired-v4",
            attempt_index=1,
            acknowledge_paid_cap_usd="40.00",
            state_root=tmp_path / "state",
        )


def test_batched_cohort_freezes_all_identities_and_only_runs_the_next_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_batch(tmp_path, monkeypatch, 1)
    ledger = oracle._read_json(Path(first["cohort_ledger"]))

    assert first["progress"] == "1/10"
    assert first["batch"]["gateway_observed_usd"] == "1.8"
    assert ledger["summary"] is None
    assert ledger["cumulative_gateway_observed_usd"] == "1.8"
    assert ledger["next_attempt_index"] == 2
    assert [item["attempt"] for item in ledger["planned_attempts"]] == list(range(1, 11))
    assert ledger["planned_attempts"][0]["arm_order"] == ["control", "treatment"]
    assert ledger["planned_attempts"][1]["arm_order"] == ["treatment", "control"]
    assert ledger["planned_attempts"][0]["execution_state"] == "consumed"
    assert ledger["planned_attempts"][1]["execution_state"] == "pending"

    with pytest.raises(oracle.OracleError, match="already has an immutable receipt"):
        _run_batch(tmp_path, monkeypatch, 1)
    with pytest.raises(oracle.OracleError, match="frozen attempt order"):
        _run_batch(tmp_path, monkeypatch, 3)


def test_batched_cohort_resume_consumes_the_persisted_arm_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_batch(tmp_path, monkeypatch, 1)
    ledger_path = Path(first["cohort_ledger"])
    ledger = oracle._read_json(ledger_path)
    assert ledger["planned_attempts"][1]["arm_order"] == ["treatment", "control"]

    def refuse_recomputation(_index: int) -> list[str]:
        raise AssertionError("resume recomputed a frozen assignment")

    monkeypatch.setattr(oracle, "_compounding_batch_order", refuse_recomputation)
    second = _run_batch(tmp_path, monkeypatch, 2)
    receipt = oracle._read_json(Path(second["batch"]["receipt_path"]))
    assert receipt["attempts"][0]["arm_order"] == ["treatment", "control"]


def test_batched_cohort_rejects_changes_to_the_frozen_pair_assignments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_batch(tmp_path, monkeypatch, 1)
    ledger_path = Path(first["cohort_ledger"])
    ledger = oracle._read_json(ledger_path)
    ledger["planned_attempts"][1]["arm_order"] = ["control", "treatment"]
    oracle._atomic_json(ledger_path, ledger)

    with pytest.raises(oracle.OracleError, match="frozen pair assignments changed"):
        _run_batch(tmp_path, monkeypatch, 2)


def test_batched_cohort_refuses_to_start_late_or_change_resume_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(oracle.OracleError, match="start with attempt 1"):
        _run_batch(tmp_path, monkeypatch, 2)

    _run_batch(tmp_path, monkeypatch, 1)
    with pytest.raises(oracle.OracleError, match="settings differ"):
        _run_batch(tmp_path, monkeypatch, 2, revision="b" * 40)


def test_batched_cohort_rejects_a_mutated_prior_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_batch(tmp_path, monkeypatch, 1)
    receipt_path = Path(first["batch"]["receipt_path"])
    receipt_path.write_text(receipt_path.read_text() + "\n", encoding="utf-8")

    with pytest.raises(oracle.OracleError, match="changed after it entered"):
        _run_batch(tmp_path, monkeypatch, 2)


def test_batched_cohort_enforces_cumulative_authoritative_gateway_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(1, 10):
        _run_batch(tmp_path, monkeypatch, index, gateway_cost=40.1)

    with pytest.raises(oracle.OracleError, match=r"registered \$400\.00 cohort ceiling"):
        _run_batch(tmp_path, monkeypatch, 10, gateway_cost=1.0)

    ledger = oracle._read_json(
        tmp_path / "state/runs/cohort-v4/compounding-cohort.json"
    )
    assert ledger["next_attempt_index"] == 10
    assert ledger["planned_attempts"][9]["execution_state"] == "pending"


def test_batched_cohort_rejects_a_second_concurrent_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preregistration = tmp_path / "preregistration.md"
    preregistration.write_text("frozen v4", encoding="utf-8")
    monkeypatch.setattr(oracle, "DEFAULT_COMPOUNDING_PREREGISTRATION", preregistration)
    state_root = tmp_path / "state"
    lock_path = state_root / "runs/cohort-v4/.batch.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(oracle.OracleError, match="already running"):
            oracle.run_compounding_batch(
                cohort_id="cohort-v4",
                attempt_index=1,
                acknowledge_paid_cap_usd="40.00",
                state_root=state_root,
            )


def test_invalid_started_batch_is_consumed_without_early_effect_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_batch(tmp_path, monkeypatch, 1, invalid_attempt=1)
    ledger = oracle._read_json(Path(first["cohort_ledger"]))

    assert first["batch"]["analysable"] is False
    assert first["batch"]["excluded_reason"] == "chain_did_not_complete"
    assert "reward" not in first["batch"]
    assert "treatment" not in first["batch"]
    assert ledger["next_attempt_index"] == 2
    assert ledger["summary"] is None


def test_preflight_failure_does_not_consume_index_and_existing_receipt_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preregistration = tmp_path / "preregistration.md"
    preregistration.write_text("frozen v4", encoding="utf-8")
    monkeypatch.setattr(oracle, "DEFAULT_COMPOUNDING_PREREGISTRATION", preregistration)
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}", encoding="utf-8")
    artifact_manifest = tmp_path / "artifact.json"
    artifact_manifest.write_text("{}", encoding="utf-8")

    def fail_before_receipt(**_kwargs: Any) -> dict[str, Any]:
        raise oracle.OracleError("preflight failed")

    monkeypatch.setattr(oracle, "run_compounding", fail_before_receipt)
    with pytest.raises(oracle.OracleError, match="preflight failed"):
        oracle.run_compounding_batch(
            cohort_id="cohort-v4",
            attempt_index=1,
            certification_fixture=fixture,
            artifact_manifest=artifact_manifest,
            acknowledge_paid_cap_usd="40.00",
            state_root=tmp_path / "state",
        )
    ledger_path = tmp_path / "state/runs/cohort-v4/compounding-cohort.json"
    assert oracle._read_json(ledger_path)["next_attempt_index"] == 1
    assert oracle._read_json(ledger_path)["batches"] == []
    assert oracle._read_json(ledger_path)["planned_attempts"][0]["execution_state"] == (
        "started_no_receipt"
    )

    monkeypatch.setattr(oracle, "run_compounding", _fake_batch_runner())
    completed = oracle.run_compounding_batch(
        cohort_id="cohort-v4",
        attempt_index=1,
        certification_fixture=fixture,
        artifact_manifest=artifact_manifest,
        acknowledge_paid_cap_usd="40.00",
        state_root=tmp_path / "state",
    )
    ledger = oracle._read_json(Path(completed["cohort_ledger"]))
    # Simulate a crash after the batch receipt was persisted but before its
    # digest entered the cohort ledger.
    ledger["batches"] = []
    ledger["next_attempt_index"] = 1
    ledger["cumulative_gateway_observed_usd"] = "0"
    ledger["planned_attempts"][0]["execution_state"] = "started_no_receipt"
    ledger["planned_attempts"][0]["consumed_at"] = None
    oracle._atomic_json(Path(completed["cohort_ledger"]), ledger)

    recovered = oracle.run_compounding_batch(
        cohort_id="cohort-v4",
        attempt_index=1,
        certification_fixture=fixture,
        artifact_manifest=artifact_manifest,
        acknowledge_paid_cap_usd="40.00",
        state_root=tmp_path / "state",
    )
    assert recovered["recovered"] is True
    assert recovered["progress"] == "1/10"


def test_batched_cohort_aggregates_only_after_exactly_ten_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result: dict[str, Any] | None = None
    for index in range(1, 11):
        result = _run_batch(tmp_path, monkeypatch, index)
        ledger = oracle._read_json(Path(result["cohort_ledger"]))
        if index < 10:
            assert ledger["summary"] is None
            assert result["cohort_complete"] is False

    assert result is not None
    ledger = oracle._read_json(Path(result["cohort_ledger"]))
    assert result["cohort_complete"] is True
    assert ledger["status"] == "completed"
    assert ledger["next_attempt_index"] is None
    assert ledger["cohort_valid"] is True
    assert ledger["summary"]["attempts_analysed"] == 10
    assert ledger["summary"]["paired_measures"]["cumulative_gateway_observed_usd"][
        "mean_difference"
    ] == pytest.approx(0)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"model": "anthropic/claude-haiku-4.5"}, "uses exactly anthropic/claude"),
        ({"max_turns": 44}, "exactly 45 turns"),
        ({"timeout_seconds": 899.0}, "exactly 900 seconds"),
    ],
)
def test_registered_runtime_settings_cannot_be_tuned(
    tmp_path: Path, override: dict[str, object], message: str
) -> None:
    with pytest.raises(oracle.OracleError, match=message):
        oracle.run_compounding(
            run_id="bad-runtime",
            admission=True,
            attempts=1,
            acknowledge_paid_cap_usd="40.00",
            state_root=tmp_path,
            **override,
        )
