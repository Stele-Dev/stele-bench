"""Parse one arm's scored receipt into the flat metrics every comparison uses.

Lives in the package rather than the private oracle because the durable result
records need exactly the same reading. Two parsers over the same receipt would
eventually disagree, and the disagreement would show up as a trend line that
contradicts the run it was built from.

Which measures matter is a claim about the experiment, not a detail: the memory
claim is that recall surfaces a constraint *earlier* and that the agent *cites*
it. Neither shows up in the reward, so both are parsed out explicitly here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Measures that are true/false per attempt and get a rate with an interval.
BINARY_MEASURES = ("cited_decision", "reward")
# Measures compared as a per-attempt paired difference, deliberately with no
# significance test at these sample sizes.
PAIRED_MEASURES = (
    "investigative_tool_calls",
    "session_investigative_tool_calls",
    "tokens_input",
    "trial_wall_seconds",
)


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def arm_metrics(receipt: Mapping[str, Any] | None) -> dict[str, float] | None:
    """Flatten a single-challenge scored receipt, or `None` if it is not one."""

    if not isinstance(receipt, Mapping):
        return None
    results = receipt.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return None
    result = results[0]
    tokens = result.get("tokens")
    cost = result.get("cost")
    timings = result.get("timings")
    if not all(isinstance(value, Mapping) for value in (tokens, cost, timings)):
        return None
    metrics: dict[str, float] = {}
    for name in ("input", "output", "cache_read", "cache_write"):
        value = _number(tokens.get(name))
        if value is not None:
            metrics[f"tokens_{name}"] = value
    for output, field in (
        ("claude_reported_usd", "claude_reported_amount_usd"),
        ("gateway_rate_cross_check_usd", "gateway_rate_cross_check_usd"),
    ):
        value = _number(cost.get(field))
        if value is not None:
            metrics[output] = value
    for output, field in (
        ("agent_execution_seconds", "agent_execution"),
        ("trial_wall_seconds", "invocation_wall"),
    ):
        value = _number(timings.get(field))
        if value is not None:
            metrics[output] = value
    reward = _number(result.get("reward"))
    if reward is not None:
        metrics["reward"] = reward

    evidence = result.get("evidence")
    # For a conversationally-answered challenge THIS is the memory signal, not
    # `reward` (which grades the workspace).
    response = evidence.get("response_evidence") if isinstance(evidence, Mapping) else None
    if isinstance(response, Mapping) and isinstance(response.get("cited"), bool):
        metrics["cited_decision"] = float(response["cited"])
    depth = evidence.get("investigation_depth") if isinstance(evidence, Mapping) else None
    if isinstance(depth, Mapping):
        # How much reading the agent did in the graded turn: the memory claim is
        # that recall surfaces a constraint EARLIER, which shows up here rather
        # than in the reward.
        for output, field in (
            ("investigative_tool_calls", "investigative"),
            ("total_tool_calls", "total"),
        ):
            value = _number(depth.get(field))
            if value is not None:
                metrics[output] = value
        # The graded turn of a conversational question is often toolless in
        # every arm; whole-session digging is where a shortened dig would show.
        session = depth.get("session")
        if isinstance(session, Mapping):
            for output, field in (
                ("session_investigative_tool_calls", "investigative"),
                ("session_total_tool_calls", "total"),
            ):
                value = _number(session.get(field))
                if value is not None:
                    metrics[output] = value
    return metrics


def memory_evidence(receipt: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(receipt, Mapping):
        return None
    results = receipt.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], Mapping):
        return None
    evidence = results[0].get("memory_evidence")
    return evidence if isinstance(evidence, dict) else None
