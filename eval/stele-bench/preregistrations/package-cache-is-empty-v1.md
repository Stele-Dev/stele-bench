# Pre-registration: package-cache-is-empty v1

**Status:** frozen before any paid v1 model call. Do not edit after a paid run
starts; append a dated result section only.

- Challenge: `package-cache-is-empty`
- Corpus: DotState at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful`
- Model: `anthropic/claude-haiku-4.5`
- Design: 1 paired attempt, descriptive tare check
- Limits: 40 turns and $3.00 requested Claude cap per arm; $6.00 maximum pair
  cap

## Question and purpose

The task adds a small `PackageCacheData::is_empty` query and focused tests. Its
contract is explicit in the prompt and should not require project memory. It is
included as a low-memory coding control so the same artifact's setup, tool
schema, hook, token, latency, and cost overhead are visible beside the keypress
investigation. It is not a candidate Stele-win question.

The hidden verifier requires the method to depend only on whether `entries` is
empty, independent of schema version, and requires the library suite to pass.

## Predictions and stopping rule

1. Both arms score 1.
2. Treatment shows no correctness benefit.
3. Any treatment-minus-control token, time, or cost increase is reported as
   gross tare, not explained away as agent variance.
4. The pair finishes valid and non-truncated.

Run one pair only. Do not tune or rerun after observing it. If either arm is
invalid, publish apparatus-only.

## Registered invocation

```text
oracle.py run-repeat package-cache-is-empty-v1 \
  --attempts 1 \
  --control-condition no-stele \
  --treatment-condition faithful \
  --source "$STELE_BENCH_SOURCE" \
  --revision a4a5986eafa496dee73a93141588cffaed559f80 \
  --fixture fixtures/dotstate-v1/fixture.json \
  --challenge package-cache-is-empty \
  --model anthropic/claude-haiku-4.5 \
  --max-turns 40 \
  --max-budget-usd 3 \
  --acknowledge-paid-repeat-cap-usd 6
```

## Result (2026-08-11)

V1 is apparatus-only. Both agents completed normally, but both arms scored 0
because ordinary `cargo test` created a top-level `Cargo.lock` that the mutation
audit had not allowed. The hidden contract tests never ran. This is a stale
challenge contract, not a Stele or agent correctness result.

The raw run still exposes the fixed tare descriptively: treatment added 63,895
input tokens, 9.57 seconds of trial wall time, and $0.052 by the Gateway-rate
cross-check while agent execution time was effectively tied (-0.54 seconds).
These figures remain non-authoritative until a corrected control produces valid
arms. V2 must prebuild the Rust library, allow the generated lockfile, and must
not pool V1.
