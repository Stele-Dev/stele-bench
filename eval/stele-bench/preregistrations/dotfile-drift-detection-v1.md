# Pre-registration — dotfile-drift-detection (cohort dotfile-drift-v1)

**Committed before the first attempt of this cohort's measured run. Do not edit
after results exist; write a new dated section or a new file for a re-run.**

- Challenge: `dotfile-drift-detection` (two-step, `resume_trajectory`)
- Cohort / fixture: `dotfile-drift-v1` — fixture id `dotfile-drift-decision-v1`
  (one faithful graph; memory-vs-no-memory needs no anchor variants)
- Conditions (arms): `no-stele` (control) and `faithful`
- Model: `anthropic/claude-haiku-4.5`
- Design: **5 paired attempts** — 10 arms total — via `oracle.py run-repeat`
- Run id: `drift-r1`
- Corpus: `dotstate` at `a4a5986eafa496dee73a93141588cffaed559f80`

## What has already happened, and why this run exists

`drift-003` (2026-07-23) ran this challenge **once**, not pre-registered, as a
design validation. It produced `cited_decision` faithful 1.0 / control 0.0 — the
first memory signal this apparatus has ever produced. One attempt per arm is a
single coin flip; it establishes that the mechanism *can* fire, not how often.
This run exists to turn that into a rate, and to test the second half of the
claim — earliness — which `drift-003` left entirely open (investigative tool
calls 11 vs 12, i.e. noise).

`drift-003` is **not** pooled into this run's results. It ran under a different
per-arm budget cap and before this pre-registration existed; counting it now
would be choosing the analysis after seeing the data. It is cited only as the
reason the predictions below point in the direction they do.

## The mechanism under test

Turn 1 (establish, **not** scored) asks how to integrate dotfile-drift detection
into the codebase, and says *"Don't change any code yet - I just want your
recommendation."* Turn 2 (follow-up, **scored**) is six words issued in the same
resumed session: *"can we do this in the background?"* — no filename, no symbol,
no mention of daemons. Turn 1 deliberately never says "background", "periodic"
or "daemon", so the triggering concept first appears at the scored turn.

The seeded memory is a decision **not** to build something: DotState runs no
resident process, settled after the **watchdog-2** spike, where a resident file
watcher sat at **8–12% CPU idle**. A decision not to act leaves no trace in the
source — there is no daemon in the tree, and nothing distinguishes "we decided
against this" from "nobody got to it yet". The agent either has the memory or it
guesses, and the guess is "sure, spawn a worker."

The correct answer **surfaces the constraint**; it does not refuse. Stele's
doctrine is inform, push back, still help — the user holds the decision. An
agent that cites watchdog-2 and then explains alternatives is fully correct and
scores full marks.

## Oracle (deterministic — no judge)

Two separately-recorded measures, neither of which involves a model:

- **`cited_decision`** — the memory signal, graded **host-side from the
  transcript**, because this question is answered in conversation rather than in
  a file. The runner scans **only agent messages after the final user turn** for
  the challenge's `response_markers` (`watchdog-2`, `watchdog 2`, `watchdog2`,
  `8-12%`, `8-12 %`, `8 to 12%`), case-folded over dash/whitespace-normalized
  text. Reasoning from an earlier turn cannot score.
- **`reward`** — a **workspace floor**, not the memory signal. Turn 1 said not to
  change code, so `verifier.sh` gives reward 1 only if no drift/watcher/daemon
  source appeared. It is a validity indicator; it is not the thing being
  measured, and it is expected to be 1 in every arm.

Generic good advice about background processes deliberately does **not** score.
The markers are the whole point: certification proves both `solution_markers`
("runs no resident process", "watchdog-2 spike") are absent from **all 150
corpus files**, so a control arm cannot produce them by inference at any token
budget. That absence proof is re-run as a gate before this run spends anything.

## Measures

**Primary (confirmatory):** `cited_decision` rate per arm over 5 attempts.

**Floor (validity):** `reward` per arm.

**Secondary (exploratory, and declared under-powered in advance):**
`session_investigative_tool_calls` (the whole resumed session),
`investigative_tool_calls` (the graded turn alone), `tokens_input`, and
`trial_wall_seconds`. See the revision note below for why the earliness
prediction rides on the **session** scope and not the graded turn.

**Descriptive:** the injection `channels` mix recorded in `memory_evidence` for
each faithful attempt, and per-arm `claude_reported_usd`.

## Analysis plan (fixed before running)

1. All 5 attempts run to completion before any result is read. **No peeking, no
   early stopping, no adding attempts after seeing the split.**
2. Per-arm rate of `cited_decision`, reported with a **Wilson 95% score
   interval** — never as a bare point estimate.
3. Primary test: **one-sided Fisher's exact test** on the 2×2 table
   (arm × cited), in the pre-specified direction faithful > control, at
   α = 0.05. At N = 5 per arm a perfect 5/0 split gives p = 1/252 ≈ 0.004; a 4/0
   split gives p ≈ 0.024; a 3/0 split gives p ≈ 0.083 and **does not clear the
   threshold**. This is stated here so the threshold cannot be moved afterwards.
4. Secondary measures are reported as the paired per-attempt difference
   (faithful − control) with its median and full range. **No p-value is computed
   for them** — they are exploratory at this N and will be labelled as such.
5. Attempts are analysed **intent-to-treat**: every attempt that produced a
   graded transcript counts, including ones where the floor failed. Only an
   attempt with no graded transcript at all (harness failure, `outcome:
   invalid`) is excluded, and any exclusion is recorded with its reason and
   replaced by a re-run so N stays 5.

## Named confounds (recorded before running)

1. **Arm order / time position.** Arms run sequentially, so a later arm sees a
   later model-serving moment. `run-repeat` therefore **alternates arm order
   across attempts** (odd attempts control-first, even attempts faithful-first)
   and records the realised order per attempt, so arm is not confounded with
   position in the run.
2. **Budget truncation masquerading as a failure to cite.** In `drift-003` the
   control arm reported \$0.92 against a \$1.00 per-arm cap — 92% of the ceiling.
   An arm cut off mid-answer would score `cited_decision = 0` for a reason that
   has nothing to do with memory. This run therefore raises the per-arm cap to
   **\$1.50** (acknowledged total \$15.00 for 10 arms; expected actual spend is
   ~\$2, since the drain-measured cost of `drift-003` was ~\$0.18/arm). An arm is
   flagged in the results as a possible truncation — whatever it scored — if
   either the runner reports it actually hit its cap
   (`claude_report_exceeded_requested_cap`) or its `claude_reported_usd` came
   within 10% of the ceiling.
3. **The control can be right without knowing.** "Can we do this in the
   background?" has a defensible generic answer that mentions CPU cost. That
   answer scores zero here by design — only the unguessable incident name and
   figure count. This makes the measure conservative for the control, which is
   the direction we want, but it means `cited_decision = 0` for the control is
   **not** the same as "the control answered badly".
4. **One question, one model, one corpus.** Nothing here licenses a claim about
   agents in general, other models, or other repositories. The unit of
   generalisation is this question.
5. **This question can only exercise semantic recall, never anchor recall.**
   `drift-003` showed the decision reaching the agent via `rag` only, never via
   the file-anchor nudge, despite the decision being anchored to
   `sync_service.rs`. The reason is structural, and was confirmed against the
   retained trajectories before this run: the file-anchor cell fires on
   **Edit/Write**, not on Read, and turn 1 tells the agent not to change any
   code — so an obedient agent never triggers it. (The agent did read the
   anchored file; reading is simply not the trigger.) This is not a threat to
   the run — the claim under test is memory vs no memory, and `rag` is memory —
   but it bounds what the result may be said to show: **nothing about the
   anchor channel, in either direction.** A `rag`-only mix is the expected
   outcome here, not a failure.

## Falsifiable predictions

Let `c(arm)` = rate of `cited_decision = 1` over the 5 attempts.

1. **Primary:** `c(faithful) ≥ 0.8` and `c(control) = 0`, with one-sided
   Fisher's exact p ≤ 0.05.
2. **Floor:** `reward = 1` in at least 9 of the 10 arms. A floor failure means
   the agent wrote code after being told not to — a task-validity problem to
   diagnose, not a memory result.
3. **Earliness (exploratory):** faithful shows fewer
   `session_investigative_tool_calls` than control. **Pre-committed threshold
   for claiming it at all:** the paired difference must favour faithful in
   **≥ 4 of 5 attempts** *and* the median reduction must be **≥ 25%**. Below
   that, the answer is "not established", and it will be reported as not
   established even if the mean looks favourable. The graded-turn scope
   (`investigative_tool_calls`) is recorded but carries **no** prediction — it
   is expected to be 0 in both arms (see the revision note).
4. **Injection:** every faithful attempt records at least one memory channel,
   and that channel is expected to be `rag` (see confound 5). If none do, the
   run is an injection failure and the primary result is uninterpretable —
   check `memory_evidence` before blaming the model.

## Outcome → decision (decided in advance)

- **P1 holds** (`c(faithful) ≥ 0.8`, `c(control) = 0`, p ≤ 0.05): the question is
  a working memory probe with a defensible rate at N = 5. It becomes the
  reference question for the portfolio, and the next move is a second *distinct*
  question built on the same template — not more attempts of this one.
- **P1 direction holds but p > 0.05** (e.g. 3/5 vs 0/5): suggestive, not
  established. Pre-committed response: re-run at **N = 10** under a **new**
  pre-registration file. Do not publish the N = 5 number as a rate.
- **`c(control) > 0`:** the answer is inferable without memory, exactly the
  failure that retired session-boundary-recall. The question is contaminated —
  find how the marker text or its content reached the control before drawing any
  conclusion, and redesign rather than publish.
- **`c(faithful)` low:** memory did not surface, or surfaced and was ignored.
  Check the recorded channels first — an injection failure and a recall failure
  are different bugs and only the receipt distinguishes them.
- **Floor fails in more than one arm:** task-validity problem. Fix the challenge
  and re-run before reading any memory result.
- **P3 fails its threshold:** report "earliness not established at N = 5" and
  leave it unclaimed. This is an expected and acceptable outcome; the primary
  claim does not depend on it.

## Revision — 2026-07-24, before any attempt of this run

Found while verifying the apparatus, **before spending anything**: the earliness
measure was counting the wrong thing, and as originally registered prediction 3
could not have fired.

`_investigation_depth` documented itself as counting tool calls "in the graded
turn" but walked the entire trajectory. On a multi-step challenge
`resume_trajectory` is on, so the graded step's trajectory carries every earlier
turn with it — the count was therefore mostly turn-1 exploration, which is the
same work in both arms.

Replaying the corrected measure on the retained `drift-003` trajectories (free,
no re-run) shows how badly this mattered:

| arm | graded turn | whole session |
|---|---|---|
| faithful | 0 | 11 |
| control | 0 | 12 |

Both arms made **zero** tool calls in the graded turn — the agent answers a
conversational question straight from context. The "11 vs 12" reported for
`drift-003` was the session figure wearing the graded turn's name.

**What changed:** `investigation_depth` now reports both scopes — the graded
turn at the top level (sliced at the final user turn, the same boundary the
response scan already used) and the whole resumed session under `session`. Both
are carried into arm metrics and the paired summary.

**What this does to the predictions:** prediction 3 now rides on
`session_investigative_tool_calls`, because that is the only scope where a
shortened dig could appear for this question. The graded-turn scope is recorded
with no prediction attached; if it is ever non-zero, that is itself interesting.
Predictions 1, 2 and 4 are untouched — the primary measure (`cited_decision`)
was always sliced correctly, and `drift-003`'s `scanned_from: 9` confirms it.

The same verification pass settled the open anchor-recall question from
`drift-003` and sharpened confound 5: the file-anchor cell fires on Edit/Write,
never on Read, so a question that forbids code changes cannot exercise it. That
bounds the claim (this run says nothing about the anchor channel) without
changing any prediction.

Nothing in this section was informed by a result of this run, which has not been
executed. It is recorded as a revision rather than a silent edit so the original
wording stays visible in the file's history.

---

## Results — run `drift-r1`, 2026-07-24 (written after the run)

**Disposition: a valid execution that FALSIFIES prediction 1. The apparatus is
fine; the question is not.** All 10 arms completed, all 5 attempts produced
graded transcripts, no arm hit a money fence, total drain **$1.58** against a
$15 ceiling. There is nothing wrong with the run — the prediction was simply
wrong, and `drift-003`'s 1-for-1 was a lucky draw.

### Primary — prediction 1 FAILED

| measure | faithful | control |
|---|---|---|
| `cited_decision` | **1/5** (0.20), 95% CI [0.04, 0.62] | 0/5 (0.00), 95% CI [0.00, 0.43] |

One-sided Fisher **p = 0.50**. Registered threshold was rate ≥ 0.80 and
p ≤ 0.05. Failed on both, by a wide margin. Re-deriving with attempt 3 excluded
(below) gives 1/4 vs 0/4, p = 0.50 — the conclusion does not depend on that
judgement call.

### Why it failed — the question is ambiguous, and the control is not wrong

The design assumed the unaided guess would be *"sure, spawn a worker"* and that
this was **wrong**. It isn't. For a TUI that already runs `git_status_receiver`
and `update_check_receiver` as tokio one-shot channels, *"can we do this in the
background?"* reads naturally as **an async in-process task**, and that is not
what the seeded decision forbids. The decision rules out a **resident process**;
an async task inside the app's own event loop was never in scope. Nine of ten
arms — control and faithful alike — answered that reading, with real citations
to the existing pattern in `app.rs`. That is a good answer. It needs no
`watchdog-2`.

So a faithful arm can hold the memory, use it, answer correctly, and still score
zero. That is exactly what happened in attempt 5, where the agent's own
reasoning weighed the seeded decision — *"Given the Stele context that says
'DotState runs no resident process', option 2 (daemon) doesn't fit the design
philosophy"* — and its answer concluded **"No daemon needed"**, without ever
naming the incident. Scored: 0.

Contrast attempt 2, the one that scored, which read the question the other way:
*"Not without revisiting the architectural decision. Your codebase has a
documented decision against resident processes (KNOW-1): a prototype watcher
consumed 8-12% CPU idle and kept laptop fans audible…"*

**The marker did not measure recall. It measured which of two readings the agent
happened to pick.**

### Uptake, in three layers (this is the useful part)

| layer | count |
|---|---|
| memory injected (`channels: ['rag']`, `injected: true`) | **5/5** |
| decision visible in the answer or the reasoning | **3/5** (attempts 1, 2, 5) |
| registered marker produced | **1/5** |

Injection is reliable. Uptake is not: two faithful arms (3, 4) show no trace of
the decision anywhere — injected and ignored. And the gap between layer 2 and
layer 3 is the scoring defect above.

### Prediction 2 (floor) — met, with one caveat

`reward = 1` in every arm that produced a verdict. Attempt 3's **control** came
back `outcome: invalid` (no verifier artifacts, reward `null`), so 9 arms
returned a floor and all 9 passed.

### Prediction 3 (earliness) — FAILED, and the direction is REVERSED

| measure (faithful − control) | median | attempts favouring faithful |
|---|---|---|
| `session_investigative_tool_calls` | **+2.5** | 0/4 |
| `tokens_input` | **+743k** | 0/4 |
| `trial_wall_seconds` | **+40.3s** | 1/5 |

Memory did not shorten the dig; the faithful arm consistently dug **more**. The
registered bar (≥4 of 5 favouring faithful, ≥25% median reduction) is not merely
unmet — every measure moved the other way. Reported as **refuted at N=5**, not
"not established".

One honest caveat before anyone quotes the token figure: the faithful arm also
carries the plugin's tool schemas and injected context, so `tokens_input` is
confounded by fixed overhead and is not a clean behavioural measure (cf.
[[KNOW-1623]]). `session_investigative_tool_calls` is the cleaner signal, and it
also went the wrong way.

### Prediction 4 (injection) — HELD

5/5 faithful attempts recorded a memory channel, and `rag` in every case, exactly
as confound 5 predicted. No anchor-nudge fired, as expected.

### Two apparatus defects found by this run, both since fixed

1. **The truncation warning used the wrong denominator.** It compared an arm's
   whole cost against the *per-arm* budget, but a two-step challenge bills each
   step against that cap, so the real ceiling is `cap × steps` ($3.00, not
   $1.50). Attempts 1 and 3 were flagged as near-truncation when they were
   nowhere near it. Both flags were false positives; no arm was truncated. The
   check now reads the effective cap from the receipt.
2. **`outcome: invalid` was not excluded.** This document promised to exclude an
   attempt with no valid graded result; the code only checked exit codes, so
   attempt 3 was analysed despite its control being disowned by the runner. Now
   excluded by reason `arm_outcome_invalid`. As noted above, applying it does
   not change the verdict.

### Decision (applying the rule registered above)

The registered branch for a low `c(faithful)` says to check the channels first,
and distinguishes *injection failure* from *recall failure*. The channels rule
out injection failure — but the data show a **third mode the rule did not
anticipate**: memory present, memory used, marker absent because the question
admits a correct answer that needs no marker.

So neither the "re-run at N = 10" branch nor the "contaminated question" branch
fits as written. Recording that plainly rather than forcing the result into the
nearest box:

- **Do not** publish 20% as a recall rate. It is a rate for *"picked the daemon
  reading of an ambiguous question"*.
- **Do not** re-run this question at higher N. More attempts would measure the
  ambiguity more precisely.
- **Do** fix the question: turn 2 must force the resident-process reading (ask
  about it running *while the app is closed*, or *without the app open*), so
  that the async-task answer is no longer available and the decision genuinely
  bears on the answer. Then re-register as `-v2` and re-run.
- **Do** keep the three-layer uptake instrumentation. "Injected 5/5, visible
  3/5, marker 1/5" is far more informative than the reward, and the gap between
  those layers is now the most interesting open question about recall.

**What this run bought for $1.58:** it stopped a 100%-recall claim that was one
lucky draw wide. At N = 1 this question looked like a validated memory probe; at
N = 5 it is a validated *ambiguity* probe. That is the entire argument for
pre-registration and N, demonstrated on our own headline result.

## Cost

Not predicted as a target. `drift-003` drained ~\$0.18/arm, so ~\$2 total is the
expectation for 10 arms, against an acknowledged ceiling of \$15.00. The ceiling
is a fence, not an estimate. The authoritative figure is the shared-balance
drop recorded between arms, not Claude's self-reported cost.
