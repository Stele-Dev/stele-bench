# Pre-registration — Class C overhead on aider-polyglot (borrowed tasks)

**Committed before the first attempt of this cohort's measured run. Do not edit
after results exist; write a new dated section or a new file for a re-run.**

- Dataset: `aider/aider-polyglot`, downloaded 2026-07-25, **sanitised** with
  `scripts/benchmark-provisioner/prepare_external_dataset.py`
- Sanitised copy: `~/.local/state/stele-bench/datasets/aider-polyglot-clean`
  - 225 tasks · 225 oracle payloads removed · 225 reward fail-safes added
  - `sanitisation.json` sha256
    `1f30ed63820d6091cf9dc33386e5a0e739768836a909e731e4afb96abeeaf400`
  - workspaces sha256
    `5bc6e597395069cbaddd96306a72bb96f335d9fd39520be8e5127e0950934530`
  - tests sha256
    `6db459c139ee97afe25b1fc64fd1bf3120dc9f093c7e449b416771de72390225`

  > The workspaces digest is **unchanged** from the first preparation — what the
  > agent sees was not touched. Only the tests digest moved, by the reward
  > fail-safe described under *Confounds* 9.
- Conditions (arms): `no-stele` (control, stock `claude-code`) and `stele-fresh`
  (treatment — Stele installed, project created in-container by
  `stele project create`, auto-RAG on)
- Model: `anthropic/claude-haiku-4.5`
- Run shape: `oracle.py run-suite`
- Design: **two stages** — a pilot that tests nothing, then a powered run
- Non-inferiority margin: **δ = 0.10** (declared here; see *The margin*)

## Why this run exists

Every other benchmark in this corpus asks a question we wrote, on a graph we
froze, graded by a verifier we wrote. All three are places a favourable result
can be authored without anyone meaning to.

This family gives up all three. The tasks, the workspaces and the verifiers are
`aider-polyglot`'s. Nothing in them rewards memory: they are single-file
Exercism exercises with no history, no prior decisions, and nothing a knowledge
graph could usefully recall. There is no upside for Stele here **by
construction**.

So the claim inverts. Not *does memory help* but **does installing it hurt** —
the overhead an ordinary agent pays on an ordinary coding task for carrying a
memory system it cannot use. That is the honest regression guard for every
number the rest of the corpus produces.

## The prediction (registered before running)

Treatment solves **the same tasks** as control, at some cost in tokens and wall
time. Concretely:

1. `reward` (the borrowed verifier's pass/fail) — **no material difference**.
2. `tokens_input` and `trial_wall_seconds` — **treatment higher**, because the
   install adds a memory block, tools and hooks to every turn.
3. `tool_calls.stele` — **greater than zero in treatment**, or the arm did not
   actually engage the memory system and the overhead measured is only the
   passive kind.

Prediction 1 is the confirmatory claim. 2 and 3 are exploratory.

## The margin

A non-inferiority claim is meaningless without a threshold fixed in advance, so:
**a drop of more than 10 percentage points in solve rate counts as harm.**

Why 0.10 and not something tighter. Required pairs for a paired binary
non-inferiority test scale as `n ≈ (z_α + z_β)² · ψ / δ²`, where ψ is the
proportion of *discordant* pairs (tasks where exactly one arm solved it). At
one-sided α = 0.05 and 80% power that constant is ≈ 6.19:

| δ | ψ = 0.15 | ψ = 0.25 | ψ = 0.35 |
|---|---|---|---|
| 0.10 | 93 tasks | 155 | 217 |
| 0.05 | 371 | 619 | 866 |

The dataset has **225** tasks. A 5% margin is therefore not purchasable here at
any discordance rate — it would need three to four times the corpus. 0.10 is the
tightest margin this dataset can actually support, and it is chosen for that
reason rather than because it is the drop we would consider acceptable.

**What this does and does not license.** A `non_inferior` verdict licenses
exactly one sentence: *installing Stele does not cost more than 10 points of
solve rate on these tasks.* It does not license "no cost", "no measurable cost",
or any statement about a 3-point drop. That distinction goes in the write-up
verbatim.

## Stage 1 — pilot (12 tasks). Tests nothing.

**Purpose:** measure the control arm's baseline solve rate, the discordance rate
ψ, and the actual per-arm cost. All three are needed to size stage 2, and none
of them is available any other way.

**The pilot's solve rates are not a result and are not pooled into stage 2.**
Same discipline as `drift-003` → `drift-r1`: a run whose analysis was chosen
after seeing its data is not evidence, and counting it later would be choosing
the analysis after seeing the data.

**Task selection (fixed by rule, before any run).** Within each of the six
languages, sort task names lexicographically and take the first and the middle
(`names[0]` and `names[len(names)//2]`). Deterministic, recomputable by anyone
from the dataset alone, and settled before a single trial ran:

```
polyglot_cpp_all-your-base           polyglot_cpp_knapsack
polyglot_go_alphametics              polyglot_go_paasio
polyglot_java_affine-cipher          polyglot_java_pig-latin
polyglot_javascript_affine-cipher    polyglot_javascript_poker
polyglot_python_affine-cipher        polyglot_python_pig-latin
polyglot_rust_accumulate             polyglot_rust_nucleotide-codons
```

Stratified by language because the polyglot dataset's entire point is the
language mix, and a sample from one language would measure that language.

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py run-suite aider-pilot-1 \
  --dataset ~/.local/state/stele-bench/datasets/aider-polyglot-clean \
  --task polyglot_cpp_all-your-base --task polyglot_cpp_knapsack \
  --task polyglot_go_alphametics --task polyglot_go_paasio \
  --task polyglot_java_affine-cipher --task polyglot_java_pig-latin \
  --task polyglot_javascript_affine-cipher --task polyglot_javascript_poker \
  --task polyglot_python_affine-cipher --task polyglot_python_pig-latin \
  --task polyglot_rust_accumulate --task polyglot_rust_nucleotide-codons \
  --max-budget-usd 2.00 --acknowledge-paid-suite-cap-usd 48.00 \
  --timeout-seconds 2700
```

> **Amendment, 2026-07-25, before any analysable data existed.** The per-arm
> budget was raised from \$0.50 to \$2.00 (acknowledged ceiling \$12.00 → \$48.00)
> after the first paired task showed the treatment arm being **truncated at the
> cap while the control finished under it** — exactly confound 3. See
> *Confounds* 10 for the evidence. The ceiling is what the fence permits, not
> the expected bill: expected actual spend is ~\$13.
>
> This amendment changes only the budget. The margin, the task selection, the
> measures, the analysis plan and the stop rules are untouched, and no
> analysable result had been produced when it was made — the two arms run under
> the old budget are discarded, not pooled.

`--timeout-seconds` bounds the whole Harbor invocation per arm (image, setup,
agent, verifier). 2700s is roughly four times the measured typical arm and still
bounds a hang; the tasks' own agent and verifier ceilings are 1800s each. Expect
several hours of wall clock for 24 arms — the run is long, not expensive.

### Stop rules for stage 1 (declared now, so they cannot be relaxed later)

Stage 2 does **not** run if any of these holds:

1. **Floor / ceiling.** Control solve rate < 0.15 or > 0.85. A dataset the model
   almost never or almost always solves cannot answer this question at any N,
   and no amount of pairing fixes it.
2. **Required N exceeds the corpus.** If the observed ψ implies n > 225 at
   δ = 0.10, this dataset cannot support the claim. The response is to say so —
   not to quietly widen δ until the number fits.
3. **Differential network access.** See *Confounds* below.
4. **Any oracle-payload hit at all.** One trial reaching the reference solution
   means sanitisation is not holding, and everything stops until it does.

## Stage 2 — the powered run

N computed from stage 1's observed ψ by the formula above and capped at the 225
available tasks.

**Selection rule for stage 2, fixed here.** Sort each language's tasks
lexicographically. Emit round-robin across languages in sorted language order
(cpp, go, java, javascript, python, rust): index 0 of each, then index 1 of
each, and so on, skipping a language once it runs out. Take the first N of that
sequence. The realised list is **written into this file as an appendix before
the run starts**, so the selection is on the record independently of the code
that generated it.

Pilot tasks that fall inside that first N are simply re-run as fresh trials.
Their stage-1 results are still not reused — the pilot data is discarded, not
merged.

The margin stays 0.10. The conditions, model and dataset digests stay as
recorded above; if any digest has moved, it is a different dataset and this
pre-registration does not cover it.

## Measures

**Primary (confirmatory).** `reward` per task per arm → the paired difference in
solve rate, its descriptive two-sided 95% interval, the one-sided 95% lower
bound used by the registered α = 0.05 test (Newcombe method 10), and the
non-inferiority verdict against δ = 0.10.

**Secondary (exploratory, declared under-powered in advance).**
`tokens_input`, `trial_wall_seconds`, `tokens_output`, `claude_reported_usd` —
reported as paired per-task differences with median and range, **no p-value**.

**Descriptive.**
- `tool_calls` — total, and the Stele share. This is the direct measure of how
  much of the agent's work went to memory rather than the exercise.
- `graph_uptake` per task — node counts by type the agent actually persisted,
  whether it created and completed a task for its own work, and
  `seeded_tasks_touched`.
- `external_source_access` per arm.

## Analysis plan (fixed before running)

1. All tasks in a stage run to completion before any result is read. **No
   peeking, no early stopping, no adding or removing tasks after seeing a
   split.**
2. The primary test is **one-sided**: it asks only whether treatment is worse
   than control by more than δ. The direction is fixed here and cannot be
   swapped afterwards.
3. Verdicts are reported with all three of margin, difference and interval. A
   bare verdict word is not a permitted output.
4. **`inconclusive` is reported as `inconclusive`.** It is the expected outcome
   at pilot size and it means *the sample could not decide* — never "no harm
   found", never "no significant difference", never a bare "✓". Failing to
   detect harm is not evidence of safety, and the report styles this verdict
   neutrally rather than as a pass for exactly this reason.
5. Tasks are analysed **intent-to-treat**: every task where both arms produced a
   graded trial counts, including ones both arms failed. A task is excluded only
   when the apparatus disowned it (`outcome: invalid` — oracle-payload hit,
   unauditable trajectory, or a host/container graph disagreement), and every
   exclusion is recorded with its reason in `summary.attempts_excluded`.
6. Secondary measures get no significance test at this N.

## Named confounds (recorded before running)

1. **The welcome task.** A new project seeds an open task, *"Fill this project's
   graph with what the codebase already knows"*, and the agent can read it. It
   may go and do that instead of the exercise — which would show up as large
   overhead plus a correctness hit. **This is what a real new user gets on day
   one, so it is a finding about onboarding, not a fixture bug.** If it happens
   it gets reported as a finding; the fixture is not quietly changed to hide it.
   `graph_uptake.seeded_tasks_touched` makes it visible per task.
2. **Arm order / time position.** Arms run sequentially, so a later arm meets a
   later model-serving moment. `run-suite` alternates order across tasks (odd
   tasks control-first, even tasks treatment-first) and records the realised
   order, so arm is not confounded with position.
3. **Budget truncation.** An arm cut off at its own ceiling fails the task for a
   reason unrelated to memory, and the treatment arm spends more per turn — so
   truncation would bite it first and manufacture exactly the regression this
   run is looking for. Any arm above 90% of its effective cap is flagged in
   `budget_truncation_risk` whatever it scored. If flagged arms are not balanced
   across conditions, the run is not interpretable at that budget.
4. **Internet access.** Every task sets `allow_internet = true`, and these
   Exercism exercises have published solutions all over the web. This is the
   same differential-cheat shape as the bundled payload — the treatment arm is
   instructed to investigate and explores more — but the signal is ambiguous: a
   network call may equally be a package manager fetching a dependency. It is
   therefore **recorded, not auto-excluded**, because dropping those trials
   would bias against whichever arm installs more dependencies.
   **Decision rule:** if the per-arm rates differ by more than 20 percentage
   points, the run is not interpretable and the flagged trajectories are read by
   hand before anything is reported.
5. **The bundled reference solution.** Every task shipped
   `environment/workspace/.oracle/solution.enc`, decryptable with a password
   published in the adapter's own `solve.sh`. Removed by sanitisation, re-audited
   at run time, and detected in the trajectory anyway; a hit marks the arm
   invalid and drops the **whole paired task** rather than scoring the honest
   arm. Checked separately and recorded here: the verifier tree (which holds
   `tests/.meta/example.*`, the reference implementation) is uploaded by
   Harbor's verifier **after** the agent phase ends — `single_step._run` is
   `_run_agent()` → `_upload_agent_logs()` → `_collect_artifacts()` →
   `_run_verifier()` — so `/tests` does not exist while the agent is working,
   and the sanitised Dockerfiles copy `workspace/` and nothing else.
6. **One project per task.** Each treatment task provisions its own principal
   and project, so no task inherits what the previous one wrote. This is
   deliberate: the question is what a *fresh* install costs, not what an
   accumulated graph is worth. It also means a free-tier private-project limit
   is never hit, since no principal creates a second project.
7. **Model non-determinism.** Both arms are the same model on the same task, so
   some discordance is pure run-to-run variance rather than an arm effect. This
   is not correctable at k = 1; it is what ψ measures, and it is why the
   required N is estimated from ψ rather than assumed.
8. **Asymmetric trial loss at setup.** Found before the first paid run, in a
   free install-only probe. Harbor's default agent-setup ceiling is 360s, and on
   these images the **stock** `claude-code` install alone — `apt-get update`
   plus downloading the Claude Code bootstrap — exceeds it: 1 of 9 control-arm
   setups aborted with `AgentSetupTimeoutError`. The treatment arm's setup does
   strictly more work (upload a ~96 MB artifact, install the plugin, create a
   project, run the structural preflight), so a ceiling either arm can graze
   will be crossed by the treatment arm **more often**, and trials would vanish
   in the direction that flatters the control. Both arms are therefore given the
   **same** raised ceiling (1800s); setup invokes no model, so this costs wall
   clock and nothing else.
   **Check at analysis time:** setup-timeout exclusions must be balanced across
   arms. If they are not, the run is reported as invalid rather than analysed —
   a paired comparison that loses more treatment trials than control trials is
   not a comparison, and the imbalance flatters the control by construction.
9. **A build failure scored as an infrastructure error.** Found on the first
   paid arm, which was stopped after \$0.32. `aider-polyglot`'s `test.sh` ends
   with its own verdict — `exit_code == 0 → 1`, else `0` — but every
   compiled-language task (cpp, java, rust: **103 of 225**) also has earlier
   `exit 1` paths for "build failed" that never reach it. Harbor then finds no
   reward file, raises `RewardFileNotFoundError`, and the trial is recorded as
   `infrastructure-error` rather than a failure.

   This is the most dangerous confound in this file, because it is
   **self-concealing and differential**. A compile failure is the most common
   failure mode for a weak model here, and an excluded trial leaves the
   *denominator* instead of scoring zero — so the arm that fails to compile more
   often would look **better**, its worst results vanishing from the analysis
   rather than counting against it. Nothing in the summary would have looked
   wrong.

   **Fix, applied at dataset preparation and recorded in the manifest:** a
   `trap ... EXIT` is inserted after `set -e` in every task's `test.sh`, writing
   reward 0 **only if the script wrote none** (`[ -f … ] ||`). It applies the
   script's own stated rule on the paths where the script forgets to reach it,
   and can never override a real verdict. Bash runs an `EXIT` trap when the
   script decides to stop and **not** when the process is killed, so a build
   failure becomes an honest 0 while an OOM-killed or timed-out verifier still
   produces no reward file and is still reported as infrastructure — the
   distinction comes from bash semantics rather than from us guessing at
   somebody else's output. All three behaviours were verified in a container.

   **Check at analysis time:** `infrastructure-error` outcomes should now be
   rare. If they are not, or are imbalanced across arms, the run is reported as
   invalid rather than analysed.
10. **Budget truncation, observed rather than predicted.** Confound 3 said the
    treatment arm would hit a shared ceiling first. It did, on the first paired
    task, at the original \$0.50/arm budget:

    | arm | steps | tools | cost | outcome |
    |---|---|---|---|---|
    | control | 4 | Read×2, Edit×2 | \$0.29 | completed, failed the tests |
    | treatment | 6 | Read×2, Edit×2, **Bash×2** | \$0.51 | **budget_exhausted** |

    The treatment arm was killed mid-tool-call (`terminal_reason:
    budget_exhausted`, `result_subtype: error_max_budget_usd`), which also
    truncated its trajectory — `missing_atif_tool_results` — so the trial was
    correctly marked `invalid` rather than scored as a failure.

    Two things are worth recording, because they shape how the eventual result
    must be read:

    - **The extra cost was not Stele tool calls.** `tool_calls.stele` was
      **zero**. The overhead is passive — the memory block, tool definitions and
      hook output riding on every turn — not the agent stopping to use memory.
    - **The treatment arm was doing more work, not worse work.** Its two extra
      turns were `Bash` calls attempting to build and test; the control declared
      done after editing and never compiled. Whether that thoroughness is
      Stele's guidance or run-to-run variance is unanswerable at n = 1 and must
      not be reported as a finding — but it is the reason a truncating budget
      penalises the treatment arm specifically, and it is why the fix is a
      higher ceiling rather than a shorter leash.

    Budget raised to \$2.00/arm. The check at analysis time is unchanged and now
    matters more: any arm above 90% of its effective cap is flagged in
    `budget_truncation_risk` whatever it scored, and if flagged arms are not
    balanced across conditions the run is not interpretable at that budget.

## What this run cannot say

- Nothing about whether memory **helps**. There is nothing here to recall; that
  is what the rest of the corpus is for.
- Nothing about a drop smaller than the margin.
- Nothing about other models, other task families, or a graph with history in
  it. `stele-fresh` is a day-one install and only that.

## Post-pilot audit and stage-2 decision (2026-07-25)

This section was added after `aider-pilot-1`; it records why the powered run
described above is **not authorised under this pre-registration**. The pilot
remains a discarded sizing run and is not promoted into evidence.

1. **The stage-2 point estimate is unstable.** Ten analysable pairs produced
   one discordance, so ψ = 0.10 but its Wilson 95% interval is approximately
   [0.018, 0.404]. The implied N ranges from 12 to 250. The point estimate 62
   is not a defensible fixed powered sample, and the upper end exceeds the
   entire corpus.
2. **An adaptive expansion would contradict the registered analysis plan.** The
   plan says every stage runs to completion without peeking, early stopping, or
   adding tasks after seeing the split. A staged expansion invented after this
   pilot therefore needs a new pre-registration and an independently discarded
   sizing sample; it cannot be smuggled into stage 2 as if it were the original
   fixed design.
3. **The implementation used the wrong bound for the registered alpha.** The
   sizing formula is for one-sided α = 0.05, but the original verdict compared
   the margin to the lower end of a two-sided 95% interval, which tests at
   one-sided α = 0.025. The implementation now preserves that descriptive
   interval and computes a separate one-sided 95% lower bound for the verdict.
   The pilot's verdict remains inconclusive under either bound.
4. **The paid ceiling is not currently fundable.** At the registered
   $2.00/arm ceiling, 62 tasks require an explicit $248 pledge before the first
   arm starts. The eval key did not have that headroom after the pilot and the
   production reserve, so the money fence correctly prevents the run.
5. **The ATIF result concern is withdrawn.** Retained trajectories do contain
   tool results at `steps[].observation.results[].content`; the handoff looked
   for a flat `output` field that is not the ATIF shape. No observability repair
   is required for this reason.

Decision: keep Class C and its pilot as a useful regression apparatus, but do
not spend on its powered run now. A future powered attempt needs a new
pre-registration with a stable sizing design, an exclusion/replacement rule,
and a fundable ceiling. Until then the higher-value compounding experiment is
the benchmark that can test whether this measured deposit ever earns a return.
