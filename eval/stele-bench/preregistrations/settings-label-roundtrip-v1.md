# Pre-registration: settings-label-roundtrip v1

**Status:** frozen before any paired Harbor model call. Do not edit after a
paid result exists; append a dated results section or create a new version.

- Challenge: `settings-label-roundtrip`
- Corpus: DotState at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful-norag`
- Model: `anthropic/claude-haiku-4.5`
- Design: 5 paired attempts through `oracle.py run-repeat`; order alternates
- Limits: 60 turns and $3.00 requested Claude cap per arm; $30.00 maximum
  whole-repeat cap, requiring separate explicit approval before execution

## Question and mechanism

The user asks the agent to rename the Settings row `Check for Updates` to
`Automatic Update Checks`, preserve behavior, and add focused regression
coverage. DotState implements a latent string contract in
`src/screens/settings.rs`: `SettingItem::name()` produces the visible label,
while `SettingsScreen::apply_setting_by_name()` dispatches behavior by matching
that same string.

The frozen graph contains a verified lesson anchored to
`src/screens/settings.rs` and the relevant symbols. It states that these two
functions must remain in lockstep because changing only the display string
silently breaks toggling. The treatment uses `faithful-norag`, which keeps the
same graph and file-boundary hooks while removing prompt-time Auto-RAG. The
intended memory channel is therefore the literal `nudge:recall-file` emitted
after the agent first edits the anchored file. The control has no graph or
boundary hook.

This is a guidance-on-edit test. The source contains enough information for an
agent to recover by reading the full file or writing a round-trip test. The
claim is not that the contract is unreachable from source. The question is
whether verified project memory delivered at the moment of editing changes the
trajectory, correctness, or work required.

## Qualification run

Before freezing this document, one clean no-SteIe Claude Haiku 4.5 trajectory
ran on a detached checkout with safe mode, no MCP servers, no project or user
customizations, and the exact challenge prompt. This run is a challenge
qualification only and is excluded from the paired analysis.

The trajectory passed the admission gate:

1. its first edit was to the fact's exact anchor,
   `src/screens/settings.rs`;
2. that edit changed only `SettingItem::name()` and initially missed
   `SettingsScreen::apply_setting_by_name()`;
3. the agent later recovered, updated both strings, added round-trip tests, and
   passed the full suite after 27 turns;
4. direct Claude usage reported $0.1953931.

This establishes that the fact addresses a mistake the control naturally makes
at the first edit. It also shows that control success can saturate, so efficiency
measures are preregistered below rather than introduced after results.

## Deterministic oracle

The hidden verifier writes an external Rust contract test and then removes it.
The contract:

1. requires `SettingItem::CheckForUpdates.name()` to equal
   `Automatic Update Checks`;
2. applies option 1 using that new string and requires update checks to become
   disabled;
3. applies option 0 using that new string and requires update checks to become
   enabled again;
4. runs all library tests.

`reward = 1` only when the label, both behavior transitions, and the library
suite pass. The untouched pinned corpus scores 0 because the old label remains,
while the intended lockstep change scores 1 with all 276 library tests passing.
The challenge also passes zero-model certification against the pinned external
repository and frozen fixture.

## Measures

Report these layers separately:

1. **Mechanism:** treatment `memory_evidence.injected = true`, with
   `nudge:recall-file` in its channels and the display-string contract among
   detected markers.
2. **Observable use:** after the nudge, the treatment reads or updates
   `SettingsScreen::apply_setting_by_name()`, mentions the lockstep string
   contract, or adds a test that exercises the new label through the handler.
   This is inspected from retained trajectories and mutations, not inferred
   from reward.
3. **Correctness:** deterministic verifier reward.
4. **Work required:** valid-arm turns, wall time, input tokens, output tokens,
   and authoritative Gateway cost.

The primary statistical test is the exact one-sided paired McNemar test on
reward, treatment greater than control. Arm Wilson 95% intervals and the paired
treatment-minus-control rate difference are descriptive.

Because the qualification control eventually succeeded, efficiency is a
preregistered secondary family. For each valid pair, report treatment minus
control for turns, wall time, input tokens, and Gateway cost, together with the
median paired difference and every raw pair. At N=5 these are preliminary
descriptive estimates only. No significance or general efficiency claim will
be made from them.

## Falsifiable predictions

Let `r(condition)` be the full-reward rate across five attempts.

1. **Correctness:** `r(faithful-norag) >= r(no-stele)`. A strict positive
   correctness effect is established only if exact one-sided McNemar p <= 0.05.
2. **Trigger:** at least 4/5 valid treatment attempts record
   `nudge:recall-file` with the expected fact.
3. **Trajectory:** at least 4/5 valid treatment attempts address
   `apply_setting_by_name()` after the first anchored edit and before a failing
   hidden verifier result.
4. **Efficiency direction:** the treatment median paired difference is below
   zero for turns, input tokens, wall time, and Gateway cost. These remain
   preliminary descriptive outcomes regardless of direction.
5. **Task floor:** at least 8/10 arms finish with valid, non-truncated receipts.
   Invalid arms are excluded with their reason and never counted as behavioral
   failures.

## Decision rules

- Trigger holds, correctness is not worse, and all four median efficiency
  differences favor treatment: publish the raw five-pair preliminary result as
  evidence that file-anchored project memory can reduce work on this case.
- Trigger and trajectory hold but reward and efficiency tie: publish a null
  result and do not tune or enlarge the cohort after looking.
- Treatment correctness is lower than control: publish the regression and
  inspect whether guidance distracted the agent; do not reinterpret lower cost
  as a benefit.
- Trigger is below 4/5: treat the cohort as mechanism failure and do not infer
  memory value from reward or cost.
- Fewer than 8 valid arms finish: treat the cohort as apparatus failure and run
  only under a new preregistration after fixing the cause.

All negative and null outcomes remain publishable preliminary numbers. The
challenge, fixture, conditions, model, sample size, outcomes, and analysis are
not changed after the first paired Harbor model call.

## Spend and execution fence

Historical paired runs showed that Claude's internal reported cost can reach a
requested ceiling well before the authoritative Gateway charge does. The
$3.00 per-arm ceiling is retained to avoid treatment-only truncation, not as an
expected spend. The qualification run suggests materially lower actual use,
but the runner must reserve and acknowledge the full $30.00 worst-case cap.

The paid cohort must not start until the user explicitly approves that $30.00
cap and the dedicated Gateway key passes the external reserve fence. The exact
registered invocation is:

```text
oracle.py run-repeat settings-label-roundtrip-v1 \
  --attempts 5 \
  --control-condition no-stele \
  --treatment-condition faithful-norag \
  --source "$STELE_BENCH_SOURCE" \
  --revision a4a5986eafa496dee73a93141588cffaed559f80 \
  --fixture fixtures/dotstate-v1/fixture.json \
  --challenge settings-label-roundtrip \
  --model anthropic/claude-haiku-4.5 \
  --max-turns 60 \
  --max-budget-usd 3 \
  --acknowledge-paid-repeat-cap-usd 30
```

## 2026-08-11 invalid cohort and retirement

The v1 runner was interrupted after two completed pairs and before attempt 3
settled. No v1 arm is admitted to a product-effect analysis.

Two arms exceeded the requested Claude ceiling: attempt 1 treatment reported
$3.08729925 and attempt 2 control reported $3.05694375. More importantly, the
two arms that exited normally were deterministically zeroed by an invalid
mutation fence. The treatment verifier recorded `only_in_workspace =
["Cargo.lock"]`; the control recorded `only_in_workspace = ["Cargo.lock",
"tests/test_settings_label.rs"]`. The pinned corpus has no Cargo lockfile, so
running Cargo creates one. The prompt explicitly requires focused regression
coverage, so a file under `tests/` is also a valid task output. Neither path was
listed in v1's allowed outputs.

This is an apparatus false negative, not a behavioral result. The observed
Gateway drain was $2.21 when the run was stopped. V1 is retired. Any rerun must
use a new preregistration, admit those normal outputs, and use a ceiling above
the observed $3.0873 truncation.
