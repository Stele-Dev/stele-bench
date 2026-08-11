# Pre-registration: profile-selection compounding v1

**This document is locked before implementation and before any paid admission
run. Do not edit it after results exist. Record deviations and results in a new
dated document.**

## Purpose

This experiment asks one product-level question:

> Does the cost of building project memory in one session pay for itself in the
> first related session?

It measures the live Stele loop: natural intake in session 1, then natural
delivery and use in a fresh session 2. It does not attempt to estimate the full
multi-session crossover curve. The answer from this cohort is only "by the
first reuse" or "not by the first reuse."

## Fixed cohort

- Corpus: Dotstate at `a4a5986eafa496dee73a93141588cffaed559f80`
- Harness: Claude Code, with its version recorded in every receipt
- Model: `anthropic/claude-sonnet-4.6`
- Attempts after admission: 10 paired two-session chains
- Conditions: `no-stele` control and `stele-fresh` treatment
- Limit per agent session: 45 turns, 900 seconds, and a requested $10.00 cap
- Maximum requested cohort cap: $400.00, with authoritative Gateway cost
  reported separately from the requested ceiling

The full model identifier returned by the provider must be the same in every
admitted session. There is no fallback model. Whole-chain order alternates by
pair: control first on odd pairs and treatment first on even pairs. Within each
arm, session 1 always completes before session 2 starts.

## Session topology

Each attempt contains two independent chains.

### Control chain

1. Session 1 runs without Stele.
2. Session 2 starts as a new Claude Code conversation with no session-1
   transcript, summary, or memory.

### Treatment chain

1. Session 1 starts with shipped Stele installed against a fresh project with
   no prior user history. The normal product-created starter nodes remain; this
   is the real day-one state, not a hand-built empty approximation.
2. Session 2 starts as a new Claude Code conversation against the same project
   graph after session 1. It receives only the context and tools the shipped
   product normally provides.

Session 1 is read-only. Session 2 in both arms starts from an identical clean
copy of the pinned corpus. Code changes, generated files, build artifacts, and
conversation state from session 1 never carry forward. The treatment graph is
the only state allowed to cross the session boundary.

## Session 1: natural intake

Exact user prompt:

> Investigate why `ProfileSelectionScreen` exists alongside
> `ProfileSelectionPopup`. Determine which implementation is live in the
> profile-selection flow shown after repository setup, explain the
> relationship, and do not change code. End your answer with
> `Live implementation: <path>`.

Both arms receive the same prompt and source tree. The prompt does not mention
Stele, ask for a task or knowledge node, prescribe what should be saved, or
name an anchor. Any graph write must result from the shipped doctrine and the
agent's own judgment. No benchmark-authored extraction or cleanup follows the
session.

Session-1 investigation correctness is graded deterministically: the final
answer must contain
`Live implementation: src/components/profile_selection_popup.rs`. The source
tree must remain byte-identical to its initial state outside ignored runtime
caches. The treatment graph is snapshotted immediately before and after the
session.

Graph intake is reported at three levels, none substituted for another:

1. **Written:** session 1 created at least one agent-authored graph node beyond
   the normal starter nodes.
2. **Relevant:** a created active node contains or anchors the live popup path
   or `ProfileSelectionPopup`, and also identifies
   `ProfileSelectionScreen` or `src/screens/profile_selection.rs` as the
   alternative it was distinguished from.
3. **Deliverable:** that session-1-created node is injected or returned by a
   graph read in session 2.

If the treatment writes nothing, or writes nothing relevant, that is an
admitted intake failure and a product finding. It is not grounds to change the
prompt or teach the runner what to write.

## Session 2: first reuse

Exact user prompt:

> In the profile-selection flow shown after repository setup, make Ctrl+U
> clear the new-profile name currently being typed without closing the picker.
> Add focused unit coverage and do not change unrelated behavior.

The deterministic oracle is the already-frozen verifier at
`challenges/profile-selection-live-path/verifier.sh`. It compiles an external
contract test against `ProfileSelectionPopup`, exercises typed input followed
by Ctrl+U and Enter, and then runs the library tests. Reward is 1 only when the
live popup behavior and the existing library suite both pass.

The retired guidance-on-edit experiment does not contribute results to this
cohort. Here the live-path fact is produced naturally by the immediately prior
investigation. Anchor recall is not singled out as the mechanism under test;
any shipped delivery path may carry the memory.

## Measures

Correctness and efficiency remain separate.

### Primary

- Session-2 deterministic reward by arm and paired difference
- Cumulative authoritative Gateway cost across sessions 1 and 2 by arm and
  paired difference
- Cumulative wall time across sessions 1 and 2 by arm and paired difference

### Mechanism and diagnosis

- Session-1 investigation correctness
- Written, relevant, and deliverable graph-intake rates
- Whether session-1 memory was injected, explicitly retrieved, or both in
  session 2
- Session-2 investigative tool calls before the first edit
- Tool-call index and elapsed time of the first edit to
  `src/components/profile_selection_popup.rs`
- Session-specific and cumulative input, output, cache-read, and cache-write
  tokens
- Session-specific and cumulative tool calls, wall time, and authoritative
  Gateway cost
- Claude-reported cost as a cross-check, never as the monetary authority

For each continuous paired difference, report all 10 values, the mean, median,
and a paired bootstrap 95% interval. At N=10 these are estimates with wide
uncertainty, not a general performance claim. Reward rates receive Wilson 95%
intervals; paired discordant rewards receive an exact two-sided McNemar value.

## Decision rule

"Payback by first reuse" is **observed** only when:

1. treatment session-2 reward is not lower than control reward; and
2. mean treatment cumulative authoritative cost is no greater than control.

It is **established in this cohort** only if those conditions hold and the
upper bound of the paired bootstrap 95% interval for cumulative cost difference
is at or below zero. Otherwise the result is "not established by first reuse."

If treatment is more correct but more expensive, report the correctness/cost
tradeoff without calling it payback. If correctness is equal and cumulative
treatment cost remains higher, report that payback did not occur by session 2.
Do not extrapolate a later crossover from two points.

## Admission and stopping rules

Run exactly one paid paired chain for admission and discard it from the cohort.
It is admitted only if:

1. all four agent sessions complete without truncation, timeout, fallback, or
   infrastructure error;
2. both session-1 answers pass the deterministic live-path check;
3. both session-1 source trees remain unchanged;
4. treatment graph state persists into session 2 and its before/after snapshots
   are observable;
5. both session-2 workspaces start from the same pinned corpus state; and
6. the frozen session-2 verifier produces a valid reward for both arms.

The treatment does not need to write useful memory or win for admission. Those
are behavioral outcomes.

If admission fails because the model cannot complete either task within the
fixed limits, this candidate/model combination is retired before the cohort.
Limits are not raised through repeated paid pilots. If admission fails because
of an objectively false receipt or an inability to carry the treatment graph,
fix only that infrastructure defect and pre-register a new admission attempt.

After admission, run exactly 10 paired chains. Invalid sessions are retained
with their reasons and excluded from behavioral estimates. Fewer than 8 valid
pairs makes the cohort inconclusive; it does not authorize replacement attempts
or post-hoc expansion.

## Apparatus freeze

For this experiment, the existing challenge verifier, result contracts,
statistics, reporting, condition definitions, and harness adapter are frozen.
Implementation may add only the minimum orchestration and receipts required to:

1. run two fresh conversations per arm;
2. preserve the treatment graph between them while resetting the code tree;
3. prove that no other state crossed the boundary; and
4. report the pre-registered session and cumulative measures.

No runner or verifier change may be justified by an agent's behavior or by an
unfavorable result. A broken question is retired. A behavioral failure is
reported. An apparatus change after admission requires a new version of this
pre-registration and a new cohort.

## Out of scope

- Frozen session-1 graphs or a curated payoff ceiling
- More than two sessions or estimating the later crossover point
- Anchor-recall-specific claims
- Other repositories, models, or harnesses
- Runner cleanup, dashboard redesign, new validators, or unrelated benchmark
  families

Those may follow only after this live two-session result exists.
