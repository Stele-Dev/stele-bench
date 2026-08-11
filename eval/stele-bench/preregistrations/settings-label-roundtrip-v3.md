# Pre-registration: settings-label-roundtrip v3

**Status:** frozen before any v3 paired Harbor model call. Do not edit after a
paid v3 result exists; append a dated results section or create a new version.

- Challenge: `settings-label-roundtrip`
- Corpus: DotState at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful-norag`
- Model: `anthropic/claude-haiku-4.5`
- Design: 3 paired attempts through `oracle.py run-repeat`; order alternates
- Limits: 60 turns and $4.00 requested Claude cap per arm; $24.00 maximum v3
  whole-repeat cap

## Apparatus-only revision from v2

V2 proved the hidden contract and mutation guard: all four completed workspaces
scored 1. It failed its validity floor because both controls crossed the $3.25
Claude ceiling during cold Cargo compilation and exited nonzero with background
tasks killed. The two treatments exited normally, but no v2 comparison is
admitted.

V3 changes only build setup and the spending rail:

1. the task image runs `cargo test --no-run --lib` after copying the pinned
   corpus, outside measured agent execution;
2. Harbor caches that behavior-neutral image layer for every arm;
3. the per-arm ceiling is $4.00, above every completed v1/v2 arm;
4. the cohort is three pairs, for a $24 worst-case cap.

V1 and v2 observed Gateway drain totals $4.64. V3's $24 maximum keeps the
cumulative maximum at $28.64, inside the user's original $30 approval. Earlier
rewards and metrics remain excluded and are never pooled with v3.

Prompt, hidden contract, source, fixture, graph, conditions, model, outcome
definitions, and analysis remain unchanged. The prebuild contains no challenge
answer or graph content and is applied identically to both arms.

## Question and mechanism

The task renames the Settings row `Check for Updates` to `Automatic Update
Checks`, preserves behavior, and adds focused regression coverage. DotState has
a latent contract in `src/screens/settings.rs`: `SettingItem::name()` produces
the visible label, while `SettingsScreen::apply_setting_by_name()` dispatches
behavior by matching the same string.

The faithful graph contains a verified lesson anchored to that file and those
symbols. Treatment uses `faithful-norag`, retaining graph and file-boundary
hooks while removing prompt-time Auto-RAG. The intended channel is
`nudge:recall-file` after the first edit to the anchored file. Control has no
graph or boundary hook.

A clean no-SteIe qualification first changed only `SettingItem::name()`,
missing the handler contract, but later self-recovered after 27 turns. This is
a guidance-on-edit test of whether verified memory changes correctness or work,
not a claim that the contract is unreachable from source.

## Deterministic oracle

The hidden external Rust contract requires:

1. `SettingItem::CheckForUpdates.name()` equals `Automatic Update Checks`;
2. applying option 1 with that string disables update checks;
3. applying option 0 with that string enables them again;
4. all library tests pass.

`reward = 1` only when all four hold. The untouched corpus scores 0. The
intended patch scores 1 with all 276 library tests passing. Certification must
pass before v3 creates a run.

## Measures and inference

Report separately:

1. **Mechanism:** treatment records `nudge:recall-file` with the expected
   display-string fact.
2. **Observable use:** after the nudge, treatment reads or updates
   `apply_setting_by_name()`, mentions the lockstep contract, or adds a test
   exercising the new label through the handler.
3. **Correctness:** deterministic reward.
4. **Work:** turns, agent execution time, trial wall time, input/output tokens,
   investigative and total tool calls, and authoritative Gateway cost.

The primary test is exact one-sided paired McNemar on reward. At N=3 no
nontrivial result reaches p <= 0.05, so all correctness numbers are preliminary.
Report every raw pair, Wilson 95% arm intervals, paired rate difference, and
treatment-minus-control differences for every work measure. Report medians but
make no significance claim.

## Predictions and decision rules

1. Treatment reward is not lower than control reward.
2. At least 2/3 treatments record the expected `nudge:recall-file`.
3. At least 2/3 treatments address the handler contract after their first edit
   and before hidden verification.
4. Treatment median paired differences favor treatment for investigative tool
   calls, total tool calls, agent time, and Gateway cost.
5. All 6 arms finish valid and non-truncated.

- If all arms are valid, mechanism holds, correctness is not worse, and the
  four efficiency medians favor treatment, publish the raw three-pair result as
  a case-level preliminary signal.
- If mechanism holds but correctness and efficiency tie, publish the null.
- If treatment correctness is worse, publish the regression; lower cost cannot
  rescue it.
- If mechanism fails, report mechanism failure and make no memory-value claim.
- If any arm is invalid, stop and report apparatus failure.

Negative and null outcomes remain publishable. Nothing is tuned after the
first v3 paired Harbor call.

## Spend fence and registered invocation

```text
oracle.py run-repeat settings-label-roundtrip-v3 \
  --attempts 3 \
  --control-condition no-stele \
  --treatment-condition faithful-norag \
  --source "$STELE_BENCH_SOURCE" \
  --revision a4a5986eafa496dee73a93141588cffaed559f80 \
  --fixture fixtures/dotstate-v1/fixture.json \
  --challenge settings-label-roundtrip \
  --model anthropic/claude-haiku-4.5 \
  --max-turns 60 \
  --max-budget-usd 4 \
  --acknowledge-paid-repeat-cap-usd 24
```

## Results and retirement (2026-08-11)

V3 stopped after its first control arm, before any treatment arm completed.
The warmed image removed cold compilation from measured execution, and the
control produced a correct workspace (`reward = 1`), but it still crossed the
$4 Claude ceiling: $4.0646125 reported cost, 61 total tool calls, and a nonzero
exit. The observed Gateway drain for this stopped cohort was $0.81.

Under the registered all-arms-valid rule, V3 is invalid and excluded from any
effect estimate. The result isolates the remaining apparatus issue as
clean-exit budget headroom rather than Rust build setup. V4 is the final attempt
and changes only that headroom and cohort size.
