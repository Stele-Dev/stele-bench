"""Small-sample statistics for repeated benchmark attempts.

A benchmark that reports "the memory arm scored 1.0" from a single attempt has
reported a coin flip. These functions exist so a repeated run reports a *rate
with an interval* and an exact test instead of a point estimate, at the sample
sizes a paid agent benchmark can actually afford (single digits per arm).

Everything here is exact and deterministic: integer combinatorics, no sampling,
no normal approximation to a binomial tail, and no dependency beyond the
standard library. The same inputs always produce the same numbers, which is what
lets a receipt be re-derived from its own recorded attempts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

# 97.5th percentile of the standard normal — the two-sided 95% Wilson interval.
_Z_95 = 1.959963984540054
# 95th percentile — a one-sided 95% bound (equivalently, one side of a
# two-sided 90% interval). Non-inferiority is a directional test, so using
# `_Z_95` for its lower bound would silently test at alpha=0.025 while the
# pre-registered power calculation is sized for one-sided alpha=0.05.
_Z_ONE_SIDED_95 = 1.6448536269514722


def wilson_interval(successes: int, trials: int, z: float = _Z_95) -> tuple[float, float]:
    """Return the Wilson score interval for a binomial rate.

    Wilson rather than the textbook normal ("Wald") interval because Wald is
    useless exactly where this benchmark lives: at 0/5 or 5/5 it collapses to a
    zero-width interval and claims certainty from five observations. Wilson
    stays bounded inside [0, 1] and keeps a sane width at the extremes.
    """

    if trials < 0 or successes < 0:
        raise ValueError("successes and trials must be non-negative")
    if successes > trials:
        raise ValueError("successes cannot exceed trials")
    if trials == 0:
        return (0.0, 1.0)
    n = float(trials)
    phat = successes / n
    denominator = 1.0 + z * z / n
    center = (phat + z * z / (2.0 * n)) / denominator
    half_width = (z / denominator) * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    return (max(0.0, center - half_width), min(1.0, center + half_width))


def fisher_exact_greater(
    treatment_successes: int,
    treatment_trials: int,
    control_successes: int,
    control_trials: int,
) -> float:
    """One-sided Fisher exact p for `treatment rate > control rate`.

    Conditioning on both margins, the count of treatment successes is
    hypergeometric, so the p-value is an exact sum of hypergeometric terms —
    computed here with `math.comb`, so it is exact integer arithmetic until the
    final division.

    One-sided because the prediction it tests is directional and registered in
    advance: a benchmark that decides the direction after seeing the split is
    not testing anything. Callers must not use this to hunt for a difference in
    whichever direction the data happened to fall.
    """

    for value in (treatment_successes, treatment_trials, control_successes, control_trials):
        if value < 0:
            raise ValueError("counts must be non-negative")
    if treatment_successes > treatment_trials or control_successes > control_trials:
        raise ValueError("successes cannot exceed trials")
    total = treatment_trials + control_trials
    successes = treatment_successes + control_successes
    if total == 0 or successes == 0 or successes == total:
        # No margin to condition on, or every observation fell on one side:
        # every table consistent with the margins is the observed one.
        return 1.0
    denominator = math.comb(total, successes)
    upper = min(treatment_trials, successes)
    tail = sum(
        math.comb(treatment_trials, i) * math.comb(control_trials, successes - i)
        for i in range(treatment_successes, upper + 1)
        if successes - i <= control_trials
    )
    return tail / denominator


def mcnemar_exact_greater(
    *, treatment: Sequence[bool], control: Sequence[bool]
) -> float:
    """Exact paired p for `treatment success > control success`.

    Concordant pairs carry no information about a treatment effect. Under the
    null, each discordant pair is equally likely to favour either arm, so the
    treatment-only count follows Binomial(discordant, 0.5). The one-sided tail
    is the exact McNemar test, expressed with integer combinatorics until the
    final division.
    """

    if not treatment or not control or len(treatment) != len(control):
        raise ValueError("McNemar's test requires non-empty matched observations")
    treatment_only = sum(
        1 for t, c in zip(treatment, control, strict=True) if t and not c
    )
    control_only = sum(
        1 for t, c in zip(treatment, control, strict=True) if c and not t
    )
    discordant = treatment_only + control_only
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, value) for value in range(treatment_only, discordant + 1))
    return tail / (2**discordant)


def binary_measure_summary(
    *,
    treatment: Sequence[bool],
    control: Sequence[bool],
) -> dict[str, Any]:
    """Summarize one binary measure across paired attempts.

    Reports each arm as `successes/trials` with a Wilson interval, the absolute
    difference in rates, and the exact paired one-sided McNemar p for treatment
    > control. Fisher's independent-arm p remains in the receipt for continuity
    with earlier runs, but it is not the design-matched test. Never a bare point
    estimate: the interval and the p travel with the rate so a reader cannot
    quote the rate alone.
    """

    treatment_successes = sum(1 for value in treatment if value)
    control_successes = sum(1 for value in control if value)
    treatment_rate = treatment_successes / len(treatment) if treatment else None
    control_rate = control_successes / len(control) if control else None
    return {
        "treatment": _arm_rate(treatment_successes, len(treatment)),
        "control": _arm_rate(control_successes, len(control)),
        "difference": (
            treatment_rate - control_rate
            if treatment_rate is not None and control_rate is not None
            else None
        ),
        "mcnemar_one_sided_p": mcnemar_exact_greater(
            treatment=treatment, control=control
        )
        if treatment and control and len(treatment) == len(control)
        else None,
        "fisher_one_sided_p": fisher_exact_greater(
            treatment_successes, len(treatment), control_successes, len(control)
        )
        if treatment and control
        else None,
        "primary_test": "exact-paired-mcnemar",
        "direction_tested": "treatment_greater_than_control",
    }


def _arm_rate(successes: int, trials: int) -> dict[str, Any]:
    low, high = wilson_interval(successes, trials)
    return {
        "successes": successes,
        "trials": trials,
        "rate": successes / trials if trials else None,
        "wilson_95": [low, high],
    }


def paired_binary_difference(
    *,
    treatment: Sequence[bool],
    control: Sequence[bool],
) -> dict[str, Any] | None:
    """Confidence interval for the difference between two PAIRED binary rates.

    `binary_measure_summary` reports each arm's rate with its own interval and a
    test for treatment > control. That is the right shape for "does memory
    help", and the wrong shape for "does installing it hurt", which is a
    *non-inferiority* question and needs an interval on the difference itself —
    a claim of no harm is a claim about how far down the difference could
    plausibly be, and neither arm's own interval answers that.

    The two arms are not independent: they ran the same tasks, so a task that is
    hard is hard for both. Treating them as independent samples throws that
    pairing away and produces an interval far wider than the data supports.
    Newcombe's method 10 keeps it, by square-and-adding the two Wilson intervals
    with the observed correlation between the arms.

    Returns `None` when the arms are not paired observation-for-observation,
    because every number below is meaningless without that.
    """

    if not treatment or not control or len(treatment) != len(control):
        return None
    both = sum(1 for t, c in zip(treatment, control, strict=True) if t and c)
    treatment_only = sum(1 for t, c in zip(treatment, control, strict=True) if t and not c)
    control_only = sum(1 for t, c in zip(treatment, control, strict=True) if c and not t)
    neither = sum(1 for t, c in zip(treatment, control, strict=True) if not t and not c)
    n = float(len(treatment))

    treatment_rate = (both + treatment_only) / n
    control_rate = (both + control_only) / n
    difference = treatment_rate - control_rate

    # Newcombe's phi: the observed association between the arms across pairs.
    # Zero when a margin is empty, which is the conservative reading — it falls
    # back to treating the arms as independent rather than assuming agreement
    # the data does not show.
    product = (
        (both + treatment_only)
        * (control_only + neither)
        * (both + control_only)
        * (treatment_only + neither)
    )
    if product == 0:
        phi = 0.0
    else:
        phi = (both * neither - treatment_only * control_only) / math.sqrt(product)
        phi = max(-1.0, min(1.0, phi))

    lower, upper = _newcombe_paired_interval(
        both=both,
        treatment_only=treatment_only,
        control_only=control_only,
        n=int(n),
        phi=phi,
        z=_Z_95,
    )
    non_inferiority_lower, _ = _newcombe_paired_interval(
        both=both,
        treatment_only=treatment_only,
        control_only=control_only,
        n=int(n),
        phi=phi,
        z=_Z_ONE_SIDED_95,
    )
    return {
        "pairs": int(n),
        "treatment_rate": treatment_rate,
        "control_rate": control_rate,
        "difference": difference,
        "difference_95": [lower, upper],
        "non_inferiority_95_lower": non_inferiority_lower,
        "discordant": {"treatment_only": treatment_only, "control_only": control_only},
        "method": "newcombe-method-10-paired",
    }


def _newcombe_paired_interval(
    *,
    both: int,
    treatment_only: int,
    control_only: int,
    n: int,
    phi: float,
    z: float,
) -> tuple[float, float]:
    """Newcombe method 10 interval at the requested normal quantile."""

    treatment_rate = (both + treatment_only) / n
    control_rate = (both + control_only) / n
    difference = treatment_rate - control_rate
    low_t, high_t = wilson_interval(both + treatment_only, n, z)
    low_c, high_c = wilson_interval(both + control_only, n, z)
    lower_term = (
        (treatment_rate - low_t) ** 2
        - 2.0 * phi * (treatment_rate - low_t) * (high_c - control_rate)
        + (high_c - control_rate) ** 2
    )
    upper_term = (
        (high_t - treatment_rate) ** 2
        - 2.0 * phi * (high_t - treatment_rate) * (control_rate - low_c)
        + (control_rate - low_c) ** 2
    )
    return (
        max(-1.0, difference - math.sqrt(max(0.0, lower_term))),
        min(1.0, difference + math.sqrt(max(0.0, upper_term))),
    )


def non_inferiority_verdict(
    difference: Mapping[str, Any] | None, margin: float
) -> dict[str, Any]:
    """Decide non-inferiority against a margin that was declared in advance.

    The verdict is one comparison: does the lower bound of the difference
    interval stay above `-margin`. Everything else here exists to stop that
    comparison being read as more than it is.

    `inconclusive` is a distinct outcome from `inferior` on purpose. A run that
    cannot rule out a large drop has not shown the treatment is harmful; it has
    shown the sample was too small to say. Collapsing those two into "not
    non-inferior" is how an underpowered run gets written up as a finding.
    """

    if not isinstance(margin, float) or not (0.0 < margin < 1.0):
        raise ValueError("the non-inferiority margin must be a rate between zero and one")
    if not isinstance(difference, Mapping) or not isinstance(
        difference.get("difference_95"), Sequence
    ):
        return {
            "margin": margin,
            "verdict": "not_evaluable",
            "reason": "no paired difference interval",
        }
    descriptive_lower = float(difference["difference_95"][0])
    upper = float(difference["difference_95"][1])
    lower = float(difference.get("non_inferiority_95_lower", descriptive_lower))
    if lower > -margin:
        verdict = "non_inferior"
    elif upper <= -margin:
        verdict = "inferior"
    else:
        verdict = "inconclusive"
    return {
        "margin": margin,
        "verdict": verdict,
        "difference": difference.get("difference"),
        "difference_95": [descriptive_lower, upper],
        "non_inferiority_95_lower": lower,
        "pairs": difference.get("pairs"),
        "note": (
            "one-sided non-inferiority at a one-sided 95% lower bound; "
            "difference_95 remains the descriptive two-sided 95% interval; "
            "an inconclusive verdict means the sample is too small to decide, "
            "not that the treatment is safe"
        ),
    }


def paired_measure_summary(
    *,
    treatment: Sequence[float],
    control: Sequence[float],
) -> dict[str, Any] | None:
    """Summarize one continuous measure as a per-attempt paired difference.

    Reports the median and full range of `treatment - control` and how many
    attempts favoured treatment, deliberately with **no p-value**. At five
    attempts a continuous measure has no usable power, and attaching a test to
    it would dress an exploratory observation up as a result. The count of
    attempts that moved the right way is the honest summary at this N.
    """

    if not treatment or not control or len(treatment) != len(control):
        return None
    differences = [t - c for t, c in zip(treatment, control, strict=True)]
    reductions = [
        (c - t) / c * 100.0 for t, c in zip(treatment, control, strict=True) if c != 0
    ]
    return {
        "differences": differences,
        "median_difference": _median(differences),
        "min_difference": min(differences),
        "max_difference": max(differences),
        "median_reduction_percent": _median(reductions) if reductions else None,
        "attempts_favouring_treatment": sum(1 for value in differences if value < 0),
        "attempts": len(differences),
        "test": None,
        "note": "exploratory — no significance test is computed for a continuous measure at this N",
    }


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[midpoint])
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0
