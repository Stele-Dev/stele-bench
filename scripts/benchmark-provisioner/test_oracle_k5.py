from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import oracle
import pytest


def _chain(
    costs: list[str],
    rewards: list[int] | None = None,
    *,
    treatment: bool = False,
    write: bool = True,
    delivery: bool = True,
    exclusive: bool = True,
) -> dict[str, Any]:
    rewards = rewards or [1, 1, 1, 1, 1]
    sessions = [
        {
            "resolved_model": oracle.DEFAULT_COMPOUNDING_MODEL,
            "gateway_billing": {
                "delta_usd": cost,
                "source": "vercel-ai-gateway-quota",
                "exclusive_to_interval": exclusive,
            },
            "challenge": {
                "outcome": "passed" if reward else "failed",
                "reward": float(reward),
            },
        }
        for cost, reward in zip(costs, rewards, strict=True)
    ]
    return {
        "outcome": "completed",
        "cleanup_completed": True,
        "boundary": {"only_graph_crossed_boundary": True},
        "sessions": sessions,
        "graph_activity": [
            {
                "session_number": index,
                "written": treatment and write and index == 1,
            }
            for index in range(1, 6)
        ],
        "memory_delivery": [
            {
                "session_number": index,
                "delivered_prior_node": treatment and delivery and index == 2,
            }
            for index in range(1, 6)
        ],
    }


def _attempt(index: int, *, treatment_rewards: list[int] | None = None) -> dict[str, Any]:
    return {
        "attempt": index,
        "arm_order": ["control", "treatment"] if index % 2 else ["treatment", "control"],
        "receipts": {
            "control": _chain(["0.50"] * 5),
            "treatment": _chain(
                ["0.60", "0.55", "0.30", "0.30", "0.30"],
                treatment_rewards,
                treatment=True,
            ),
        },
        "analysable": True,
        "excluded_reason": None,
    }


def test_k5_manifest_and_budget_are_frozen() -> None:
    payload, sessions = oracle._load_k5_manifest(oracle.DEFAULT_K5_CHAIN_MANIFEST)

    assert payload["experiment_id"] == "click-clirunner-k5-v1"
    assert len(sessions) == 5
    assert sessions[0]["challenge_id"] == "click-clirunner-stream-contract"
    assert oracle._validate_k5_budget("3.00", "30.00") == (
        oracle.Decimal("3.00"),
        oracle.Decimal("30.00"),
    )
    with pytest.raises(oracle.OracleError, match="exactly \\$3.00"):
        oracle._validate_k5_budget("2.00", "20.00")


def test_k5_admission_requires_exclusive_cost_write_delivery_and_feasibility() -> None:
    control = _chain(["0.50"] * 5)
    treatment = _chain(["0.60", "0.55", "0.30", "0.30", "0.30"], treatment=True)

    assert oracle._k5_pair_valid(control, treatment, admission=True) is None
    assert "exclusive" in oracle._k5_pair_valid(
        control,
        _chain(["0.40"] * 5, treatment=True, exclusive=False),
        admission=True,
    )
    assert (
        oracle._k5_pair_valid(
            control,
            _chain(["0.40"] * 5, treatment=True, write=False),
            admission=True,
        )
        == "treatment_created_no_pre_reuse_graph_write"
    )
    assert (
        oracle._k5_pair_valid(
            control,
            _chain(["0.40"] * 5, treatment=True, delivery=False),
            admission=True,
        )
        == "treatment_delivered_no_exact_prior_node_version"
    )
    assert (
        oracle._k5_pair_valid(
            control,
            _chain(["1.50", "1.50", "0.10", "0.10", "0.10"], treatment=True),
            admission=True,
        )
        == "k5_crossover_infeasible_in_admission"
    )


def test_k5_summary_uses_holm_and_finds_the_registered_third_session_crossing() -> None:
    summary = oracle._k5_summary([_attempt(index) for index in range(1, 21)])

    assert summary["crossover_inference_available"] is True
    assert summary["earliest_established_crossover_session"] == 3
    assert summary["verdict"] == "crossover_established_at_session_3"
    assert summary["prefixes"][0]["median_difference_usd"] > 0
    assert summary["prefixes"][2]["negative"] == 20
    assert summary["prefixes"][2]["holm_adjusted_sign_p"] < 0.05


def test_k5_correctness_guard_is_through_prefix_and_incomplete_data_make_no_claim() -> None:
    attempts = [_attempt(index) for index in range(1, 21)]
    for attempt in attempts[:3]:
        attempt["receipts"]["treatment"]["sessions"][0]["challenge"] = {
            "outcome": "failed",
            "reward": 0.0,
        }
    summary = oracle._k5_summary(attempts)

    assert summary["prefixes"][0]["treatment_minus_control_pass_rate"] == pytest.approx(-0.15)
    assert summary["prefixes"][0]["correctness_guard_at_prefix"] is False
    assert summary["prefixes"][2]["correctness_guard_through_prefix"] is False
    assert summary["earliest_established_crossover_session"] is None

    incomplete = oracle._k5_summary([_attempt(index) for index in range(1, 20)])
    assert incomplete["crossover_inference_available"] is False
    assert incomplete["verdict"] == "incomplete_no_crossover_claim"


def test_k5_cohort_ledger_freezes_order_and_only_summarizes_after_pair_20(
    tmp_path: Path,
) -> None:
    def fake_pair_runner(**kwargs: Any) -> dict[str, Any]:
        run_id = kwargs["run_id"]
        index = kwargs["attempt_index"]
        path = tmp_path / "runs" / run_id / "k5-pair.json"
        path.parent.mkdir(parents=True)
        receipt = {
            "attempt": _attempt(index),
            "configuration": {"arm_order": kwargs["arm_order"]},
        }
        path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        return {"ok": True, "k5_pair_receipt": str(path), "receipt": receipt}

    for index in range(1, 21):
        result = oracle.run_k5_batch(
            cohort_id="click-k5-test",
            attempt_index=index,
            source=tmp_path / "source",
            acknowledge_paid_cap_usd="30.00",
            acknowledge_cohort_cap_usd="600.00",
            state_root=tmp_path,
            pair_runner=fake_pair_runner,
        )
        if index < 20:
            assert result["receipt"]["summary"] is None

    ledger = result["receipt"]
    assert ledger["status"] == "completed"
    assert len(ledger["planned_attempts"]) == 20
    assert ledger["planned_attempts"][0]["arm_order"] == ["control", "treatment"]
    assert ledger["planned_attempts"][1]["arm_order"] == ["treatment", "control"]
    assert ledger["summary"]["earliest_established_crossover_session"] == 3
    assert ledger["authoritative_spend_usd"] == "91.00"


def test_k5_cohort_consumes_pair_and_blocks_when_authoritative_cost_is_missing(
    tmp_path: Path,
) -> None:
    def fake_pair_runner(**kwargs: Any) -> dict[str, Any]:
        run_id = kwargs["run_id"]
        path = tmp_path / "runs" / run_id / "k5-pair.json"
        path.parent.mkdir(parents=True)
        attempt = _attempt(kwargs["attempt_index"])
        attempt["receipts"]["control"]["sessions"][0]["gateway_billing"][
            "exclusive_to_interval"
        ] = False
        receipt = {
            "attempt": attempt,
            "configuration": {"arm_order": kwargs["arm_order"]},
        }
        path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        return {"ok": True, "k5_pair_receipt": str(path), "receipt": receipt}

    result = oracle.run_k5_batch(
        cohort_id="click-k5-missing-cost",
        attempt_index=1,
        source=tmp_path / "source",
        acknowledge_paid_cap_usd="30.00",
        acknowledge_cohort_cap_usd="600.00",
        state_root=tmp_path,
        pair_runner=fake_pair_runner,
    )

    ledger = result["receipt"]
    assert result["ok"] is False
    assert ledger["status"] == "blocked_missing_authoritative_cost"
    assert "exclusive settled session costs" in ledger["error"]
    assert len(ledger["batches"]) == 1
    assert ledger["batches"][0]["authoritative_spend_usd"] is None
    with pytest.raises(oracle.OracleError, match="once in frozen order"):
        oracle.run_k5_batch(
            cohort_id="click-k5-missing-cost",
            attempt_index=1,
            source=tmp_path / "source",
            acknowledge_paid_cap_usd="30.00",
            acknowledge_cohort_cap_usd="600.00",
            state_root=tmp_path,
            pair_runner=fake_pair_runner,
        )
