# Pre-registration — session-boundary-recall (cohort session-boundary-recall-v1)

**Committed before the first paid run. Do not edit after results exist; write a
new dated section or a new file for a re-run.**

- Challenge: `session-boundary-recall` (two-step, `resume_trajectory`)
- Cohort / fixtures: `session-boundary-recall-v1` — variants `correct-anchor`,
  `wrong-anchor`, `no-anchor`
- Conditions (arms): `no-stele` (control), `correct-anchor`, `wrong-anchor`,
  `no-anchor` (three faithful graph variants)
- Model: `anthropic/claude-haiku-4.5`
- Design: single shared control vs three graph variants via `oracle.py run-quad`
- Attempts: **exploratory, single attempt per condition** unless stated
  otherwise below. A single binary outcome per arm is noisy; treat one quad as
  "does the apparatus run and produce signal at all," not as a rate.

## The mechanism under test

Turn 1 asks the agent to add a `.windsurf/rules` `DotfileCandidate` to
`src/dotfile_candidates.rs` and says *do not modify any other file*. Turn 2 is
a deliberately vague follow-up ("close it out the way we usually do") issued in
the **same resumed session**. Between the turns, anchor-triggered recall may
surface a prior-session lesson keyed on the touched file
(`src/dotfile_candidates.rs`): *a new dotfile candidate also needs a matching
`- **Component**: description` bullet in `CHANGELOG.md`'s `[Unreleased]`
section.* The variants differ only in where that lesson is anchored.

## Oracle (deterministic)

The hidden `verifier.sh` writes `reward` and, separately, two sub-flags in
`evidence.json`:

- `dotfile_candidate_entry` — is `path: ".windsurf/rules"` present in
  `dotfile_candidates.rs`?
- `changelog_unreleased_bullet` — is there a `- **…**: …windsurf…` bullet
  **inside the `## [Unreleased]` section** of `CHANGELOG.md`?
- `reward = 1` iff **both** are true.

`changelog_unreleased_bullet` is the discriminator. `dotfile_candidate_entry`
is explicitly instructed in turn 1 and is expected true in every arm; if it is
false anywhere, that arm is a **task/harness failure**, not a memory signal,
and must be fixed before any memory claim is read.

## Named confound (recorded before running)

`CHANGELOG.md` is a real file in the corpus and already uses Keep-a-Changelog
format with an `[Unreleased]` section. The auto-loaded convention statements
(`CLAUDE.md`, `.claude/skills/…`) are **removed by certification's redaction**,
so the convention is not handed to the agent for free — but the *format itself*
is visible to every arm. A no-memory agent could therefore add the changelog
bullet by inference from the vague follow-up plus the visible `CHANGELOG.md`.

Consequence: **the no-stele baseline is not guaranteed to be zero.** The claim
is about a *difference in rate*, not an absolute pass/fail, and a single
attempt cannot establish a rate. This is the main reason the first result is
exploratory.

## Falsifiable predictions

Let `b(condition)` = rate of `changelog_unreleased_bullet = 1`.

1. **Primary:** `b(correct-anchor) > b(no-stele)` and
   `b(correct-anchor) > b(wrong-anchor)`. The anchored lesson raises the rate
   of closing out with the CHANGELOG bullet.
2. **Negative control:** `b(wrong-anchor) ≈ b(no-stele)`. An anchor on an
   untouched file must not surface the lesson — if `wrong-anchor` behaves like
   `correct-anchor`, recall is leaking on something other than the file match.
3. **Ordinary recall:** `b(no-anchor)` sits between the two — the lesson is in
   the graph but has no structural hook, so only FTS/semantic recall on the
   vague follow-up can surface it. This is the arm that depends on **real
   embeddings**; with placeholder vectors it is uninterpretable.
4. **Task floor:** `dotfile_candidate_entry = 1` in all four arms.

## Outcome → decision (decided in advance)

- **Predicted ranking holds** (`correct-anchor` high, `wrong-anchor` ≈
  `no-stele` low): the mechanism and the apparatus work. Build the portfolio
  (TASK-2462 / 2463 / 2470) and repeat this at N ≥ 5 for a rate.
- **`no-stele` already high**: the question is too easy / contaminated by
  `CHANGELOG.md` visibility. The apparatus is fine; the *question* needs a fact
  that is genuinely unreachable without memory. Redesign the challenge, do not
  publish this one.
- **Nothing fires anywhere** (even `correct-anchor` low): recall/injection did
  not reach the agent. Check `memory_evidence` (below) before blaming the model.
- **`dotfile_candidate_entry` false anywhere**: harness/task bug; fix and
  re-run before reading any memory result.
- **`wrong-anchor` ≈ `correct-anchor`**: the anchor match is leaking; the
  recall scoping is wrong (this is the exact thing the last SDK fix touched).

## Injection vs task failure (not yet measured)

The reward alone cannot tell *the lesson never reached the agent* (injection
failure) from *it reached the agent and was ignored* (task failure).
`stele_bench.memory_evidence.detect_injected_marker` decides this by scanning
the retained ATIF trajectory for the lesson's marker text in a non-agent step.
**Trajectory retention is not wired in the runner yet (TASK-2473 / KNOW-2476)**,
so `memory_evidence` is `null` for this run. Wire it during this run if
feasible; otherwise the first result is reward-only and a low `correct-anchor`
is ambiguous between the two failure modes.

## Cost

Not predicted. The per-arm `claude_reported_usd` in the receipt is the
measurement; this run's whole purpose includes producing the first real cost
number for a Haiku coding quad. The acknowledged cap is the ceiling, not an
estimate.

---

## Results — run `sbr-001`, 2026-07-23 (written after the run)

**Disposition: INVALID as a memory measurement; VALID as an infrastructure +
design shakedown.** The oracle correctly refused to report a number — every arm
came back `outcome: invalid` — so there is no memory result to read. Do not
treat the `reward: 0` values as "memory didn't help."

**Cost (first real number): ~$0.53 total for 4 arms (~$0.13/arm), drain-measured.**
Haiku with heavy prompt caching (977K input / 721K cached / 4.5K output per
arm). The Claude CLI reported cost as `unavailable` (non-catalog provider), and
Harbor's own $1.20/arm estimate is a non-cached overestimate — the shared-balance
drop ($70.84 → $70.31) is authoritative.

**What actually worked (the important part):**
- End-to-end orchestration: 4 containers, graph restore, project binding, the
  reserve fence, between-arm drain checks (real balance tracked), verifier
  invocation, cleanup — all functioned.
- **Memory injection fired.** In the correct-anchor arm the agent's own final
  message said it "verified the work follows **KNOW-1** (the lesson about
  closing out dotfile candidates with CHANGELOG entries)" — it recalled the
  anchored lesson *by node id*. The lesson marker is present in the follow-up
  trajectory. The mechanism under test demonstrably works.

**Three concrete blockers found (none is "memory doesn't work"):**
1. **Runner doesn't read Harbor's multi-step layout.** Trajectories and sessions
   exist under `steps/<name>/agent/`, but `run_scored_smoke.py` looks in the
   single-step location → `missing_atif_trajectory`, `session_file_count: 0` →
   every arm marked `invalid`. This also blocks `memory_evidence`.
2. **Mutation audit force-zeroes the reward and masks the real result.** All
   three faithful arms tripped `undeclared workspace mutation` — and the guard
   short-circuits *before* `verifier.sh` runs, so we never learn whether the
   changelog bullet was written. The agent's trajectory only writes the two
   allowed files, so the offending write is a non-agent side-effect — the
   strong hypothesis is the Stele plugin binding writing `.stele/` into
   `/workspace` (which certification redacts from the corpus, so it reads as a
   new file). The mutation receipt records only before/after digests, no path
   list — so it is currently un-diagnosable; path-level diagnostics are a
   prerequisite fix.
3. **The prompt is ambiguous / contaminated by the plugin's own workflow.**
   "Close it out the way we usually do" collides with the Stele plugin's
   standing instruction to track work with a task. The agent "closed out" by
   creating/claiming/completing a Stele task (and referencing the changelog
   lesson) rather than unambiguously editing `CHANGELOG.md`. The challenge does
   not yet cleanly isolate changelog-convention recall from Stele's task ritual.

**Decision (per the outcome table): harness/task bugs present → fix and re-run
before reading any memory signal.** The predictions stand un-tested. Fixes
tracked as follow-up tasks blocking the eval-portfolio epic.

---

## Revisions before the re-run (`sbr-002`), 2026-07-23 (committed before the re-run)

All three `sbr-001` blockers are fixed; the fixes were validated against the
retained `sbr-001` artifacts (free — no re-run spend) before this section was
written. This is a **new exploratory attempt**, not a continuation of `sbr-001`.

**What changed:**
1. **Runner reads Harbor's multi-step layout + injection is measured.** The
   scored runner now resolves the graded step (`steps/follow-up`) and detects
   memory injection from the retained **session transcript**, not the ATIF
   trajectory. Discovery that reshapes the design (below): Harbor's ATIF
   normalizer *drops* the `hook_additional_context` attachments that carry
   Stele's injection, so the trajectory never held them — the injection lives in
   the session transcript, tagged by channel. (finding #1)
2. **Mutation audit no longer force-zeroes on Stele's binding.** `.stele/` (the
   plugin's workspace binding, which certification redacts from the corpus so it
   reads as a new file) is now exempted, and the mutation receipt lists the
   offending paths. (finding #2)
3. **Follow-up prompt disambiguated from Stele's task ritual.** New text:
   *"…close this change out the way this project expects for a change like this
   one — in the repo itself, following the project's own convention, not just
   marking your own work as done."* The close-out is now unambiguously a
   repository artifact (still not naming the changelog), explicitly not
   satisfiable by completing a task. The `sbr-001` "mechanism under test"
   paragraph above quotes the *old* follow-up; this is its replacement. (finding
   #3)

**The key design finding — prediction #2 is reinterpreted.** The channel
instrumentation from fix #1, replayed on `sbr-001`, shows the lesson **title**
reaches the agent through TWO channels: `nudge:recall-file` (the anchor recall,
the mechanism under test) and `rag` (the prompt-time Auto-RAG walk). The anchor
nudge fired **only** in correct-anchor (wrong-anchor and no-anchor got `rag`
only) — so the negative control holds cleanly **at the injection layer**. BUT
the whole challenge is topically *about* dotfile candidates, so the Auto-RAG
walk surfaces the lesson to *every* faithful arm regardless of anchor, from
turn 1. Consequence:

- **Reward-level prediction #2 (`b(wrong-anchor) ≈ b(no-stele)`) is unlikely to
  hold** — all faithful arms likely surface the convention via `rag` and can
  write the bullet, so the reward-level comparison mostly measures *any memory*
  vs *no memory* (faithful vs the no-stele control), not *anchor specificity*.
- **Anchor specificity is now read at the channel level**, from
  `memory_evidence.channels` in the receipt: `nudge:recall-file` present iff the
  anchor recall fired. That is the clean anchor signal for this cohort.
- **To isolate anchor recall at the reward level** a future cohort needs a
  lesson whose topic does NOT match the task prompt (so `rag` stays quiet and
  only the file anchor surfaces it). That is a challenge redesign, not a bug —
  tracked separately, not fixed here.

**Re-run predictions (`sbr-002`):** (1) all four arms `outcome != invalid`;
(2) faithful arms show `memory_evidence.injected = true`, correct-anchor with
`nudge:recall-file` in channels, wrong/no-anchor without it; (3) at the reward
level, faithful arms ≥ no-stele on `changelog_unreleased_bullet`, with
anchor-specificity expected only in the channels, not the reward. Still a single
attempt per arm — signal, not a rate.

---

## Results — run `sbr-002`, 2026-07-24 (written after the run)

**Disposition: the APPARATUS is fully validated; the CHALLENGE is retired as a
memory benchmark** (per this pre-registration's `no-stele`-high branch, decided
in advance). `status: completed`, `comparable: true`. Cost **$0.56 total for 4
arms (~$0.14/arm), drain-measured** ($70.16 → $69.60); the litellm per-arm
figure (~$0.70) is the usual ~5× non-cached overestimate.

**All three sbr-001 infrastructure fixes confirmed in the live container:**
- Every arm `outcome: passed`, `atif_integrity_error: null`, sessions found —
  the multi-step layout reader works.
- **Mutation audit fixed, proven by the receipt:** every arm's
  `mutation.json` is `before_sha256 == after_sha256` (identical `e7f7102…`) with
  `only_in_workspace: []`. In `sbr-001` these diverged (`e7f7102 → 4f8b19d`);
  the divergence was entirely Stele's `.stele/` binding, now exempted. So the
  verifier actually ran and graded both sub-flags.
- `memory_evidence.channels` exactly as predicted: correct-anchor
  `["nudge:recall-file", "rag"]`; wrong-anchor and no-anchor `["rag"]` only.
  **The anchor mechanism and its negative control hold at the injection layer.**

**Predictions #1 and #2 held; #4 held (`dotfile_candidate_entry = 1` everywhere).
Prediction #3 (the memory claim) FAILED in the direction the confound predicted:**

| arm | `dotfile_candidate_entry` | `changelog_unreleased_bullet` | reward | anchor channel |
|---|---|---|---|---|
| no-stele | 1 | **1** | 1 | — |
| correct-anchor | 1 | 1 | 1 | `nudge:recall-file` ✓ |
| wrong-anchor | 1 | 1 | 1 | (rag only) |
| no-anchor | 1 | 1 | 1 | (rag only) |

**`no-stele` wrote the changelog bullet** — the no-memory control inferred the
convention from the visible `CHANGELOG.md` (the named confound, §"Named
confound"). With `b(no-stele) = 1`, there is no reward-level memory advantage to
measure: the question is answerable without memory.

**Decision (this pre-registration's pre-decided `no-stele`-high branch):** the
apparatus is sound; the *question* is too easy. Do **not** publish this challenge
as a memory benchmark. A successor cohort needs a close-out fact that is (a)
genuinely unreachable without memory — not inferable from a visible file — and
(b) topically orthogonal to the task prompt, so `rag` can't surface it and only
the file anchor can (see the sbr-002 revision note above and `KNOW-2481`).
Tracked as a follow-up challenge-design task under the eval-portfolio epic.
