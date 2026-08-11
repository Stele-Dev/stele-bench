# Class C overhead replication on aider-polyglot v2

Status: locked on 2026-07-27 before any replication arm ran.

This is an independent fixed-N replication of the descriptive overhead signal
observed in the discarded sizing pilot documented by
`aider-polyglot-overhead-v1.md`. Pilot observations are not pooled into this
cohort. This replication is a result, not another sizing stage, and it does not
make a powered non-inferiority claim.

## Question

On ordinary coding tasks with no useful project memory, what token, wall-time,
and correctness cost does a fresh Stele installation add relative to the same
agent without Stele?

## Frozen inputs

- Sanitised dataset: `aider-polyglot-clean`
- Sanitisation receipt SHA-256:
  `1f30ed63820d6091cf9dc33386e5a0e739768836a909e731e4afb96abeeaf400`
- Workspace SHA-256:
  `5bc6e597395069cbaddd96306a72bb96f335d9fd39520be8e5127e0950934530`
- Tests SHA-256:
  `6db459c139ee97afe25b1fc64fd1bf3120dc9f093c7e449b416771de72390225`
- Model: `anthropic/claude-haiku-4.5`
- Control: `no-stele`
- Treatment: `stele-fresh`
- Run shape: the existing `oracle.py run-suite`, unchanged
- Fixed cohort: 12 paired tasks
- Per-arm requested cap: `$2.00`
- Whole-cohort worst-case cap: `$48.00`
- Per-arm Harbor timeout: 2700 seconds

The dataset's own verifier grades both arms. No Stele-authored task, prompt, or
validator contributes to correctness.

## Frozen task selection

Within each language, tasks are sorted lexicographically. The v1 pilot used the
first and middle task. This independent replication uses the second and the
task immediately after the middle. The resulting tasks were written here
before the run:

```
polyglot_cpp_allergies                 polyglot_cpp_linked-list
polyglot_go_beer-song                  polyglot_go_palindrome-products
polyglot_java_all-your-base            polyglot_java_poker
polyglot_javascript_alphametics        polyglot_javascript_promises
polyglot_python_beer-song              polyglot_python_poker
polyglot_rust_acronym                  polyglot_rust_ocr-numbers
```

The command order is the row order above, left then right. The suite runner's
existing counterbalancing applies: control first on odd pairs and treatment
first on even pairs.

## Measures and predictions

Primary descriptive measure: paired treatment-minus-control input tokens per
task. Report every difference, the median absolute difference, the median
percentage difference, and how many analysable pairs favour each arm.

Secondary descriptive measures:

1. paired treatment-minus-control trial wall time;
2. paired treatment-minus-control output tokens and reported cost;
3. solve rate by arm and the paired treatment-minus-control difference;
4. treatment graph uptake and Stele tool calls.

The pre-run predictions are:

1. treatment input tokens are higher in at least 10 of 12 analysable pairs and
   their median paired difference is above zero;
2. treatment wall time is higher in a majority of analysable pairs;
3. treatment solve rate is not more than 10 percentage points below control.

Prediction 1 is the replication target. Prediction 3 is a regression screen,
not a non-inferiority test at this N. No p-value or safety claim is made from
this cohort.

## Validity and stopping rules

1. Run all 12 frozen pairs without looking at intermediate outcomes.
2. Do not add, remove, replace, or rerun a task based on its result.
3. Retain invalid pairs with their reasons. Fewer than 10 analysable pairs
   makes the replication inconclusive.
4. Any oracle-payload access, dataset digest change, differential network
   access, or unbalanced budget-truncation risk makes the run uninterpretable.
5. Infrastructure validity and settled billing may stop the run for safety;
   partial outcomes are not a benchmark result.
6. No prompt, task, model, arm, runner, validator, budget, metric, or analysis
   change is permitted after the first arm begins.

The result is reported whether the signal replicates, disappears, or reverses.
