from __future__ import annotations

import math

import pytest

from stele_bench.statistics import (
    binary_measure_summary,
    fisher_exact_greater,
    mcnemar_exact_greater,
    non_inferiority_verdict,
    paired_binary_difference,
    paired_measure_summary,
    wilson_interval,
)


def test_wilson_interval_stays_bounded_at_the_extremes() -> None:
    """The whole reason for Wilson: 0/5 and 5/5 must not claim certainty."""

    low, high = wilson_interval(0, 5)
    assert low == 0.0
    assert 0.4 < high < 0.5

    low, high = wilson_interval(5, 5)
    assert 0.5 < low < 0.6
    assert high == 1.0


def test_wilson_interval_of_no_trials_is_the_whole_range() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_matches_a_known_value() -> None:
    """3/5 → (0.2307, 0.8824), the published Wilson 95% interval.

    Checked against the published interval rather than against this
    implementation's own output, so the test can actually fail:
    centre = (0.6 + z²/10) / (1 + z²/5), half-width =
    (z / (1 + z²/5)) · √(0.24/5 + z²/100).
    """

    low, high = wilson_interval(3, 5)
    assert math.isclose(low, 0.2307, abs_tol=1e-4)
    assert math.isclose(high, 0.8824, abs_tol=1e-4)


def test_wilson_interval_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        wilson_interval(6, 5)
    with pytest.raises(ValueError):
        wilson_interval(-1, 5)


@pytest.mark.parametrize(
    ("treatment_successes", "expected"),
    [
        # These three values are quoted in the dotfile-drift-detection
        # pre-registration as the thresholds the run is judged against, so they
        # are pinned here: if the arithmetic ever moves, the registered
        # decision rule moved with it.
        (5, 1 / 252),
        (4, 5 / 210),
        (3, 10 / 120),
    ],
)
def test_fisher_matches_the_preregistered_thresholds(
    treatment_successes: int, expected: float
) -> None:
    assert math.isclose(
        fisher_exact_greater(treatment_successes, 5, 0, 5), expected, rel_tol=1e-12
    )


def test_fisher_at_five_versus_zero_clears_the_registered_alpha() -> None:
    assert fisher_exact_greater(5, 5, 0, 5) < 0.05


def test_fisher_at_three_versus_zero_does_not_clear_the_registered_alpha() -> None:
    """3/5 vs 0/5 is suggestive and must NOT read as established."""

    assert fisher_exact_greater(3, 5, 0, 5) > 0.05


def test_fisher_is_one_sided_and_does_not_reward_the_wrong_direction() -> None:
    assert fisher_exact_greater(0, 5, 5, 5) == 1.0


def test_fisher_with_no_variation_is_one() -> None:
    assert fisher_exact_greater(5, 5, 5, 5) == 1.0
    assert fisher_exact_greater(0, 5, 0, 5) == 1.0


def test_fisher_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        fisher_exact_greater(6, 5, 0, 5)
    with pytest.raises(ValueError):
        fisher_exact_greater(1, 5, -1, 5)


def test_mcnemar_uses_only_paired_discordant_outcomes() -> None:
    treatment = [True, True, True, True, True]
    control = [False, False, False, False, False]
    assert math.isclose(
        mcnemar_exact_greater(treatment=treatment, control=control),
        1 / 32,
        rel_tol=1e-12,
    )


def test_mcnemar_does_not_reward_the_wrong_direction_or_concordance() -> None:
    assert mcnemar_exact_greater(
        treatment=[False] * 5, control=[True] * 5
    ) == 1.0
    assert mcnemar_exact_greater(
        treatment=[True, False], control=[True, False]
    ) == 1.0


def test_mcnemar_requires_matched_observations() -> None:
    with pytest.raises(ValueError):
        mcnemar_exact_greater(treatment=[True], control=[])
    with pytest.raises(ValueError):
        mcnemar_exact_greater(treatment=[True, False], control=[False])


def test_binary_summary_carries_interval_and_p_beside_every_rate() -> None:
    summary = binary_measure_summary(
        treatment=[True, True, True, True, True],
        control=[False, False, False, False, False],
    )
    assert summary["treatment"]["rate"] == 1.0
    assert summary["control"]["rate"] == 0.0
    assert summary["difference"] == 1.0
    assert math.isclose(summary["mcnemar_one_sided_p"], 1 / 32, rel_tol=1e-12)
    assert math.isclose(summary["fisher_one_sided_p"], 1 / 252, rel_tol=1e-12)
    assert summary["primary_test"] == "exact-paired-mcnemar"
    # A rate is never reported alone.
    assert summary["treatment"]["wilson_95"][0] < 1.0
    assert summary["control"]["wilson_95"][1] > 0.0


def test_binary_summary_of_a_split_result() -> None:
    summary = binary_measure_summary(
        treatment=[True, True, True, False, False],
        control=[False, True, False, False, False],
    )
    assert summary["treatment"]["successes"] == 3
    assert summary["control"]["successes"] == 1
    assert summary["fisher_one_sided_p"] > 0.05


def test_paired_summary_reports_direction_counts_and_no_test() -> None:
    summary = paired_measure_summary(treatment=[8.0, 9.0, 12.0], control=[12.0, 12.0, 10.0])
    assert summary is not None
    assert summary["differences"] == [-4.0, -3.0, 2.0]
    assert summary["median_difference"] == -3.0
    assert summary["attempts_favouring_treatment"] == 2
    assert summary["attempts"] == 3
    # Explicitly no significance test at this N.
    assert summary["test"] is None
    assert math.isclose(summary["median_reduction_percent"], 25.0, rel_tol=1e-9)


def test_paired_summary_handles_a_zero_control_without_dividing_by_it() -> None:
    summary = paired_measure_summary(treatment=[1.0, 2.0], control=[0.0, 4.0])
    assert summary is not None
    assert summary["median_reduction_percent"] == 50.0


def test_paired_summary_needs_matched_attempts() -> None:
    assert paired_measure_summary(treatment=[1.0], control=[]) is None
    assert paired_measure_summary(treatment=[1.0, 2.0], control=[1.0]) is None


def test_paired_summary_median_of_an_even_count() -> None:
    summary = paired_measure_summary(treatment=[1.0, 2.0], control=[5.0, 4.0])
    assert summary is not None
    assert summary["median_difference"] == -3.0


def _outcomes(pattern: str) -> list[bool]:
    """`"1"`/`"0"` per observation — keeps a paired table readable in a test."""

    return [character == "1" for character in pattern]


def test_paired_difference_is_narrower_than_treating_arms_as_independent() -> None:
    """The point of the paired method: identical arms are known to be identical.

    Ten tasks, both arms solving exactly the same three. Independent intervals
    on each arm would leave a wide window for the difference; the pairing says
    the arms never once disagreed, and the interval has to reflect that.
    """

    both = _outcomes("1110000000")
    paired = paired_binary_difference(treatment=both, control=both)
    assert paired is not None
    assert paired["difference"] == 0.0
    assert paired["discordant"] == {"treatment_only": 0, "control_only": 0}
    low, high = paired["difference_95"]
    assert -0.2 < low < 0.0 < high < 0.2

    independent = binary_measure_summary(treatment=both, control=both)
    control_low = independent["control"]["wilson_95"][0]
    treatment_high = independent["treatment"]["wilson_95"][1]
    assert high < treatment_high - control_low


def test_paired_difference_tracks_the_direction_of_the_discordant_pairs() -> None:
    treatment = _outcomes("1110000000")
    control = _outcomes("1111100000")
    paired = paired_binary_difference(treatment=treatment, control=control)
    assert paired is not None
    assert math.isclose(paired["difference"], -0.2)
    assert paired["discordant"] == {"treatment_only": 0, "control_only": 2}
    assert paired["difference_95"][0] < -0.2 < paired["difference_95"][1]
    assert paired["non_inferiority_95_lower"] > paired["difference_95"][0]


def test_paired_difference_needs_matched_observations() -> None:
    assert paired_binary_difference(treatment=[True], control=[]) is None
    assert paired_binary_difference(treatment=[True, False], control=[True]) is None


def test_paired_difference_survives_an_empty_margin() -> None:
    """Every pair a success in both arms: the phi term has a zero denominator."""

    both = _outcomes("1111")
    paired = paired_binary_difference(treatment=both, control=both)
    assert paired is not None
    assert paired["difference"] == 0.0
    assert paired["difference_95"][0] <= 0.0 <= paired["difference_95"][1]


def test_non_inferiority_needs_the_interval_to_clear_the_margin() -> None:
    identical = _outcomes("11100")
    verdict = non_inferiority_verdict(
        paired_binary_difference(treatment=identical, control=identical), 0.5
    )
    assert verdict["verdict"] == "non_inferior"
    assert verdict["margin"] == 0.5


def test_non_inferiority_uses_a_one_sided_95_percent_bound() -> None:
    """The registered alpha=.05 test must not silently become alpha=.025."""

    identical = _outcomes("1110000000")
    difference = paired_binary_difference(treatment=identical, control=identical)
    assert difference is not None
    assert difference["non_inferiority_95_lower"] > difference["difference_95"][0]

    margin_between_bounds = -(
        difference["non_inferiority_95_lower"] + difference["difference_95"][0]
    ) / 2
    verdict = non_inferiority_verdict(difference, margin_between_bounds)
    assert verdict["verdict"] == "non_inferior"
    assert verdict["non_inferiority_95_lower"] == difference["non_inferiority_95_lower"]


def test_an_underpowered_run_is_inconclusive_not_a_clean_bill_of_health() -> None:
    """The distinction the whole verdict exists for.

    Five tasks, arms agreeing perfectly, and a 5% margin. Nothing bad was
    observed — and the sample still cannot rule out a drop far larger than the
    margin. Reporting that as non-inferior is how an underpowered run becomes a
    claim.
    """

    identical = _outcomes("11100")
    verdict = non_inferiority_verdict(
        paired_binary_difference(treatment=identical, control=identical), 0.05
    )
    assert verdict["verdict"] == "inconclusive"


def test_a_clear_regression_is_reported_as_inferior() -> None:
    treatment = _outcomes("0000000000")
    control = _outcomes("1111111111")
    verdict = non_inferiority_verdict(
        paired_binary_difference(treatment=treatment, control=control), 0.1
    )
    assert verdict["verdict"] == "inferior"


def test_non_inferiority_without_a_difference_is_not_evaluable() -> None:
    verdict = non_inferiority_verdict(None, 0.1)
    assert verdict["verdict"] == "not_evaluable"


@pytest.mark.parametrize("margin", [0.0, 1.0, -0.1, 2.0])
def test_the_margin_must_be_a_rate(margin: float) -> None:
    with pytest.raises(ValueError):
        non_inferiority_verdict({"difference_95": [0.0, 0.0]}, margin)
