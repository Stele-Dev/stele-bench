# Class C overhead replication v2 results

Date: 2026-07-27

Run: `aider-overhead-replication-v2`

Registered outcome: **uninterpretable under the frozen validity rule**.

All 12 pairs ran. Ten were analysable, meeting the minimum of 10. Attempts 3
and 12 were retained as excluded and were not replaced. However, attempt 2 had
treatment-only budget-truncation risk: treatment used `$1.8990` of its `$2.00`
cap while control did not cross the 90% warning threshold. The pre-registration
states that any unbalanced budget-truncation risk makes the run
uninterpretable. The descriptive measurements below are therefore not promoted
to a benchmark claim.

## Descriptive measurements

- Input tokens: treatment minus control median `+51,838`, or `+52.53%`.
  Treatment used more input tokens in 8 of 10 analysable pairs.
- Trial wall time: treatment minus control median `+19.69s`, or `+19.56%`.
  Treatment took longer in 9 of 10 analysable pairs.
- Correctness: both arms solved 4 of 10 tasks (`40%`); paired difference `0`.
  One discordant pair favoured each arm. The paired 95% interval was
  `[-24.20pp, +24.20pp]`.
- The registered non-inferiority verdict was `inconclusive`; the sample does
  not establish that Stele costs no more than 10 percentage points of solve
  rate.
- Treatment invoked Stele tools 23 times across 4 of 12 tasks and persisted an
  agent-authored graph node in 1 of 12 tasks.

## Registered predictions

1. The input-token replication target did not pass its frozen bar. It required
   treatment to be higher in at least 10 of 12 analysable pairs; the observed
   count was 8 of 10, although the median direction was higher.
2. The wall-time direction held descriptively: treatment was slower in 9 of 10
   analysable pairs.
3. The solve-rate point estimate was not lower in treatment, but the interval
   is too wide for a safety or non-inferiority claim.

The honest reading is a substantial directional passive-overhead signal with
no observed point-estimate correctness loss, inside a cohort whose registered
validity rule prevents treating that signal as confirmed evidence. No task is
rerun and no limit is changed from this result.
