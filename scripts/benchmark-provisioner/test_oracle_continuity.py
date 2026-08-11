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
    exclusive: bool = True,
) -> dict[str, Any]:
    rewards = rewards or [1, 1, 1, 1]
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
                "tokens": {"input": 100},
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
            {"session_number": index, "written": treatment and index == 1}
            for index in range(1, 5)
        ],
        "memory_delivery": [
            {"session_number": index, "delivered_prior_node": treatment and index == 2}
            for index in range(1, 5)
        ],
    }


def _attempt(index: int, treatment_rewards: list[int] | None = None) -> dict[str, Any]:
    return {
        "attempt": index,
        "arm_order": ["control", "treatment"] if index % 2 else ["treatment", "control"],
        "receipts": {
            "control": _chain(["0.50"] * 4),
            "treatment": _chain(
                ["0.70", "0.50", "0.20", "0.20"],
                treatment_rewards,
                treatment=True,
            ),
        },
        "analysable": True,
        "excluded_reason": None,
    }


def test_continuity_manifest_and_budget_are_frozen() -> None:
    payload, sessions = oracle._load_continuity_manifest(
        oracle.DEFAULT_CONTINUITY_CHAIN_MANIFEST
    )

    assert payload["experiment_id"] == "fieldnote-continuity-v8"
    assert payload["treatment_artifact"] == {
        "source_commit": "817d4d73ca2b4806a7b4a28ecd25ca7be86f9693",
        "sha256": "90295ac2f8bb26779ee778459e5f82d23fda060ecf47c20090a76a9bcd267500",
        "target": "linux-arm64",
    }
    assert len(sessions) == 4
    assert sessions[0]["challenge_id"] == "fieldnote-milestone-1"
    assert oracle._validate_continuity_budget("3.00", "30.00") == (
        oracle.Decimal("3.00"),
        oracle.Decimal("30.00"),
    )
    with pytest.raises(oracle.OracleError, match="exactly \\$3.00"):
        oracle._validate_continuity_budget("2.00", "30.00")


def test_continuity_treatment_artifact_is_exactly_bound() -> None:
    payload, _ = oracle._load_continuity_manifest(
        oracle.DEFAULT_CONTINUITY_CHAIN_MANIFEST
    )
    binding = payload["treatment_artifact"]
    manifest = {
        "source": {
            "head": binding["source_commit"],
            "dirty": False,
        }
    }
    artifact = {
        "sha256": binding["sha256"],
        "target": binding["target"],
    }

    oracle._validate_registered_artifact_binding(
        payload,
        artifact_manifest=manifest,
        artifact=artifact,
    )
    artifact["sha256"] = "0" * 64
    with pytest.raises(oracle.OracleError, match="does not match"):
        oracle._validate_registered_artifact_binding(
            payload,
            artifact_manifest=manifest,
            artifact=artifact,
        )


def test_continuity_admission_keeps_zero_behavior_as_valid() -> None:
    control = _chain(["0.50"] * 4, [0, 0, 0, 0])
    treatment = _chain(["0.60"] * 4, [0, 0, 0, 0], treatment=True)

    assert oracle._continuity_pair_valid(control, treatment, admission=True) is None
    assert "exclusive" in oracle._continuity_pair_valid(
        control,
        _chain(["0.60"] * 4, treatment=True, exclusive=False),
        admission=True,
    )


def test_continuity_summary_uses_four_prefixes_half_correctness_guard_and_intervals() -> None:
    summary = oracle._continuity_summary([_attempt(index) for index in range(1, 21)])

    assert summary["crossover_inference_available"] is True
    assert summary["earliest_established_crossover_session"] == 3
    assert summary["verdict"] == "crossover_established_at_session_3"
    assert len(summary["prefixes"]) == 4
    assert len(summary["prefixes"][2]["paired_bootstrap_median_difference_95"]) == 2
    assert summary["prefixes"][2]["median_input_token_difference"] == 0
    descriptive = summary["descriptive_memory_outcomes"]
    assert descriptive["treatment_session_1_graph_write"]["successes"] == 20
    assert descriptive["treatment_later_exact_version_delivery"]["successes"] == 20
    assert len(descriptive["treatment_session_1_graph_write"]["wilson_95"]) == 2

    guarded = [_attempt(index, [0, 0, 1, 1]) for index in range(1, 21)]
    guarded_summary = oracle._continuity_summary(guarded)
    assert guarded_summary["prefixes"][0]["treatment_pass_rate"] == 0.0
    assert guarded_summary["prefixes"][0]["correctness_guard_at_prefix"] is False
    assert guarded_summary["earliest_established_crossover_session"] is None


def test_continuity_cohort_freezes_order_and_summarizes_after_pair_20(
    tmp_path: Path,
) -> None:
    def fake_pair_runner(**kwargs: Any) -> dict[str, Any]:
        run_id = kwargs["run_id"]
        index = kwargs["attempt_index"]
        path = tmp_path / "runs" / run_id / "continuity-pair.json"
        path.parent.mkdir(parents=True)
        receipt = {
            "attempt": _attempt(index),
            "configuration": {"arm_order": kwargs["arm_order"]},
        }
        path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        return {"ok": True, "continuity_pair_receipt": str(path), "receipt": receipt}

    for index in range(1, 21):
        result = oracle.run_continuity_batch(
            cohort_id="fieldnote-continuity-test",
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
    assert ledger["authoritative_spend_usd"] == "72.00"
