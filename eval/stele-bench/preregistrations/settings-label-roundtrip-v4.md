# Pre-registration: settings-label-roundtrip v4

**Status:** frozen before any v4 paired Harbor model call. Do not edit after a
paid v4 result exists; append a dated results section only.

- Challenge: `settings-label-roundtrip`
- Corpus: DotState at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful-norag`
- Model: `anthropic/claude-haiku-4.5`
- Design: 2 paired attempts through `oracle.py run-repeat`; order alternates
- Limits: 60 turns and $5.00 requested Claude cap per arm; $20.00 maximum v4
  whole-repeat cap

## Final apparatus revision

V3's first warmed control solved the task but crossed its $4 ceiling before a
clean exit. V4 changes only clean-exit headroom and cohort size:

1. raise the per-arm ceiling from $4 to the runner's $5 hard maximum;
2. run two pairs, setting a $20 whole-repeat ceiling;
3. reuse the already-certified challenge and behavior-neutral warm image.

No prompt, source, revision, fixture, graph, condition, model, hidden verifier,
outcome, or analysis rule changes. Observed Gateway drain across v1-v3 was
$5.45. V4's $20 maximum gives a cumulative maximum of $25.45, inside the
approved $30 ceiling. All earlier cohorts remain excluded.

This is the final settings-label attempt. If any v4 arm is invalid, no further
budget increase or rerun is permitted; publish the apparatus-only record.

## Question, mechanism, and oracle

The task renames the Settings row `Check for Updates` to `Automatic Update
Checks`, preserves behavior, and adds focused regression coverage. DotState has
a latent string contract in `src/screens/settings.rs`: `SettingItem::name()`
produces the label and `SettingsScreen::apply_setting_by_name()` dispatches on
that same label.

The faithful graph contains the verified contract anchored to that file and
those symbols. Treatment keeps file-boundary recall hooks but removes prompt-time
Auto-RAG. The intended mechanism is `nudge:recall-file` after the first edit.
Control has no graph or boundary hook.

The deterministic hidden oracle requires the new label, both toggle directions
through the new label, and all library tests. `reward = 1` only when all hold.
The untouched corpus scores 0; the intended patch scores 1 with 276 library
tests passing.

## Measures and inference

Report every raw pair and separate:

1. mechanism and observable use of the recalled contract;
2. deterministic reward;
3. turns, agent time, wall time, tokens, investigative and total tool calls;
4. authoritative Gateway cost.

Report arm rates with Wilson 95% intervals, paired reward difference, and
treatment-minus-control work differences. N=2 is descriptive and preliminary;
make no significance claim.

## Predictions and decision rules

1. Both treatment arms record the expected file-recall nudge.
2. Both treatment arms address the handler contract after their first edit.
3. Treatment reward is not lower than control reward.
4. Treatment median paired differences favor it on investigative calls, total
   calls, agent time, and Gateway cost.
5. All four arms finish valid and non-truncated.

- If all arms are valid, publish raw results, including null or negative results.
- If mechanism fails, make no memory-value claim.
- If any arm is invalid, stop and publish apparatus-only results.
- Never pool v1-v3 outcomes with v4.

## Spend fence and registered invocation

```text
oracle.py run-repeat settings-label-roundtrip-v4 \
  --attempts 2 \
  --control-condition no-stele \
  --treatment-condition faithful-norag \
  --source "$STELE_BENCH_SOURCE" \
  --revision a4a5986eafa496dee73a93141588cffaed559f80 \
  --fixture fixtures/dotstate-v1/fixture.json \
  --challenge settings-label-roundtrip \
  --model anthropic/claude-haiku-4.5 \
  --max-turns 60 \
  --max-budget-usd 5 \
  --acknowledge-paid-repeat-cap-usd 20
```

## Result (2026-08-11)

All four arms were valid and scored 1. The preliminary correctness difference
is 0/2 pairs; exact one-sided McNemar `p = 1.0`. The file-boundary reminder
surfaced the registered `KNOW-209` contract in both treatment arms after their
first edit, and both agents acted on it. However, both had already found or
identified the duplicate string contract from source, so the run does not show
an incremental correctness benefit.

Efficiency was mixed. Treatment's paired median difference was -0.5
investigative calls, but +2.5 total calls, +22.44 seconds agent time, +33.08
seconds trial wall time, and +$0.205 authoritative Gateway cost. The complete
result is in `settings-label-roundtrip-v4-results-2026-08-11.md`; the durable
machine record is
`results/settings-label-roundtrip/2026-08-11-settings-label-roundtrip-v4.json`.
