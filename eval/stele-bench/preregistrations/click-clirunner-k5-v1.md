# Pre-registration: Click `CliRunner` K=5 compounding crossover v1

Status: locked on 2026-07-27 before K-session runner changes and before any
paid admission. Do not edit this document after a model call. Record a
deviation, admission audit, or result in a new dated document.

## Question and claim boundary

This experiment asks:

> Across five related but fresh coding sessions, when does the cumulative
> authoritative cost of naturally maintaining and reusing a Stele graph become
> lower than working without Stele, without a material correctness loss?

It measures the shipped live loop. Treatment graph writes are unprescribed,
later delivery uses only shipped behavior, and quiet graphs remain in the
analysis. A result may establish crossover at or before session 5, or state
that crossover was not established by session 5. It may not extrapolate a
later crossing, generalize to other repositories or graph sizes, or subtract a
separately estimated tare from observed usage.

## Frozen source and tasks

The machine-readable source contract is `click-clirunner-k5-v1.json`. Its
repository, full revisions, trees, reachable object sets, canonical patch
digests, challenge manifests, verifiers, model, harness, and task image are
part of this preregistration.

The five sessions follow the real Click `CliRunner` incident lineage:

1. separate stdout and stderr while preserving terminal order;
2. repair the shared-buffer finalization order race;
3. remove competing buffer ownership without reviving that race;
4. support consumers that need an original file descriptor; and
5. make descriptor capture explicit so the safe default cannot damage host
   capture.

The exact user prompt and allowed output paths for each session are the prompt
and allowlist in its authenticated `challenge.json`. The deterministic reward
is the authenticated verifier's `reward.json`. No prompt, allowlist, base,
target, or verifier changes are allowed after a paid admission begins.

## History and session boundary

Every arm and session starts from a new workspace. The workspace is a real Git
repository containing exactly the objects reachable from that session's
registered base commit, with `HEAD` at the base and no remotes. Local `git log`,
blame, and earlier commits are available. The registered future target commit
and all other unreachable objects are absent.

Before the prompt, the runner must prove the registered head, tree, commit and
object counts, reachable-object digest, clean worktree, successful `git fsck`,
absent target commit, no remotes, and byte-identical control/treatment history.
Any mismatch fails before a model call.

Public networking is required for the model gateway. Any agent tool call that
uses a remote Git operation, GitHub, web search, `curl`, `wget`, or another
external source makes that chain operationally invalid. No dependency fetch is
needed by the verifier. Ordinary local Git history inspection is permitted and
reported.

No conversation, summary, agent home, code mutation, generated file, build
artifact, or cache crosses a session boundary. Both arms advance to the next
registered canonical base. The control carries no project state. The treatment
carries only the same Stele project graph.

## Conditions and schedule

- Harness: Claude Code, exact version recorded in every receipt
- Model: `anthropic/claude-sonnet-4.6`, no fallback
- Conditions: `no-stele` control and `stele-fresh` treatment
- Cohort: 20 paired five-session chains, 200 paid cohort sessions
- Session limit: 45 turns, 900 seconds, requested cap `$3.00`
- Pair fence: 10 sessions, `$30.00`
- Cohort fence: 20 pairs, `$600.00`
- Discarded admission fence: one pair, `$30.00`, outside the cohort fence
- Whole-experiment hard reserve: `$630.00`

Pairs are assigned identities 1 through 20 before pair 1. Odd pairs run the
control chain first and even pairs run the treatment chain first. Each chain
always runs sessions 1 through 5 in order. Execution is one pair per resumable
batch. A receipt consumes its frozen pair identity even if the chain later
fails; there are no replacement pairs.

No treatment-effect aggregation is exposed until all 20 pair receipts exist.
Between batches, only operational validity, settled exclusive spend, remaining
reserve, and safety may be inspected. No intermediate effect may change the
prompts, tasks, model, cap, order, sample size, or analysis.

## Natural intake and delivery

The prompts do not mention Stele, ask the agent to save memory, prescribe a
node, or name an anchor. Any treatment write must follow the shipped doctrine
and the agent's own judgment. There is no benchmark-authored extraction,
cleanup, or graph edit between sessions.

For every treatment session, report:

- agent-authored nodes created or updated, by type and version;
- active and archived nodes separately;
- node anchors and task bindings;
- whether a later session received that exact prior node version through a
  node-bearing hook attachment or Stele tool result;
- whether the later trajectory observably referenced or acted on it, as a
  lower bound only; and
- chains with zero writes and chains with zero later delivery.

All 20 treatment chains remain in the intent-to-treat analysis, including
zero-write, irrelevant-write, zero-delivery, task-failure, and truncated chains
when authoritative cost is settled. Graph activity is a mechanism outcome, not
an inclusion criterion for the cohort.

## Authoritative cost

The primary monetary value is the settled Gateway-observed USD delta for each
individual session. The evaluation key must be exclusive during every measured
window. A pre-session and post-session observation, window timestamps, key
identity hash, settlement status, and exclusivity check are retained in the
receipt. Model-reported cost and rate-table reconstruction are cross-checks
only.

Costs from behavioral failures and truncations are included. A session without
an exclusive, settled authoritative delta cannot contribute a substituted
value. If any of the 20 cohort pairs lacks a complete ten-session authoritative
cost vector, the crossover claim is unavailable; the operational result is
reported without replacement attempts.

## Outcomes

At each cumulative prefix `k = 1..5`, compute for every pair:

`D[i,k] = treatment cumulative Gateway USD - control cumulative Gateway USD`.

Primary outcomes are all 20 paired differences, their sample median, mean,
quartiles, and the count below, equal to, and above zero at each prefix.
Session-specific and cumulative provider-reported input, output, cache-read,
and cache-creation tokens, tool calls, wall time, local Git commands, graph
writes, graph delivery, and deterministic rewards are secondary outcomes.

## Crossover inference

All five prefixes are evaluated once, after the full cohort. For each prefix,
the raw one-sided exact sign-test value is

`P(Binomial(20, 0.5) >= count(D[i,k] < 0))`.

Exact ties count as nonnegative, which is conservative. Apply Holm's step-down
correction across the five raw values at family-wise alpha `0.05`.

The earliest observed crossover is the smallest chronological `k` for which:

1. the sample median of `D[:,k]` is strictly below zero;
2. the Holm-adjusted one-sided value is below `0.05`; and
3. the correctness guard below holds at every prefix through `k`.

If no prefix satisfies all three, report "crossover not established by session
5." A raw crossing, an uncorrected value, a mean below zero, or a favorable
subgroup is not a crossover result.

With 20 pairs, the worst-case Holm threshold is `0.01`; at least 16 of 20
negative paired differences are then required (`p = 0.005909`). The resulting
power is approximately 0.63 when a treatment chain is cheaper with probability
0.80, 0.83 at probability 0.85, and 0.96 at probability 0.90. The study is
therefore sized for a strong, consistent crossover, not a small cost shift.

## Correctness guard

Reward is binary and deterministic per authenticated stage verifier. At each
prefix, arm pass rate uses all `20 * k` tasks, with behavioral failure,
truncation, or undeclared mutation scored zero. Infrastructure failure is
reported separately and never relabeled as behavioral success.

The guard holds through prefix `k` only when, at every `j <= k`:

- control cumulative pass rate is at least 80%;
- treatment cumulative pass rate is at least 80%; and
- treatment minus control cumulative pass rate is at least -10 percentage
  points.

At `k=1`, one extra treatment failure is a -5 point difference and remains
inside the margin; three extra failures produce -15 points and fail it. Report
stage and cumulative arm rates, Wilson 95% intervals, all paired discordances,
and exact two-sided McNemar values. These do not replace the fixed guard.

## Feasibility and funding arithmetic

The prior discarded two-session profile-selection admission observed
`$1.21678845` control and `$2.13266955` treatment, a `$0.91588110` cumulative
gap. If a new chain carried that same gap after session 2, crossing by session
5 would require average treatment savings of `$0.30529370` in each of sessions
3 through 5. This is about half the prior control cost per session, so the K=5
question is informative only for substantial later-session savings.

Linear scaling of that prior receipt gives an expected five-session pair cost
of `$8.37364500`, `$167.47290000` for 20 cohort pairs, and `$175.84654500`
including admission. This is a planning estimate, not a result. The `$630.00`
hard reserve is 3.58 times that estimate. A `$3.00` session cap is 58% above the
largest `$1.899` single-session cross-check observed in the overhead run and
does not reproduce its near-binding `$2.00` fence.

The discarded admission must publish the same break-even calculation from its
authoritative per-session receipts. If its cumulative treatment deficit after
session 2 is at least the entire control cost of sessions 3 through 5, even
zero-cost later treatment sessions could not cross by session 5 in that pair.
The candidate is then infeasible at K=5 and no cohort is authorized.

## One discarded paid admission

No admission may start without explicit user approval of its `$30.00` cap. It
is permanently excluded from the cohort. It passes only if:

1. all ten sessions use the pinned model and complete without fallback,
   infrastructure error, forbidden network access, or source-boundary failure;
2. every verifier emits a valid behavioral reward in both arms and each arm
   passes at least four of five tasks;
3. all ten sessions have exclusive, settled authoritative Gateway receipts;
4. the treatment graph persists across fresh sessions while every other state
   boundary remains clean;
5. at least one treatment session 1 through 4 naturally creates or updates an
   agent-authored node; and
6. a later treatment session receives the exact earlier node version through a
   node-bearing shipped Stele delivery path.

The node need not make treatment win. Admission proves that the loop and the
primary receipt exist, not the effect. If the fixed prompts produce no graph
write or later delivery, the candidate fails admission and is not tuned or
rerun. If a zero-model boundary defect is found before any model call, it may
be fixed under a new preregistration version. Any apparatus change after spend
requires a new version and a new explicit approval.

## Zero-model gate before admission

The runner may become eligible for approval only after tests prove:

1. all five challenge manifests authenticate;
2. every verifier fails at its base and passes at its target in the pinned task
   image;
3. each truncated repository passes every registered object and history
   assertion while its future target is absent;
4. control and treatment start from byte-identical history and source;
5. local Git history is allowed but remote and external-source access is
   detected fail-closed;
6. only the treatment graph crosses sessions;
7. retained node-bearing hook attachments and Stele tool results count exact
   delivered node versions;
8. session and cumulative K=5 receipts, intent-to-treat zeros, Holm correction,
   correctness guards, fixed pair order, immutable batching, and money fences
   pass unit tests; and
9. a simulated missing or nonexclusive Gateway observation blocks crossover
   aggregation rather than falling back to calculated cost.

Zero-model success does not authorize the paid admission.
