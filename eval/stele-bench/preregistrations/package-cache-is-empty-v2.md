# Pre-registration: package-cache-is-empty v2

**Status:** frozen before any paid v2 model call. Do not edit after the paid run
starts; append a dated result section only.

- Challenge: `package-cache-is-empty`
- Corpus: DotState at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful`
- Model: `anthropic/claude-haiku-4.5`
- Design: one paired attempt, descriptive tare check
- Limits: 40 turns and $3.00 requested Claude cap per arm; $6.00 maximum pair
  cap

## Apparatus correction and purpose

V1's agents completed, but the mutation audit rejected both arms because
ordinary `cargo test` generated an undeclared top-level `Cargo.lock`. V2 changes
only the task environment: it warms `cargo test --no-run --lib` before the
baseline and permits `/workspace/Cargo.lock`, matching the already-validated
settings-label challenge. The prompt, hidden contract, source, fixture, graph,
conditions, model, and analysis remain unchanged. Do not pool v1 with v2.

This explicit, low-memory coding task should not benefit from project memory.
It measures fixed setup, tool-schema, hook, token, latency, and cost tare beside
the keypress value question.

## Predictions and stopping rule

1. Both arms finish valid and score 1.
2. Treatment shows no correctness benefit.
3. Report treatment-minus-control setup, token, wall-time, tool-call, and cost
   deltas as gross tare.

Run one pair only. Do not tune or rerun after observing it. Any invalid arm is
published as apparatus-only.

## Registered invocation

```text
oracle.py run-repeat package-cache-is-empty-v2 \
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

## Result, 2026-08-11

The one registered pair was valid and both arms earned full reward. On this
low-memory task, treatment added 83,520 input tokens and 21.71 seconds of agent
execution. See
[`../preliminary-results-2026-08-11.md`](../preliminary-results-2026-08-11.md)
and the durable record at
[`../results/package-cache-is-empty/2026-08-11-package-cache-is-empty-v2.json`](../results/package-cache-is-empty/2026-08-11-package-cache-is-empty-v2.json).
