# Pre-registration: input-keypress-architecture v3

**Status:** frozen before any paid v3 model call. Do not edit after the paid run
starts; append a dated result section only.

- Challenge: `input-keypress-architecture`
- Corpus: DotState at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful` with prompt-time recall enabled
- Model: `anthropic/claude-sonnet-4.5`
- Design: one final paired attempt, control then treatment
- Limits: 60 turns and $5.00 requested Claude cap per arm; $10.00 maximum pair
  cap
- Verifier SHA-256:
  `61dd13e360344100f9b4e3d38ed3be35da81709b9f887f54aabefa231f2b0860`

## Final apparatus corrections

V3 changes only two defects observed in v2:

1. the neutral prompt now explicitly forbids `.git` and repository-history
   inspection, which both v2 arms attempted and the corpus audit rejects;
2. the lexical verifier accepts the bounded correct paraphrases `app-level`,
   `event handler`, `eat`, `interfere`, and `fire`. A regression case based on
   the v2 faithful answer scores all five points.

The question, source, fixture, graph, conditions, model, budgets, and analysis
remain unchanged. V1 and v2 remain frozen and apparatus-only. Do not pool them
with v3.

## Measures and predictions

Report deterministic reward, injection evidence, investigative and total calls,
agent and wall time, token classes, authoritative Gateway cost, and first-request
context delta from raw JSONL.

1. Both arms finish valid, non-truncated, and score 1.
2. Treatment receives the registered focus/keymap cluster before acting.
3. Treatment uses fewer investigative calls and less agent time than control.
4. Gross token and cost deltas are reported even if fixed Stele tare outweighs
   saved investigation.

Run one pair only and publish the raw result. This is the final admission run;
do not tune or rerun after observing it. Any invalid arm makes v3
apparatus-only.

## Registered invocation

```text
oracle.py run-repeat input-keypress-architecture-v3 \
  --attempts 1 \
  --control-condition no-stele \
  --treatment-condition faithful \
  --source "$STELE_BENCH_SOURCE" \
  --revision a4a5986eafa496dee73a93141588cffaed559f80 \
  --fixture fixtures/dotstate-v1/fixture.json \
  --challenge input-keypress-architecture \
  --model anthropic/claude-sonnet-4.5 \
  --max-turns 60 \
  --max-budget-usd 5 \
  --acknowledge-paid-repeat-cap-usd 10
```

## Result, 2026-08-11

The one registered pair was valid and both arms earned full reward. Treatment
used 11 fewer investigative calls, 1,292,046 fewer input tokens, and 86.87 fewer
agent-execution seconds. See
[`../preliminary-results-2026-08-11.md`](../preliminary-results-2026-08-11.md)
and the durable record at
[`../results/input-keypress-architecture/2026-08-11-input-keypress-architecture-v3.json`](../results/input-keypress-architecture/2026-08-11-input-keypress-architecture-v3.json).
