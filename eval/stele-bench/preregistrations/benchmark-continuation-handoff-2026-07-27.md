# Benchmark continuation handoff

Date: 2026-07-27
Last updated: 2026-07-28

## Current state after Fieldnote continuity v8

The multi-session work continued after the terminal Click K=5 admission. The
replacement Fieldnote scenario uses a small, single-file Python project over
four fresh Sonnet 4.6 conversations. Only the treatment graph persists between
sessions; source checkpoints, graders, prompts, and inference gates are frozen.

The imperative-prompt doctrine fix succeeded at task lifecycle intake. In Q1,
treatment created, claimed, and completed `TASK-4` before implementation. That
exact node was delivered in Q2, Q3, and Q4, so project binding, Auto-RAG, and
explicit graph retrieval all worked across fresh conversations.

The retained node was too narrow to help. It captured milestone 1 and immediate
implementation constraints, but omitted milestones 2 through 4, the 50 KiB
normal-output ceiling, recursive secret redaction, and the no-daemon/no-watch
decision. No knowledge node was written. The omissions explain the later
behavior:

- Q2 received the task but had to ask what milestone 2 meant;
- Q3 accepted an instruction that could exceed 50 KiB because that constraint
  was absent from memory;
- Q4 read the graph extensively but invented a release plan because the
  original release-hardening milestone was absent.

The full pair passed every apparatus gate. Both arms scored `[0, 0, 1, 0]`.
Control cost `$1.03690905`; treatment cost `$2.17382158`; the treatment delta
was `+$1.13691253` (`+109.64%`). Treatment cost more at every cumulative prefix
and no absolute crossover occurred through K4. Total settled Gateway cost was
`$3.21073063`.

This is one discarded admission pair, not an effect estimate. Its useful
publication boundary is nevertheless clear: for small, locally obvious,
single-file work, memory was a net negative because control needed almost no
exploration and graph overhead replaced no meaningful discovery cost. Do not
generalize that result to navigationally complex or historically dependent
work.

Durable evidence:

- `eval/stele-bench/preregistrations/fieldnote-continuity-v8.md`
- `eval/stele-bench/results/fieldnote-continuity/2026-07-28-fieldnote-continuity-v8-admission.json`
- `KNOW-2544` — full v8 mechanism result and publication boundary
- result commit `01427f3f`; frozen-registration commit `7de3a51f`; doctrine-fix
  commit `817d4d73`

One post-run accounting issue was found. The frozen aggregate snapshot treated
the seeded `COMP-1`, `TASK-2`, and `KNOW-3` as agent-authored because all three
carry `source_type='claude'`. Raw trajectories remained authoritative and show
the exact Q1 write was only `TASK-4`. Future snapshots now count lifecycle
agent writes plus non-starter Claude-authored knowledge and explicitly exclude
the starter knowledge node. The v8 receipt remains frozen and carries this
limitation rather than being rewritten.

## Completed in this session

- Admission 003 for the profile-selection compounding experiment was audited
  from retained verifier evidence. Both session-2 zeros came from the mutation
  wrapper rejecting a generated `Cargo.lock`; neither Rust test ran. The v3/v4
  setup is retired before cohort pair 1 and the CLI blocks accidental v4 spend.
- One-pair cohort batching was implemented and zero-model tested: immutable
  pair identities and arm order, explicit pending/started/consumed states, no
  replacement attempts, a `$40` pair fence, and cumulative authoritative spend
  against the registered `$400` cohort ceiling.
- An independent Class C overhead replication ran on 12 frozen, sanitised
  aider-polyglot tasks. The result and its registered interpretation are saved
  in:
  - `eval/stele-bench/results/aider-polyglot-clean/2026-07-27-aider-overhead-replication-v2.json`
  - `eval/stele-bench/preregistrations/aider-polyglot-overhead-replication-v2-results-2026-07-27.md`
- The tare question was resolved at zero model spend by reconstructing the
  exact first-request envelopes. The modal 17,215-token difference consists of
  17,076 standing tokens and a 139-token two-node Auto-RAG payload.
- A five-session Click `CliRunner` incident lineage was frozen, preregistered,
  implemented, and qualified at zero model spend. Its one discarded paid
  admission then failed. The cohort is not authorized and v1 must not be rerun.

Relevant commits:

- `ef5c69ac` — retire invalid compounding admission and add safe batching
- `f462d88e` — lock the 12-task overhead replication
- `3c8f6ebf` — record the replication result and registered interpretation

## Class C result

All 12 pairs ran; 10 were analysable and two were retained as excluded. The
descriptive measurements were:

- treatment minus control median input tokens: `+51,838` (`+52.53%`);
- treatment used more input tokens in 8/10 analysable pairs;
- treatment minus control median wall time: `+19.69s` (`+19.56%`);
- solve rate: 40% in each arm, with one discordant win per arm;
- treatment invoked Stele tools 23 times across 4/12 tasks and persisted an
  agent-authored graph node in 1/12 tasks.

The registered outcome is **uninterpretable**, not a confirmed 52.5% product
cost. Attempt 2 had treatment-only budget-truncation risk, which the frozen
validity rule says invalidates interpretation. The input-token replication bar
also required at least 10/12 pairs in the predicted direction and observed
8/10. The non-inferiority verdict is inconclusive.

Do not rerun, replace tasks, raise this cohort's limit, or subtract a tare from
the recorded 52.5% post hoc.

## Tare decomposition

Future reports should keep the customer-real gross cost and separately explain
its mechanism:

```
gross overhead = standing Stele tare + dynamic memory payload + behavioral change
```

- **Standing tare:** MCP tool schemas, doctrine/plugin instructions, and
  startup-hook payload.
- **Dynamic payload:** Auto-RAG and explicit retrieved project context.
- **Behavioral change:** extra or avoided model turns, reads, tool calls, and
  reasoning caused by Stele.

The tare is not one constant. It has a cold-start component and a cached
per-model-call component. A valid calibration must use the same artifact,
model, harness, cache state, and project state as the benchmark. Report gross,
cold tare, cached per-call tare, dynamic injection tokens, and the remaining
behavioral residual. The existing v2 result stays gross.

First inspect the retained per-turn Claude streams to determine whether they
contain enough request-level usage to decompose the completed run
observationally. If they do not, design a separate paired one-turn calibration;
do not retrofit a number from static source text or an approximate tokenizer.

### Continuation result

The exact Claude Code `2.1.220` control and treatment envelopes were replayed
against a local recorder with zero model calls. Controlled Gateway token counts
reconcile the modal 17,215-token first-request difference as 15,693 added tool-
catalog tokens, 1,383 installed attachment tokens excluding Auto-RAG, and 139
tokens for the two-node Auto-RAG wrapper and payload. The standing surface is
therefore 17,076 tokens, or 99.19% of this narrow first-request difference.

This decomposition is scoped to the pinned harness, artifact, model, and
two-node graph. It does not repair the gross v2 result or supply an
authoritative dollar receipt. No paid three-cell tare calibration is needed for
this first-request question. Full evidence and count-route limitations are in:

- `eval/stele-bench/preregistrations/aider-polyglot-overhead-v2-request-audit-2026-07-27.md`
- `eval/stele-bench/preregistrations/aider-polyglot-overhead-v2-envelope-reconstruction-2026-07-27.json`

## Multi-session crossover result

The source and zero-model implementation gap is closed. The frozen experiment
uses five chronological Click `CliRunner` stream-capture incidents. Each
session has an exact base, canonical target, source-history identity, hidden
verifier, and expected base/target boundary in:

- `eval/stele-bench/preregistrations/click-clirunner-k5-v1.json`
- `eval/stele-bench/preregistrations/click-clirunner-k5-v1.md`
- `eval/stele-bench/preregistrations/click-clirunner-k5-candidate-audit-2026-07-27.md`

The runner now accepts the frozen session manifest, builds a reachable-only
Git repository for each checkpoint, creates five fresh conversations per arm,
and preserves only the treatment graph. The receipt records per-session source
identity, correctness, authoritative cost, graph deltas, exact prior-node
delivery, boundary evidence, and cumulative prefixes.

The outer oracle freezes 20 paired chains with alternating order. It applies
the $3 session, $30 pair, and $600 cohort fences, consumes invalid attempts,
and computes the registered exact sign tests, Holm adjustment, and correctness
guard only after pair 20. Missing exclusive settled cost blocks the immutable
ledger without a claim.

The approved discarded admission `click-clirunner-k5-admission-001` failed.
All ten Gateway intervals were exclusive and settled, costing `$4.24664025`
control and `$6.07389187` treatment, `$10.32053212` total. Treatment cost 43.03%
more in this one discarded pair. This is descriptive only, not a crossover
estimate.

Both arms recorded 0/5 rewards. Three sessions ended in output-token
infrastructure errors, one control session attempted external source access,
and six sessions exposed a mutation-wrapper defect involving Python
`__pycache__`. The generic wrapper was repaired after spend, but the repair does
not change the frozen result.

More decisively, treatment made no graph write through session 4. One task was
created and completed in session 5, too late for reuse, and no exact prior node
version was delivered later. The registered rule therefore forbids tuning or
rerunning v1. Full evidence is in:

- `eval/stele-bench/preregistrations/click-clirunner-k5-admission-001-results-2026-07-27.md`
- `eval/stele-bench/results/click-clirunner-k5/2026-07-27-click-clirunner-k5-admission-001.json`

## Continuation order

1. Do not start the v1 cohort and do not rerun or tune its admission.
2. Use the retained receipt and checked-in result as the terminal v1 record.
3. Do not start a Fieldnote v8 cohort. Its N=1 result is mechanism evidence and
   a small-work boundary, not a statistically interpretable effect estimate.
4. The next useful question is targeted search on a navigationally complex but
   implementation-light multi-file fixture: Q1 should discover and retain the
   correct seam, while Q2 tests whether memory avoids repeated exploration.
   This is tracked by `TASK-2495`.
5. Any new paid design still needs a new frozen preregistration and complete
   zero-model qualification. The standing user approval permits consecutive
   single admission attempts up to `$30` each; it does not authorize a cohort.
