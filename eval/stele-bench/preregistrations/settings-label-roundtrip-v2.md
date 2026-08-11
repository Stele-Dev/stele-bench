# Pre-registration: settings-label-roundtrip v2

**Status:** frozen before any v2 paired Harbor model call. Do not edit after a
paid v2 result exists; append a dated results section or create a new version.

- Challenge: `settings-label-roundtrip`
- Corpus: DotState at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful-norag`
- Model: `anthropic/claude-haiku-4.5`
- Design: 4 paired attempts through `oracle.py run-repeat`; order alternates
- Limits: 60 turns and $3.25 requested Claude cap per arm; $26.00 maximum v2
  whole-repeat cap

## Apparatus-only revision from v1

V1 was interrupted after two pairs because normal task outputs were absent
from its mutation allowlist. Running Cargo created `Cargo.lock`, and one agent
placed the requested regression coverage under `tests/`. The mutation guard
zeroed those arms even though both paths are normal consequences of the prompt.
V1 also produced two nonzero agent exits at a $3.00 ceiling, with the larger
reported value at $3.08729925.

V2 changes only the apparatus and spending rail:

1. `/workspace/Cargo.lock` and `/workspace/tests` are valid output paths;
2. the per-arm ceiling is $3.25, above every completed v1 arm;
3. the cohort is four pairs, keeping the new $26 worst-case cap plus v1's $2.21
   observed spend within the user's original $30 approval.

Challenge prompt, hidden behavioral contract, pinned source, fixture, graph,
conditions, model, outcomes, and analysis remain unchanged. V1 rewards and
metrics are excluded. The v1 false negatives are never pooled with v2.

## Question and mechanism

The task renames the Settings row `Check for Updates` to `Automatic Update
Checks`, preserves behavior, and adds focused regression coverage. DotState has
a latent string contract in `src/screens/settings.rs`: `SettingItem::name()`
produces the visible label, while `SettingsScreen::apply_setting_by_name()`
dispatches behavior by matching the same string.

The frozen graph contains a verified lesson anchored to that file and those
symbols. Treatment uses `faithful-norag`, retaining graph and file-boundary
hooks while removing prompt-time Auto-RAG. The intended channel is the literal
`nudge:recall-file` after the first edit to the anchored file. Control has no
graph or boundary hook.

This is a guidance-on-edit test, not an unreachable-from-source quiz. A clean
no-SteIe qualification first changed only `SettingItem::name()`, missing the
handler contract, but later self-recovered after 27 turns. The benchmark asks
whether verified project memory delivered at edit time changes correctness or
the work required.

## Deterministic oracle

The hidden external Rust contract requires:

1. `SettingItem::CheckForUpdates.name()` equals `Automatic Update Checks`;
2. applying option 1 with that string disables update checks;
3. applying option 0 with that string enables them again;
4. all library tests pass.

`reward = 1` only when all four conditions hold. The untouched pinned corpus
scores 0. The intended lockstep patch scores 1 with all 276 library tests
passing. The packaged source, evidence corpus, faithful graph, and challenge
must pass zero-model certification before v2 creates a run.

## Measures and inference

Report separately:

1. **Mechanism:** treatment records `nudge:recall-file` and detects the expected
   display-string contract.
2. **Observable use:** after the nudge, treatment reads or updates
   `apply_setting_by_name()`, mentions the lockstep contract, or adds a test
   exercising the new label through the handler.
3. **Correctness:** deterministic reward.
4. **Work required:** valid-arm turns, wall time, input/output tokens,
   investigative and total tool calls, and authoritative Gateway cost.

The primary statistical test is exact one-sided paired McNemar on reward,
treatment greater than control. Wilson 95% arm intervals and the paired rate
difference are descriptive. At N=4, no nontrivial correctness result can reach
p <= 0.05; all correctness numbers are explicitly preliminary.

For every valid pair, report treatment minus control for turns, wall time,
input/output tokens, tool calls, and Gateway cost. Report every raw pair and the
median paired difference. These efficiency results are descriptive only.

## Predictions and decision rules

1. Treatment reward is not lower than control reward.
2. At least 3/4 valid treatments record `nudge:recall-file` with the expected
   fact.
3. At least 3/4 valid treatments address `apply_setting_by_name()` after the
   first anchored edit and before a failing hidden verifier result.
4. Treatment median paired differences favor treatment for investigative tool
   calls, total tool calls, wall time, and Gateway cost.
5. At least 7/8 arms finish valid and non-truncated. Invalid arms are excluded
   with their reason and never counted as behavioral failures.

- If mechanism holds, correctness is not worse, and the four efficiency
  medians favor treatment, publish the raw four-pair preliminary result as a
  case-level efficiency signal.
- If mechanism holds but correctness and efficiency tie, publish the null.
- If treatment correctness is worse, publish the regression; lower cost cannot
  rescue it.
- If fewer than 3/4 treatments receive the expected nudge, report mechanism
  failure and make no memory-value claim.
- If fewer than 7 arms are valid, report apparatus failure and stop.

No challenge, fixture, condition, outcome, or analysis changes after the first
v2 paired Harbor call. Negative and null outcomes remain publishable.

## Spend fence and registered invocation

V1's observed Gateway drain was $2.21. V2's acknowledged worst-case cap is
$26.00, leaving the combined maximum within the original $30 approval. The
dedicated key and shared reserve fence must pass immediately before the run.

```text
oracle.py run-repeat settings-label-roundtrip-v2 \
  --attempts 4 \
  --control-condition no-stele \
  --treatment-condition faithful-norag \
  --source "$STELE_BENCH_SOURCE" \
  --revision a4a5986eafa496dee73a93141588cffaed559f80 \
  --fixture fixtures/dotstate-v1/fixture.json \
  --challenge settings-label-roundtrip \
  --model anthropic/claude-haiku-4.5 \
  --max-turns 60 \
  --max-budget-usd 3.25 \
  --acknowledge-paid-repeat-cap-usd 26
```

## 2026-08-11 invalid cohort and retirement

V2 was interrupted after two completed pairs because its validity floor became
impossible. All four completed workspaces passed the hidden verifier with
`reward = 1`, proving that the corrected mutation paths removed v1's false
negative. Both no-SteIe controls nevertheless exited nonzero and were invalid.

The controls reported $3.28287075 and $3.31817050 against the requested $3.25
ceiling. Their retained streams show long cold Cargo compiles moved into
background tasks; when the ceiling ended Claude, those tasks were killed and
Harbor recorded `NonZeroAgentExitCodeError`. Both faithful-norag treatments
exited normally and scored 1. The apparent asymmetry is not an admissible
memory-effect result because the registered 7/8 valid-arm floor failed after
only four arms.

V2's observed Gateway drain was $2.43. V2 is retired and its rewards and
efficiency metrics are excluded. A later run must prebuild Rust dependencies
outside agent execution, prove the warm image through preflight, and use a
ceiling above the observed $3.3182 value.
