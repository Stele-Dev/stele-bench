# Profile-selection compounding experiment v2

Status: locked before admission attempt 002. This version inherits
`profile-selection-compounding-v1.md` in full except for the two apparatus
corrections below.

## Why a new version exists

The single discarded v1 admission, `profile-selection-admission-001`, completed
all four model sessions with the pinned model, source, budgets, and distinct
conversation boundaries. It exposed two objectively false receipts:

1. Both session-1 agents correctly ended with the live popup path, but formatted
   the path as a Markdown code span. Literal matching treated the backticks as
   answer content and recorded both correct answers as incorrect.
2. Both session-2 agents edited the live popup implementation, but the frozen
   verifier could not execute either contract because the task image emitted
   `/tests/challenge-verifier.sh: 53: cargo: not found` and the same error at
   line 56. Both rewards were therefore infrastructure failures, not behavioral
   scores.

The v1 admission is discarded and never contributes to the cohort.

## Frozen corrections

Only these two changes are admitted:

1. Response-marker normalization removes Markdown backtick characters before
   the existing case-folded literal substring check. The required marker and
   correct path are unchanged.
2. The Harbor task image is pinned to
   `rust@sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89`
   and installs `cmake`, `make`, the complete `perl` runtime, and `pkg-config`,
   so the unchanged Rust verifier can compile and run.

No prompt, repository revision, model, session limit, budget, graph behavior,
verifier script, outcome rule, metric, or cohort size changes. In particular,
the treatment's failure to write a graph node in admission 001 is preserved as
a behavioral observation and is not used to tune Stele or the prompt.

## Next gate

Run exactly one new discarded admission pair. If it satisfies the v1 admission
criteria with these two corrections, run the unchanged fixed cohort of 10
paired chains. Otherwise apply the v1 stopping rules.
