# Pre-registration: profile-selection-live-path v1

**Committed before the first paid attempt. Do not edit after results exist;
append a dated results section or create a new pre-registration for a rerun.**

- Challenge: `profile-selection-live-path`
- Corpus: Dotstate at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful-norag`, the same frozen graph and boundary hooks as
  faithful, with prompt-time Auto-RAG removed
- Model: `anthropic/claude-haiku-4.5`
- Design: 5 paired attempts through `oracle.py run-repeat`; order alternates
- Limits: 20 turns and $0.50 requested Claude cap per arm; $5.00 acknowledged
  whole-repeat cap

## Question and mechanism

The user asks for Ctrl+U to clear the new-profile name in the profile-selection
flow shown after repository setup. The corpus contains two plausible targets:

- `src/screens/profile_selection.rs`, a complete 559-line controller whose
  module comment says it handles profile selection after setup, but which is
  not routed by the application;
- `src/components/profile_selection_popup.rs`, the component the application
  actually instantiates and dispatches to.

The frozen graph contains an active gap anchored to both paths that identifies
the first implementation as orphaned and the popup as live. Prompt-time
Auto-RAG is removed from the treatment so the intended memory channel is the
literal file-anchor recall emitted after an Edit or Write. The control has no
graph or boundary hook.

This is a guidance-on-edit test, not a pure knowledge quiz. The agent can infer
the live path by tracing `app.rs`; memory is not the only possible route to a
correct result. The claim is therefore a difference in successful behavior
under a fixed time/turn budget, not that the answer is unknowable from source.

## Deterministic oracle

The hidden verifier compiles an external Rust contract test against the public
`ProfileSelectionPopup` API. It:

1. opens the profile picker on an empty temporary repository;
2. types `draft-profile` into the create-name input;
3. sends Ctrl+U;
4. sends Enter and requires the picker to remain open, proving the name was
   cleared rather than submitted.

It then runs all library tests. `reward = 1` only when the behavioral contract
and the existing library suite both pass. Editing the orphaned screen alone
cannot satisfy the contract. The untouched pinned corpus has been verified to
score 0; the intended minimal live-popup change scores 1 with all 275 library
tests passing.

## Measures

Three layers are reported separately:

1. **Boundary trigger:** treatment `memory_evidence.injected = true` with
   `nudge:recall-file` in its channels and the live-path gap among the detected
   markers. This is mechanism evidence, not task success.
2. **Observable use, exploratory:** after the nudge, the trajectory reads or
   edits `src/components/profile_selection_popup.rs`, or explicitly corrects
   course from the orphaned screen to the popup. This will be inspected from
   retained trajectories and mutation paths, not inferred from reward alone.
3. **Behavior:** deterministic verifier `reward`.

The primary statistical test is the exact one-sided paired McNemar test on
reward, treatment greater than control. Arm Wilson 95% intervals and the
treatment-minus-control rate difference are descriptive. The independent-arm
Fisher value remains in receipts for continuity with earlier runs but is not
the design-matched test.

## Falsifiable predictions

Let `r(condition)` be the full-reward rate across five attempts.

1. **Primary:** `r(faithful-norag) > r(no-stele)` with exact one-sided McNemar
   p <= 0.05. At N=5 this requires all five discordant outcomes to favor the
   treatment; anything weaker is not established.
2. **Trigger:** at least 4/5 treatment attempts record `nudge:recall-file`.
3. **Task floor:** at least 8/10 arms finish with valid, non-truncated results.
   Invalid or truncated arms are excluded with their reason, never counted as
   behavioral failures.
4. **Cost and earliness:** token, wall-time, and tool-call differences are
   exploratory. No efficiency claim will be made from N=5.

## Decision rules

- Primary and trigger both hold: retain the question as the first working
  guidance-on-edit probe, then add a distinct anchored-risk question before
  generalizing beyond this case.
- Trigger holds but reward does not differ: boundary recall works, but this
  source task is easy enough to solve without it or the guidance did not change
  behavior. Report that null result; do not enlarge N after looking.
- Trigger is below 4/5: mechanism failure. Diagnose hook delivery before
  interpreting reward.
- Control is 5/5: retire the question as behaviorally saturated even if the
  treatment also passes. The source makes the live path too easy to recover.
- Fewer than 8 valid arms: apparatus/task failure. Fix the cause under a new
  pre-registration before drawing a memory conclusion.

## Known constraints

- The live-path fact is discoverable through repository-wide reference tracing,
  so this question does not satisfy the stronger "unreachable from source"
  standard. It deliberately tests whether just-in-time guidance changes a
  realistic implementation outcome.
- Failed-command lesson recall is not part of this cohort. Claude's current
  installed hook receives successful Bash `PostToolUse` events but no failed
  command result, so an error-trigger question would measure absent event
  wiring rather than recall quality. That channel must be instrumented before
  a command-error cohort is admissible.

## 2026-07-25 invalid pilot and budget amendment

The first paid sequence was stopped after five completed arms. None is admitted
to the registered analysis: three control arms reached Claude but terminated at
the $0.50 requested cap, and two treatment arms failed structural setup before
Claude started. The treatment failure was a verifier false positive: its broad
recursive search counted the benchmark log containing the probe's own literal
`stele hook auto-rag` command as live hook wiring.

Before any valid result was observed, a one-pair apparatus validation was run at
$2.00 per arm. The setup fix held, but both agents again terminated at the
requested cap before editing: Claude reported $2.16 and $2.12 while the Gateway
rate cross-checks were $0.43 and $0.42. This validation is also discarded.

The registered rerun cap is therefore $3.00 per arm, with a $30.00 acknowledged
whole-repeat ceiling. This changes only the spend rail. Model, turns, fixture,
conditions, attempt count, predictions, and decision rules remain fixed. The
invalid pilots are not pooled with the rerun.

## 2026-07-25 retirement after the $3 control

The registered rerun was stopped after its first control arm. It again reached
the requested cap (`$3.05` Claude-reported, `$0.61` Gateway cross-check) after
42 tool calls and did not finish. The retained trajectory shows that the agent
found `ProfileSelectionPopup` immediately, then began an over-broad global
keymap refactor across `keymap/actions.rs` and `keymap/presets.rs`.

That exposes a construct-validity failure in the question itself. The graph fact
under test addresses choosing the live popup instead of the orphaned screen, but
the observed failure was choosing a global keymap change instead of a focused
popup change. The first edits were also outside the fact's anchors, so boundary
recall could not fire. The frozen `KNOW-137` node anchors
`src/screens/profile_selection.rs`, `src/app.rs`, and `src/screens/mod.rs`; it
does not anchor `src/components/profile_selection_popup.rs`, contrary to the
earlier pre-registration description.

No behavioral or memory-effect conclusion is admissible from these runs. This
probe is retired rather than rerun at a higher cap. A replacement must prove in
an unpaid pilot that the likely wrong first edit hits the tested fact's anchor
and that the fact directly addresses the observed mistake.
