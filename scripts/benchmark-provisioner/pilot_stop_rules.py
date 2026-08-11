#!/usr/bin/env python3
"""Apply a pilot's pre-registered stop rules to its receipt, mechanically.

A pilot's job is not to produce a result. It is to decide whether the powered
run should happen at all, and at what N. Those decisions were written down
before the run, so applying them should be a command that reads the receipt —
not a person reading numbers and forming an impression. A rule you evaluate by
eye is a rule you can talk yourself past.

Every threshold here is quoted from `aider-polyglot-overhead-v1.md`. Changing
one means amending the pre-registration first; this file is the executable copy,
not the authority.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# Stop rule 1: a dataset the model almost never or almost always solves cannot
# answer this question at any N, and no amount of pairing fixes it.
CONTROL_FLOOR = 0.15
CONTROL_CEILING = 0.85
# Stop rule 2: the corpus is finite, so a required N above it means the claim is
# not purchasable here — the response is to say so, not to widen the margin.
AVAILABLE_TASKS = 225
# Confound 4: an ambiguous cheat signal gets a declared decision rule.
EXTERNAL_ACCESS_GAP = 0.20
# One-sided alpha 0.05, 80% power: (z_a + z_b)^2.
POWER_CONSTANT = (1.6448536269514722 + 0.8416212335729143) ** 2


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _fmt(value: float | None, places: int = 3) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def required_pairs(discordance: float, margin: float) -> int | None:
    """Pairs needed for a paired binary non-inferiority test at this discordance.

    n ≈ (z_α + z_β)² · ψ / δ². Returns None when ψ is zero — with no discordant
    pairs the formula says zero, which is an artefact of a sample too small to
    have produced one yet, not a finding that no sample is needed.
    """

    if discordance <= 0:
        return None
    return math.ceil(POWER_CONSTANT * discordance / (margin * margin))


def _arm_outcomes(receipt: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Per arm label: outcome, reward, terminal reason, network access."""

    parsed: dict[str, dict[str, Any]] = {}
    for label, arm in (receipt.get("arms") or {}).items():
        body = arm.get("receipt")
        if not isinstance(body, Mapping):
            parsed[label] = {"outcome": None, "reward": None, "terminal": None}
            continue
        result = (body.get("results") or [{}])[0]
        evidence = result.get("evidence") or {}
        parsed[label] = {
            "outcome": body.get("outcome"),
            "reward": result.get("reward"),
            "terminal": evidence.get("terminal_reason"),
            "external": evidence.get("external_source_access_detected"),
            "oracle": evidence.get("oracle_payload_markers") or [],
            "exit_code": arm.get("exit_code"),
            "error": arm.get("error"),
        }
    return parsed


def evaluate(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return every pre-registered verdict, plus the numbers behind each."""

    configuration = receipt.get("configuration") or {}
    summary = receipt.get("summary") or {}
    attempts = receipt.get("attempts") or []
    margin = float(configuration.get("non_inferiority_margin") or 0.10)
    arms = _arm_outcomes(receipt)

    analysable = [record for record in attempts if record.get("analysable")]
    control_solved = treatment_solved = discordant = 0
    for record in analysable:
        control = (record["metrics"]["control"] or {}).get("reward")
        treatment = (record["metrics"]["treatment"] or {}).get("reward")
        control_pass = isinstance(control, (int, float)) and control >= 1.0
        treatment_pass = isinstance(treatment, (int, float)) and treatment >= 1.0
        control_solved += control_pass
        treatment_solved += treatment_pass
        discordant += control_pass != treatment_pass

    n = len(analysable)
    control_rate = _rate(control_solved, n)
    discordance = _rate(discordant, n)

    # Rule 1 — floor / ceiling.
    if control_rate is None:
        floor = {"verdict": "not_evaluable", "reason": "no analysable tasks"}
    elif control_rate < CONTROL_FLOOR:
        floor = {
            "verdict": "STOP",
            "reason": f"control solve rate {control_rate:.2f} is on the floor",
        }
    elif control_rate > CONTROL_CEILING:
        floor = {
            "verdict": "STOP",
            "reason": f"control solve rate {control_rate:.2f} is on the ceiling",
        }
    else:
        floor = {
            "verdict": "proceed",
            "reason": f"control solve rate {control_rate:.2f} is measurable",
        }
    floor.update(
        {
            "control_solve_rate": control_rate,
            "treatment_solve_rate": _rate(treatment_solved, n),
        }
    )

    # Rule 2 — is the powered run purchasable on this corpus?
    needed = required_pairs(discordance, margin) if discordance is not None else None
    if discordance is None:
        power = {"verdict": "not_evaluable", "reason": "no analysable tasks"}
    elif needed is None:
        power = {
            "verdict": "underdetermined",
            "reason": (
                f"0 of {n} pairs were discordant — too small a sample to estimate "
                "the required N, not evidence that none is needed"
            ),
        }
    elif needed > AVAILABLE_TASKS:
        power = {
            "verdict": "STOP",
            "reason": (
                f"{needed} pairs needed at delta={margin} but only "
                f"{AVAILABLE_TASKS} tasks exist"
            ),
        }
    else:
        power = {"verdict": "proceed", "reason": f"{needed} of {AVAILABLE_TASKS} tasks needed"}
    power.update({"discordance": discordance, "required_pairs": needed, "margin": margin})

    # Rule 3 — differential network access.
    external = summary.get("external_source_access") or {}
    trials = external.get("trials") or 0
    control_external = _rate(external.get("control") or 0, trials)
    treatment_external = _rate(external.get("treatment") or 0, trials)
    if control_external is None or treatment_external is None:
        network = {"verdict": "not_evaluable", "reason": "no analysable trials"}
    elif abs(treatment_external - control_external) > EXTERNAL_ACCESS_GAP:
        network = {
            "verdict": "STOP",
            "reason": (
                f"arms differ by {abs(treatment_external - control_external):.2f} in "
                "network access; read the trajectories by hand before reporting"
            ),
        }
    else:
        network = {"verdict": "proceed", "reason": "network access is balanced across arms"}
    network.update({"control": control_external, "treatment": treatment_external})

    # Rule 4 — any reach for the reference solution at all.
    reached = sorted(label for label, arm in arms.items() if arm.get("oracle"))
    payload = (
        {"verdict": "STOP", "reason": f"{len(reached)} arm(s) referenced the answer payload"}
        if reached
        else {"verdict": "proceed", "reason": "no arm referenced the answer payload"}
    )
    payload["arms"] = reached

    # Confounds 8 and 10 — exclusions must not be lopsided, in either direction.
    balance: dict[str, Any] = {}
    for name, predicate in (
        ("setup_timeout", lambda arm: arm.get("error") == "condition_runner_failed"),
        ("budget_truncation", lambda arm: arm.get("terminal") == "budget_exhausted"),
        ("infrastructure_error", lambda arm: arm.get("outcome") == "infrastructure-error"),
        ("invalid", lambda arm: arm.get("outcome") == "invalid"),
    ):
        counts = {"control": 0, "treatment": 0}
        for label, arm in arms.items():
            role = "treatment" if label.endswith("-treatment") else "control"
            counts[role] += bool(predicate(arm))
        lopsided = counts["control"] != counts["treatment"]
        balance[name] = {
            **counts,
            "verdict": "REVIEW" if lopsided else "balanced",
            # Stated every time, because this is the trap: an exclusion leaves
            # the denominator, so whichever arm loses more trials looks better.
            "note": (
                "unbalanced exclusions flatter the arm that lost more trials"
                if lopsided
                else "symmetric"
            ),
        }

    evaluated = (
        ("floor_ceiling", floor),
        ("statistical_power", power),
        ("network_access", network),
        ("answer_payload", payload),
    )
    stops = [name for name, block in evaluated if block["verdict"] == "STOP"]
    # A rule that could not be evaluated has not been passed. Reporting
    # "proceed" for a run that is still going, or produced nothing, would be the
    # worst possible failure in a file whose entire job is to withhold a green
    # light — so anything short of a real verdict blocks.
    unevaluated = [
        name
        for name, block in evaluated
        if block["verdict"] in ("not_evaluable", "underdetermined")
    ]
    reviews = [name for name, block in balance.items() if block["verdict"] == "REVIEW"]
    in_progress = receipt.get("status") == "in_progress"

    if stops:
        decision = "STOP"
    elif in_progress:
        decision = "INCOMPLETE — the run has not finished"
    elif unevaluated:
        decision = f"INCONCLUSIVE — cannot evaluate: {', '.join(unevaluated)}"
    elif reviews:
        decision = "REVIEW"
    else:
        decision = "proceed to the powered run"

    return {
        "run_id": receipt.get("suite_id"),
        "status": receipt.get("status"),
        "tasks_run": summary.get("attempts_run") or 0,
        "tasks_analysed": n,
        "excluded": summary.get("attempts_excluded") or [],
        "rules": dict(evaluated),
        "exclusion_balance": balance,
        "decision": decision,
        "stops": stops,
        "unevaluated": unevaluated,
        "reviews": reviews,
    }


def render(verdicts: Mapping[str, Any]) -> str:
    lines = [
        f"pilot: {verdicts['run_id']}  ({verdicts['status']})",
        f"tasks: {verdicts['tasks_analysed']} analysable of {verdicts['tasks_run']} run",
        "",
        "pre-registered stop rules",
    ]
    for name, block in verdicts["rules"].items():
        mark = {"proceed": "ok", "STOP": "STOP", "not_evaluable": "??"}.get(
            block["verdict"], block["verdict"]
        )
        lines.append(f"  [{mark:>4}] {name}: {block['reason']}")
    rules = verdicts["rules"]
    lines += [
        "",
        f"  control solve rate : {_fmt(rules['floor_ceiling']['control_solve_rate'], 2)}"
        f"   (must be {CONTROL_FLOOR}–{CONTROL_CEILING})",
        f"  treatment solve rate: {_fmt(rules['floor_ceiling']['treatment_solve_rate'], 2)}",
        f"  discordance psi    : {_fmt(rules['statistical_power']['discordance'], 2)}",
        f"  pairs needed       : {rules['statistical_power']['required_pairs'] or '—'}"
        f"   (of {AVAILABLE_TASKS} available)",
        "",
        "exclusion balance (lopsided exclusions flatter the arm that lost more)",
    ]
    for name, block in verdicts["exclusion_balance"].items():
        mark = "ok" if block["verdict"] == "balanced" else "!!"
        lines.append(
            f"  [{mark:>4}] {name}: control={block['control']} treatment={block['treatment']}"
        )
    if verdicts["excluded"]:
        lines += ["", "excluded tasks"]
        lines += [
            f"  task {record.get('attempt')}: {record.get('reason')}"
            for record in verdicts["excluded"]
        ]
    lines += ["", f"DECISION: {verdicts['decision']}"]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path, nargs="?", help="path to a suite.json")
    parser.add_argument("--run-id", help="resolve the receipt from the state root")
    parser.add_argument("--json", action="store_true", help="emit the verdicts as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    path = args.receipt
    if path is None:
        if not args.run_id:
            parser.error("pass a receipt path or --run-id")
        root = Path(
            os.environ.get("STELE_BENCH_STATE_ROOT")
            or Path.home() / ".local/state/stele-bench"
        )
        path = root / "runs" / args.run_id / "suite.json"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1

    verdicts = evaluate(receipt)
    print(json.dumps(verdicts, indent=2) if args.json else render(verdicts))
    # Only a clean, complete pilot exits 0. Everything else is a non-zero the
    # caller has to look at — a stop rule fired, the run is unfinished, a rule
    # could not be evaluated, or exclusions were lopsided.
    return 0 if verdicts["decision"] == "proceed to the powered run" else 2


if __name__ == "__main__":
    raise SystemExit(main())
