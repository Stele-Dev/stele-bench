"""The pilot's stop rules, applied mechanically rather than by eye."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_pilot_stop_rules", Path(__file__).with_name("pilot_stop_rules.py")
)
assert _SPEC is not None and _SPEC.loader is not None
rules = importlib.util.module_from_spec(_SPEC)
sys.modules["_pilot_stop_rules"] = rules
_SPEC.loader.exec_module(rules)


def _receipt(
    *,
    control: tuple[float, ...],
    treatment: tuple[float, ...],
    margin: float = 0.10,
    external: dict[str, int] | None = None,
    arm_extras: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A suite receipt in the shape `run_suite` writes."""

    attempts, arms = [], {}
    for index, (c, t) in enumerate(zip(control, treatment, strict=True), start=1):
        labels = {
            "control": f"task-{index:02d}-control",
            "treatment": f"task-{index:02d}-treatment",
        }
        attempts.append(
            {
                "attempt": index,
                "labels": labels,
                "analysable": True,
                "metrics": {"control": {"reward": c}, "treatment": {"reward": t}},
                "outcomes": {"control": "passed", "treatment": "passed"},
            }
        )
        for role, reward in (("control", c), ("treatment", t)):
            label = labels[role]
            arms[label] = {
                "exit_code": 0,
                "receipt": {
                    "outcome": "passed" if reward >= 1.0 else "failed",
                    "results": [
                        {
                            "reward": reward,
                            "evidence": {"terminal_reason": "completed"},
                        }
                    ],
                },
                **(arm_extras or {}).get(label, {}),
            }
    trials = len(control)
    return {
        "suite_id": "aider-pilot-1",
        "status": "completed",
        "configuration": {"non_inferiority_margin": margin},
        "arms": arms,
        "attempts": attempts,
        "summary": {
            "attempts_run": trials,
            "attempts_analysed": trials,
            "attempts_excluded": [],
            "external_source_access": external
            or {"trials": trials, "control": 0, "treatment": 0},
        },
    }


def test_a_floored_dataset_stops_the_powered_run() -> None:
    """A dataset the model never solves cannot answer the question at any N."""

    verdicts = rules.evaluate(_receipt(control=(0.0,) * 10, treatment=(0.0,) * 10))
    assert verdicts["rules"]["floor_ceiling"]["verdict"] == "STOP"
    assert verdicts["decision"] == "STOP"
    assert "floor" in verdicts["rules"]["floor_ceiling"]["reason"]


def test_a_ceilinged_dataset_stops_the_powered_run() -> None:
    verdicts = rules.evaluate(_receipt(control=(1.0,) * 10, treatment=(1.0,) * 10))
    assert verdicts["rules"]["floor_ceiling"]["verdict"] == "STOP"
    assert "ceiling" in verdicts["rules"]["floor_ceiling"]["reason"]


def test_a_measurable_solve_rate_proceeds() -> None:
    control = (1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    treatment = (1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    verdicts = rules.evaluate(_receipt(control=control, treatment=treatment))
    assert verdicts["rules"]["floor_ceiling"]["verdict"] == "proceed"
    assert verdicts["rules"]["floor_ceiling"]["control_solve_rate"] == 0.4


def test_zero_discordance_is_underdetermined_not_zero_tasks_needed() -> None:
    """The formula says 0 pairs; the honest reading is 'sample too small'.

    With no discordant pair yet, `psi = 0` and `n = k*psi/delta^2` is zero —
    which would license running the powered comparison on nothing at all.
    """

    control = (1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    verdicts = rules.evaluate(_receipt(control=control, treatment=control))
    power = verdicts["rules"]["statistical_power"]
    assert power["discordance"] == 0.0
    assert power["required_pairs"] is None
    assert power["verdict"] == "underdetermined"
    assert "not evidence that none is needed" in power["reason"]


def test_a_required_n_beyond_the_corpus_stops_the_run() -> None:
    """Say the claim is not purchasable — do not widen the margin until it fits."""

    # Every pair discordant: psi = 1.0 needs 619 pairs at delta = 0.10.
    control = (1.0, 0.0) * 5
    treatment = (0.0, 1.0) * 5
    verdicts = rules.evaluate(_receipt(control=control, treatment=treatment))
    power = verdicts["rules"]["statistical_power"]
    assert power["discordance"] == 1.0
    assert power["required_pairs"] > rules.AVAILABLE_TASKS
    assert power["verdict"] == "STOP"


def test_required_pairs_matches_the_preregistered_table() -> None:
    """These three are quoted in the pre-registration; pin them."""

    assert rules.required_pairs(0.15, 0.10) == 93
    assert rules.required_pairs(0.25, 0.10) == 155
    assert rules.required_pairs(0.35, 0.10) == 217


def test_differential_network_access_stops_the_run() -> None:
    control = (1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0)
    verdicts = rules.evaluate(
        _receipt(
            control=control,
            treatment=control,
            external={"trials": 10, "control": 0, "treatment": 5},
        )
    )
    network = verdicts["rules"]["network_access"]
    assert network["verdict"] == "STOP"
    assert "by hand" in network["reason"]


def test_balanced_network_access_proceeds() -> None:
    control = (1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0)
    verdicts = rules.evaluate(
        _receipt(
            control=control,
            treatment=control,
            external={"trials": 10, "control": 2, "treatment": 3},
        )
    )
    assert verdicts["rules"]["network_access"]["verdict"] == "proceed"


def test_any_reach_for_the_answer_payload_stops_the_run() -> None:
    control = (1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0)
    receipt = _receipt(control=control, treatment=control)
    arm = receipt["arms"]["task-01-treatment"]["receipt"]["results"][0]
    arm["evidence"]["oracle_payload_markers"] = [".oracle"]

    verdicts = rules.evaluate(receipt)
    assert verdicts["rules"]["answer_payload"]["verdict"] == "STOP"
    assert verdicts["rules"]["answer_payload"]["arms"] == ["task-01-treatment"]


def test_lopsided_truncation_is_flagged_for_review() -> None:
    """The trap: an excluded trial leaves the denominator.

    Whichever arm loses more trials looks better, so an imbalance is reported
    even when every stop rule passes.
    """

    # Discordant on one pair, so the power rule evaluates and the exclusion
    # balance is what this test is actually isolating.
    control = (1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    treatment = (1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    receipt = _receipt(control=control, treatment=treatment)
    for label in ("task-01-treatment", "task-02-treatment"):
        receipt["arms"][label]["receipt"]["results"][0]["evidence"]["terminal_reason"] = (
            "budget_exhausted"
        )

    verdicts = rules.evaluate(receipt)
    truncation = verdicts["exclusion_balance"]["budget_truncation"]
    assert truncation == {
        "control": 0,
        "treatment": 2,
        "verdict": "REVIEW",
        "note": "unbalanced exclusions flatter the arm that lost more trials",
    }
    assert verdicts["decision"] == "REVIEW"


def test_symmetric_exclusions_do_not_block() -> None:
    control = (1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    treatment = (1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    receipt = _receipt(control=control, treatment=treatment)
    for label in ("task-01-treatment", "task-02-control"):
        receipt["arms"][label]["receipt"]["results"][0]["evidence"]["terminal_reason"] = (
            "budget_exhausted"
        )

    verdicts = rules.evaluate(receipt)
    assert verdicts["exclusion_balance"]["budget_truncation"]["verdict"] == "balanced"
    assert verdicts["decision"] == "proceed to the powered run"


def test_a_stop_exits_nonzero_so_a_script_cannot_ignore_it(tmp_path: Path) -> None:
    import json

    receipt = tmp_path / "suite.json"
    receipt.write_text(json.dumps(_receipt(control=(0.0,) * 10, treatment=(0.0,) * 10)))
    assert rules.main([str(receipt)]) == 2


def test_a_clean_pilot_exits_zero(tmp_path: Path) -> None:
    import json

    control = (1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    treatment = (1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    receipt = tmp_path / "suite.json"
    receipt.write_text(json.dumps(_receipt(control=control, treatment=treatment)))
    assert rules.main([str(receipt)]) == 0


def test_the_report_renders_every_rule_and_the_decision(tmp_path: Path) -> None:
    control = (1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    treatment = (1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    text = rules.render(rules.evaluate(_receipt(control=control, treatment=treatment)))
    for expected in (
        "floor_ceiling",
        "statistical_power",
        "network_access",
        "answer_payload",
        "discordance psi",
        "DECISION:",
    ):
        assert expected in text


@pytest.mark.parametrize("margin", [0.05, 0.10, 0.20])
def test_a_tighter_margin_always_needs_more_pairs(margin: float) -> None:
    assert rules.required_pairs(0.25, margin) == pytest.approx(
        rules.required_pairs(0.25, 0.10) * (0.10 / margin) ** 2, rel=0.01
    )


def test_an_unfinished_run_never_says_proceed(tmp_path: Path) -> None:
    """The worst possible failure for a file whose job is withholding a green light."""

    import json

    receipt = _receipt(control=(), treatment=())
    receipt["status"] = "in_progress"
    verdicts = rules.evaluate(receipt)
    assert verdicts["decision"].startswith("INCOMPLETE")

    path = tmp_path / "suite.json"
    path.write_text(json.dumps(receipt))
    assert rules.main([str(path)]) == 2


def test_an_unevaluable_rule_blocks_rather_than_passes() -> None:
    """A rule that could not be evaluated has not been passed."""

    control = (1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    # Identical arms -> zero discordance -> the power rule is underdetermined.
    verdicts = rules.evaluate(_receipt(control=control, treatment=control))
    assert verdicts["rules"]["statistical_power"]["verdict"] == "underdetermined"
    assert verdicts["decision"].startswith("INCONCLUSIVE")
    assert "statistical_power" in verdicts["unevaluated"]


def test_a_review_also_exits_nonzero(tmp_path: Path) -> None:
    import json

    control = (1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    treatment = (1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    receipt = _receipt(control=control, treatment=treatment)
    receipt["arms"]["task-01-treatment"]["receipt"]["results"][0]["evidence"][
        "terminal_reason"
    ] = "budget_exhausted"
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(receipt))
    assert rules.main([str(path)]) == 2
