# Benchmark oracle and local graph provisioner

This directory contains the host-side oracle used for the published runs and a
reference implementation of the benchmark package's JSON-over-stdin graph
provisioner protocol. The reference adapter targets a compatible local Supabase
database and knows Stele's current schema. It does not add a database function
or migration.

The oracle is not coupled to that adapter. Set `STELE_BENCH_PROVISIONER` to an
executable implementing the same `prepare`, `restore`, `snapshot`, `destroy`,
and `cleanup` operations to run against another Stele integration. This is the
public rerun boundary; access to the Stele source repository is not required.

Inspecting the v0.1 evidence, validating its manifest, certifying the public
DotState source, rendering reports, and building the task image need no Stele
source or backend. Starting a new treatment run additionally needs a compatible
Stele executable, backend, and provisioner adapter. The exact system-under-test
executables from the preliminary runs are identified by digest in the results
but are not licensed as part of this MIT repository.

## Maintainer quick start

`oracle.py` is the normal operating surface. The lower-level scripts documented
below remain useful for fixture work and debugging, but routine benchmark runs
should go through this command so the two conditions share one pinned
configuration and always produce durable receipts.

Prerequisites for a new run are Python 3.12+, `uv`, Git, Docker, Harbor 0.20.x,
a compatible Stele executable and backend, and a dedicated Vercel AI Gateway
key exported as `AI_GATEWAY_KEY_EVAL`. The reference adapter also needs the
Supabase CLI. Prepare the public DotState commit with `stele-bench
source-prepare`; pass the printed object-cache path through `--source`.

When using the reference adapter, start and reset a compatible local backend
before the first run:

```sh
supabase start
supabase db reset

cd eval/stele-bench
uv sync --locked
uv run ../../scripts/benchmark-provisioner/oracle.py doctor
```

For a different adapter:

```sh
export STELE_BENCH_PROVISIONER=/absolute/path/to/benchmark-provisioner
uv run ../../scripts/benchmark-provisioner/oracle.py doctor \
  --source "$STELE_BENCH_SOURCE" \
  --artifact-manifest /absolute/path/to/artifact-manifest.json
```

`doctor` is zero-cost. It validates the pinned Dotstate commit, frozen fixture,
challenge and verifier digests, Docker architecture, Harbor version, local
backend, Gateway-key presence, and any current integration artifact without
printing credentials. It also runs the packaged-input certification and the
shared-Gateway reserve fence described below, reporting them under
`checks.certification` and `checks.gateway_reserve` — so "doctor is green"
means a paid run would actually be allowed to start.

### Certification runs before anything is created or spent

`doctor`, `preflight`, and `run-pair` all call `stele-bench certify` first — a
static, zero-model probe of the packaged corpus, the evidence corpus, every
declared graph arm, and the selected challenge (see the eval package README for
what it rejects and why). `run-pair` certifies **before** it creates the pair
directory and before the first token, because a contaminated corpus or a
benchmark-aware graph makes the whole pair uninterpretable, and discovering that
after the money is gone is too late. The resulting receipt is persisted at
`runs/<pair-id>/certification.json` and referenced from the pair receipt's
`provenance.certification`, including the list of paths redacted from the corpus.

The concrete thing this caught: the pinned Dotstate commit ships a `CLAUDE.md`
and a `.claude/skills` tree that a coding harness loads into context on its own,
plus a `.stele/project.json` binding a real project. Packaging those handed every
arm — including the supposedly memory-free control — a hand-written memory file,
and `CLAUDE.md` states the exact changelog obligation the session-boundary
challenge grades. Packaging now removes that class of file uniformly and records
what it removed; certification then proves the result is clean.

Build the exact Linux integration executable from the current Stele worktree,
including uncommitted changes:

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py build-local-artifact
```

The build records the executable digest, Git HEAD, tracked diff digest,
untracked-content digest, dirty status, target architecture, and build-tool
versions. It stores the executable and a mode-0600 manifest under
`$XDG_STATE_HOME/stele-bench`, or `~/.local/state/stele-bench` when
`XDG_STATE_HOME` is unset.

Before spending model tokens, exercise the matched stock and configured Harbor
containers, real plugin installation, project binding, backend access, and
structural preflight:

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py preflight keypress-preflight
```

`preflight` explicitly removes `AI_GATEWAY_KEY_EVAL` from its child environment
and invokes Harbor in install-only mode, so it cannot run the agent or make a
model request. Its durable receipt and evidence paths can be read with
`oracle.py inspect keypress-preflight`.

Run the matched no-Stele and faithful arms for one challenge:

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py run-pair keypress-001 \
  --challenge input-keypress-architecture \
  --acknowledge-paid-pair-cap-usd 10.00

uv run ../../scripts/benchmark-provisioner/oracle.py inspect keypress-001
```

`run-pair` runs the two arms as separate Harbor jobs with identical source,
challenge, model, turn cap, and budget. The default is the pinned keypress
challenge with Claude Haiku 4.5, 60 turns, and a $5 per-arm Claude CLI cap.
The acknowledgement must equal twice `--max-budget-usd`. A missing or stale
acknowledgement fails before either paid arm starts. That cap is a
post-response stop condition, not a guaranteed Gateway ceiling — which is why
the reserve fence below exists. This command makes paid model calls.

### The shared-Gateway reserve fence

The eval key draws on the **same** Vercel AI Gateway credit balance as
production. Per-key budgets are **caps, not reservations**: they bound what a
key may spend and set aside nothing for anyone else. On 2026-07-12 an eval run
spent $109 while sitting comfortably under its $150 key budget, drained the
shared balance, and got production's assistant and embedding keys declined —
they had spent $3.90 and $0.05. The per-pair acknowledgement cannot prevent
that either: it bounds what one run may spend, not what is left for everything
else.

`doctor` and `run-pair` therefore verify three things through
`gateway_reserve.py`, before `run-pair` creates any state and before the first
token:

1. **Key identity** — the exported key's SHA-256 must match the configured
   fingerprint. This is what stops a run being billed to the production key
   because the wrong value was exported into the shell.
2. **A per-key budget that exists** — the acknowledgement asserts the key has
   "an external budget ceiling"; the Gateway quota API is now asked directly,
   and an absent, inactive, mismatched, or exhausted budget fails.
3. **Shared headroom** — the team balance must cover the configured production
   reserve *plus* the acknowledged pair ceiling.

and one more once the run is in flight:

4. **Observed drain** — between the two arms the balance is measured again, and
   the second arm does not start if more has drained than the pair declared.
   Preflight proves the pair *fits* above the reserve; only this proves it
   *behaved*. Observed drain is the drop in the shared balance, so it also
   counts draws by anything else on the account — deliberately, because the
   next dollar past that line comes out of the reserve either way. A flaky GET
   fails open and is recorded as unobserved; a rejected key does not fail open.

Configuration lives at `<state-root>/gateway-reserve.json`, mode 0600 (override
with `--gateway-reserve-config`). Nothing in it is a secret:

```jsonc
{
  "schema_version": 1,
  "key_sha256": "<sha256 of the dedicated eval key>",
  "key_id": "<AI Gateway API key id>",
  "key_name": "<the key's name in the dashboard>",
  "production_reserve_usd": "25.00",   // what prod must still be able to draw
  "calibrated_at": "2026-07-23",
  "max_calibration_age_days": 30,   // optional, default 30
  "require_key_budget": true        // optional, default true
}
```

Gather the two identifiers:

```sh
printf %s "$AI_GATEWAY_KEY_EVAL" | shasum -a 256   # key_sha256
vercel api "/v1/api-keys?purpose=ai-gateway"       # key_id and name
```

Inspect the fence at any time without running anything (a cap of `0` reports
the current state and still fails on anything unverifiable):

```sh
uv run ../../scripts/benchmark-provisioner/gateway_reserve.py \
  --config ~/.local/state/stele-bench/gateway-reserve.json --pair-cap-usd 10.00
```

**The Gateway's numbers are advisory.** Credits move for reasons this process
cannot see, so `calibrated_at` records when you last reconciled the reserve
against the Vercel dashboard, and the fence **fails** once that is older than
`max_calibration_age_days`. The dashboard remains authoritative for actual
billing; the preflight's non-secret values are persisted under the pair
receipt's `billing.gateway_reserve_preflight`, and the between-arms
observations under `billing.gateway_drain_checks` (with
`billing.gateway_drain_stop` set when the tripwire fired).

`production_reserve_usd` is what production must still be able to draw after
this run spends everything it declared — the assistant's and embedder's own
budgets, not a guess. Recalibrate it whenever those change.

The pair receipt is written atomically to
`~/.local/state/stele-bench/runs/<pair-id>/pair.json` and links the per-arm
receipts and retained Harbor evidence. Results remain exploratory until repeated
with a pre-registered design.

### Multi-variant discrimination (`run-quad`)

A pair compares no-stele vs one faithful graph. A memory question needs more
than that: the *same* control against several graph variants that should
behave differently. `run-quad` runs one shared no-stele control against N
faithful graph-variant arms and writes a single comparable receipt.

The session-boundary-recall cohort is the motivating case — three variants of
the same one-lesson graph:

- **correct-anchor** — the lesson is anchored to the file the first turn
  touches; anchor-triggered recall should surface it on the follow-up.
- **wrong-anchor** — anchored to a plausible but untouched file; a negative
  control that must *not* leak the lesson.
- **no-anchor** — no file anchor at all; isolates what ordinary FTS/semantic
  recall does with only the vague follow-up (this is the arm that needs real
  embeddings — see the fixture cohort section).

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py run-quad sbr-001 \
  --cohort-root ../../eval/stele-bench/fixtures/session-boundary-recall-v1 \
  --challenge session-boundary-recall \
  --max-budget-usd 1.00 \
  --acknowledge-paid-quad-cap-usd 4.00
```

The acknowledged cap is the per-arm budget times the arm count (1 control +
one per `--variant`, default three → `1.00 × 4 = 4.00`). Every variant fixture
is certified before any state or spend; the shared control runs once; the
between-arm drain check stops the run before the next variant if the balance
falls. The receipt at `runs/<quad-id>/quad.json` carries one delta per variant
against the shared control and a `memory_evidence` map (currently `null` per
variant: trajectory retention that would populate it via
`stele_bench.memory_evidence` is not wired in the runner yet — tracked on
TASK-2473). **Pre-register the prediction before running** (correct-anchor
recalls the CHANGELOG lesson, wrong-anchor does not leak it, no-anchor/no-stele
mostly miss it) and label a single quad exploratory until repeated with N.

### Repeated attempts for a rate (`run-repeat`)

A pair is one attempt per arm — one coin flip each. It can show that a memory
mechanism fires; it cannot say how often, and a benchmark that reports `1.0`
from a single attempt has reported a coin flip. `run-repeat` runs the same
pair N times under **one** certification and **one** money fence, and summarises
the result as a rate rather than a point.

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py run-repeat drift-r1 \
  --attempts 5 \
  --challenge dotfile-drift-detection \
  --fixture ../../eval/stele-bench/fixtures/dotfile-drift-v1/fixture.json \
  --max-budget-usd 1.50 \
  --acknowledge-paid-repeat-cap-usd 15.00
```

#### Choosing the control

`--control-condition` and `--treatment-condition` decide what the comparison
means. The default pair (`no-stele` vs `faithful`) asks *what is the graph worth
at all*. Swapping the control to `faithful-norag` asks a different question —
*what is proactive injection worth* — by holding the graph constant and removing
only the prompt-time push:

```sh
  --control-condition faithful-norag --treatment-condition faithful
```

`faithful-norag` is a full Stele install: same artifact, same restored graph,
same tools, skills and memory block. The single difference is that the auto-RAG
hook is neutralised in the installed plugin, so anything the control arm recalls
it had to go and fetch. Because both arms then have a graph, the receipt keeps
**memory evidence for both** — whether the un-pushed arm reached the memory on
its own is the measurement.

The manipulation is verified in-container, after the runtime config is staged,
against the tree the harness actually reads — and it fails closed in **both**
directions. A `faithful-norag` arm whose wiring survived aborts; so does a
`faithful` arm whose wiring is missing. That second direction is the one worth
having: an arm that silently lost its injection scores as evidence *against* the
feature, which is a result nobody would think to question. The verdict lands in
each arm's receipt under `evidence.injection_channel`.

To check the strip on the real image without spending a cent, run the
install-only path with `--auto-rag-injection off`: it performs the same strip and
the same verification, and no model is ever invoked.

The acknowledged cap covers **the whole repeat** — per-arm budget × 2 × attempts
(`1.50 × 2 × 5 = 15.00`) — not one attempt, so the fence cannot be defeated by
running many small pairs. The between-arm drain check runs between *every* arm
across the repeat and stops it if the shared balance falls.

Three properties the receipt at `runs/<repeat-id>/repeat.json` is built to give
you:

- **Arm order alternates.** Odd attempts run control-first, even attempts
  treatment-first, and the realised order is recorded per attempt. Arms run
  sequentially, so without this the arm is confounded with its position in the
  run — a later arm sees a later model-serving moment.
- **Each attempt restores its own graph.** Every arm invocation carries its own
  run id, so the runner provisions and destroys a fresh project per attempt and
  no attempt inherits another's state.
- **Nothing is dropped silently.** An attempt where either arm failed to
  complete is listed in `summary.attempts_excluded` with its reason, because a
  repeat that quietly analyses 3 of 5 reads exactly like one that ran 3.

`summary` splits the measures by what the sample size can actually support:

- **Binary measures** (`cited_decision`, `reward`) get a rate, a **Wilson 95%
  interval**, and a **one-sided Fisher exact p** for treatment > control. Wilson
  rather than the textbook normal interval because the normal one collapses to
  zero width at 0/5 and 5/5 and claims certainty from five observations. At
  N = 5 a perfect 5/0 split gives p ≈ 0.004, 4/0 gives ≈ 0.024, and 3/0 gives
  ≈ 0.083 — which does **not** clear α = 0.05.
- **Continuous measures** (`investigative_tool_calls`, `tokens_input`,
  `trial_wall_seconds`) get the paired per-attempt difference, its median and
  range, and how many attempts moved the right way — and deliberately **no
  p-value**. A continuous measure has no usable power at single-digit N, and
  attaching a test would dress an exploratory observation up as a result.

Two things it will not do for you. The test is **one-sided**, so the direction
must be registered before the run — picking the direction after seeing the split
is not a test of anything. And any arm that spent more than 90% of its own
budget ceiling is flagged in `budget_truncation_risk`: an answer cut off at the
ceiling scores like a failure to recall for a reason that has nothing to do with
memory.

Write the pre-registration first (`eval/stele-bench/preregistrations/`), commit
it, and only then run.

### Borrowed tasks: what does installing it cost? (`run-suite`)

Every run shape above asks a question we wrote, on a graph we froze, graded by a
verifier we wrote. All three are places a favourable result can be authored
without anyone meaning to. `run-suite` gives up all three: it pairs the arms
across tasks taken from **someone else's** benchmark, none of which reward
memory, and the only thing we supply is the agent configuration.

That inverts the claim. There is nothing here for memory to help with, so the
question is not *does it help* but *does installing it hurt* — the honest
regression guard for the overhead the install adds on every ordinary task.

#### Prepare the dataset first

A borrowed task set is built for leaderboard runs, where a shared cheat inflates
every model about equally and mostly does not change the ranking. A paired
comparison has no such tolerance. `aider/aider-polyglot` ships the reference
solution inside the agent's own working directory (`.oracle/solution.enc`,
decryptable with a password published in the adapter's own `solve.sh`), and the
arm that explores the filesystem more would reach it more often — a spectacular
false positive on the one family whose job is to be the honest control.

```sh
uv run ./prepare_external_dataset.py ~/downloads/aider-polyglot ./aider-clean
```

This copies the dataset, strips the payload, audits what the image would copy,
and writes `sanitisation.json` with a digest of every task's workspace. Run
from the copy, never from the registry — the tasks stop being a moving target,
and `run-suite` refuses a directory that has not been through this.

#### Run it

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py run-suite aider-s1 \
  --dataset ./aider-clean \
  --non-inferiority-margin 0.10 \
  --max-budget-usd 0.50 \
  --acknowledge-paid-suite-cap-usd 20.00
```

The cap covers the whole suite — per-arm budget × 2 × tasks — and the dataset is
re-audited and re-hashed **before** the cap is validated, because the task count
is what the cap is computed from.

What the run shape does that the others do not:

- **Each task gets a brand-new project**, created in-container by the same
  `stele project create` a new user runs. Not an empty fixture approximating
  one: a real new project seeds a root component, a welcome task and a welcome
  decision, and those matter — with them, recall retrieves something instead of
  short-circuiting on an empty graph, so what gets measured is the real path.
- **After every treatment trial the host reads that project back** out of the
  database and records what the agent actually wrote, split by node type, plus
  whether it created and completed a task for its own work. A trajectory shows
  what was attempted; only the graph shows what stuck. If the graph is untouched
  at the end, the overhead bought nothing — which is itself the result.
- **Grading is the borrowed benchmark's own verifier.** We do not wrap it, add a
  mutation audit, or adjust it. A verdict we adjusted is a verdict we authored.
- **A trial that reached the reference solution is excluded, not scored.** The
  trajectory is scanned for the payload by name and by act (`openssl enc … -d`),
  and a hit marks the arm invalid — which drops the whole paired task, never
  scores it on one honest arm.

#### The claim is non-inferiority, and it has its own discipline

`summary` carries the per-measure statistics every other run shape reports, plus
two things specific to a no-harm claim:

- `solve_rate` — the paired difference in solve rate with a descriptive
  two-sided 95% interval and a separate one-sided 95% lower bound (Newcombe's
  method 10). Paired, because both arms ran the same tasks and a
  hard task is hard for both; treating them as independent samples throws that
  away and produces an interval far wider than the data supports.
- `non_inferiority` — the verdict against the margin declared on the command
  line **before** the run. A margin chosen after seeing the number is not a
  margin.

The verdict has three values, and the third is the one that matters:
`non_inferior` (the one-sided lower bound clears −margin), `inferior` (the
upper bound is at or below it), and **`inconclusive`** — the sample cannot
decide. Failing to detect harm is not evidence of safety, and at single-digit
task counts `inconclusive` is the usual and correct answer. The report renders
it in the neutral style, never the passing one.

One outcome to register in the pre-registration before spending anything: the
seeded welcome task asks the reader to go and fill the project's graph, and an
agent may do that **instead of** the exercise, showing up as large overhead plus
a correctness hit. That is what a real new user gets on day one, so it is a
finding about onboarding rather than a fixture bug. `graph_uptake` reports
`seeded_tasks_touched` per task so it is visible in the record rather than
inferred from a bad score.

### Keeping the result (`export-result`, `report`)

A run receipt lives in `~/.local/state/stele-bench/runs/<id>/`, on one machine,
with no backup, next to gigabytes of corpus trees and trajectories that
`clean --delete-work` exists to delete. That is the right home for forensics and
the wrong one for a record: a reimaged laptop takes every score with it, and
nothing can be compared across runs.

So the small, durable part gets copied into the repository and **committed**:

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py export-result drift-r1
uv run ../../scripts/benchmark-provisioner/oracle.py report
```

`export-result` writes `eval/stele-bench/results/<challenge>/<date>-<run>.json` —
attempt envelopes, per-arm aggregates with their intervals, the treatment-vs-
control verdict, memory uptake, and the pinned provenance. `report` renders every
record into a single self-contained `results/history.html` (no scripts, no fonts,
no network; it opens from disk).

A quad exports **one record per variant**, because a quad is not one comparison —
it is N comparisons sharing a control arm, and `correct-anchor` and `wrong-anchor`
are asking opposite questions.

#### What makes two runs comparable

The view groups runs into trend lines keyed on the question's identity: challenge
id, fixture id, model, both conditions, and the digests of the challenge manifest
and the verifier. Change any of those and the runs get **separate lines** plus a
"Not comparable" note naming the field that differs — a changed verifier moves the
score without anything about memory changing, and plotting those together
manufactures a regression that never happened.

The **artifact digest is deliberately not part of the key.** The build is the
thing under test; keying on it would give every build its own island and switch
regression detection off exactly when it matters. It is labelled on each point
instead, so a step in the trend can be attributed to the build that caused it.

A borrowed suite fills those fields with what it has: the dataset directory is
the challenge, `created-per-task` is the fixture, and the two digests are the
tasks' workspaces and the tasks' tests. They mean the same thing they always do
— what was asked, and how it was graded — so an upstream change to either one
breaks the line rather than moving the score. A suite is **one** record, not one
per task: the dataset is the question and the tasks are the sample, so splitting
it would put twenty single-attempt lines on the chart, the least informative
possible reading of the same data.

Everything is drawn as a rate **with its Wilson interval**, never a bare point. At
five attempts, 4/5 and 5/5 are the same measurement, and the first run of
`dotfile-drift-detection` makes the case on its own: one attempt scored 1.00
[0.21, 1.00] and the pre-registered N=5 repeat of the identical question scored
0.20 [0.04, 0.62]. Same line, overlapping intervals — the single run never
established anything, and a points-only chart would have shown a catastrophic
regression instead of a sample size.

#### Retention: what is kept and what is disposable

| | Lives in | Survives | Delete when |
|---|---|---|---|
| Result records | `eval/stele-bench/results/` (git) | reimage, `clean --delete-work` | never |
| Pre-registrations | `eval/stele-bench/preregistrations/` (git) | reimage | never |
| Run + arm receipts | `~/.local/state/stele-bench/runs/<id>/` | `clean` without `--delete-work` | after `export-result` |
| Corpus trees, Harbor jobs, ATIF trajectories, session transcripts | `runs/<id>/work/` | nothing | once the question is settled |

**Export before you clean.** Trajectories are the expensive-to-replace half:
replaying retained ones is free and has already produced findings a re-run would
have cost real money to get — including one metric-scoping bug caught before it
invalidated half an experiment. Keep the work tree for any question still being
debugged; delete it once the question is settled and its record is committed.

### Recording billing

After checking the Vercel dashboard, record its authoritative total without
hand-editing JSON:

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py record-billing keypress-001 \
  --gateway-total-usd 3.41
```

Recover or clean an interrupted run by its pair id. Cleanup is idempotent and
does not require the original process to still exist:

```sh
uv run ../../scripts/benchmark-provisioner/oracle.py clean keypress-001
uv run ../../scripts/benchmark-provisioner/oracle.py clean keypress-001 --delete-work

cd ../..
supabase stop
```

Faithful execution currently accepts one challenge per invocation because
Harbor supplies one credential set per configured job. `run-pair` deliberately
orchestrates one isolated faithful job instead of sharing graph state or recall
budget across questions. A future batch command should aggregate several of
these isolated pair runs rather than weakening that boundary.

## Freezing a faithful project graph

Freezing is a two-step operation. `embed_project_export.py` sends the exact
production embedding input for every node (`title`, two newlines, then `body`)
to the Vercel AI Gateway using `openai/text-embedding-3-small`. It writes a
canonical sidecar containing the source-export digest, input digest, model,
dimensions, and full vector for each node in stable short-ID order. The script
reads the Gateway credential only from `AI_GATEWAY_KEY_EVAL`; it never writes
the credential to the sidecar or diagnostics.

`freeze_project_export.py` then converts the strict version-2 JSON emitted by
`stele project export` and that exact sidecar into a portable fixture manifest
and a `stele-canonical-graph-v2` artifact. The converter accepts no source UUIDs
in its output, sorts nodes and edges by stable short IDs, normalizes timestamps,
proves that the sidecar covers the source nodes exactly, and verifies each
node's embedding-input digest. It quantizes every complete 1,536-dimensional
vector to IEEE-754 binary16 for the backend's `halfvec(1536)` representation;
it does not synthesize or sparsify embeddings.

The current DotState faithful export is trunk-only. The export command reported
that **10 workspace-scoped nodes were omitted**; that omission is intentional in
`dotstate-faithful-20260723-v1` and must be revisited if a later benchmark needs
those overlays. The graph fixture contains no challenge prompts or answer keys.

Generate the embedding sidecar once, then freeze the fixture. Both commands
refuse to overwrite existing output unless `--replace` is explicit:

```sh
AI_GATEWAY_KEY_EVAL=... \
python3 scripts/benchmark-provisioner/embed_project_export.py \
  /private/tmp/dotstate-faithful-export.json \
  /private/tmp/dotstate-faithful-embeddings.json

python3 scripts/benchmark-provisioner/freeze_project_export.py \
  /private/tmp/dotstate-faithful-export.json \
  eval/stele-bench/fixtures/dotstate-v1 \
  --embedding-sidecar /private/tmp/dotstate-faithful-embeddings.json
```

Use `--validate` in routine checks to prove the frozen bytes still match the
source export and embedding sidecar without writing anything. `--replace` is an
explicit maintainer operation for deliberately refreshing a differing sidecar
or frozen fixture; on the freezer it cannot be combined with `--validate`.

## Building the session-boundary-recall fixture cohort

`build_session_boundary_fixtures.py` is a different shape of fixture builder:
instead of converting a real `stele project export`, it authors one small
lesson node directly and writes three fixture variants that share everything
except the lesson's `anchor_files` (`correct-anchor`, `wrong-anchor`,
`no-anchor`) into `eval/stele-bench/fixtures/session-boundary-recall-v1/`.
See the paired `session-boundary-recall` challenge and its module docstring
for the full design.

```sh
python3 scripts/benchmark-provisioner/build_session_boundary_fixtures.py \
  eval/stele-bench/fixtures/session-boundary-recall-v1
```

Its embeddings are deterministic **placeholders**, not real
`text-embedding-3-small` vectors (no `AI_GATEWAY_KEY_EVAL` call is made) —
schema-valid enough to exercise restore, blindness, and harness plumbing
end-to-end, but not semantically meaningful. The `correct-anchor` and
`wrong-anchor` conditions test the structural anchor-file match, so they do
not depend on embedding fidelity; the `no-anchor` (ordinary recall) condition
does, and needs a real `embed_project_export.py`-style pass before it can
support a publishable claim. `--replace` works the same as on the freezer
above.

The restored project's **identity** — its name and description — is what an
agent sees first, so it must read like the real project it stands in for. An
earlier revision called it a "Session-Boundary Recall Fixture", which told the
agent it was inside an experiment; certification now rejects that, and the
builder names the project after the real one. Re-run the builder with
`--replace` after any edit and re-certify each variant.

The executable accepts five operations:

```sh
python3 scripts/benchmark-provisioner/provision.py prepare
python3 scripts/benchmark-provisioner/provision.py restore
python3 scripts/benchmark-provisioner/provision.py snapshot
python3 scripts/benchmark-provisioner/provision.py destroy
python3 scripts/benchmark-provisioner/provision.py cleanup
```

Each invocation reads one JSON request from stdin and writes one JSON response to
stdout. Diagnostics go to stderr. The default database target is the local CLI
container `supabase_db_stele`; override it with
`STELE_BENCH_DB_CONTAINER`, `STELE_BENCH_DB_USER`, or
`STELE_BENCH_DB_NAME`. These values are passed as subprocess arguments and are
restricted to simple local names. No database password is read or printed.

## Attempt credential handoff

Set `STELE_BENCH_ATTEMPT_ROOT` to a private host directory outside the evaluated
project. Before restore, call `prepare` with
`{"run_id":"...","harnesses":["claude"]}`. Prepare creates a fresh user through
the local Supabase Auth API and writes three mode-0600 files below
`$STELE_BENCH_ATTEMPT_ROOT/<sha256(run_id)>/`:

- `credentials.json` — the exact access/refresh session shape consumed by the CLI;
- `preferences.json` — the requested installed-harness preferences;
- `state.json` — private owner/run binding used only by this tool.

After the graph transaction succeeds, restore atomically adds a fourth mode-0600
file beside them:

- `project.json` — the restored project's exact `ActiveProject` cache shape,
  ready to place at `.stele/project.json` in the isolated checkout. A fresh
  checkout starts with no manually activated workspace overlays.

Prepare returns only `ok` and the non-secret attempt directory key. Tokens and
passwords are never written to stdout, stderr, receipts, or the evaluated
project. The Harbor-side setup may upload `credentials.json` and
`preferences.json` directly into the isolated user's `~/.config/stele/` before
installing the plugin, and may upload `project.json` to `.stele/project.json` in
the isolated checkout. The private state file stays on the host.

Local Auth discovery uses `supabase status -o env`. Tests or orchestration may
instead set `STELE_BENCH_SUPABASE_URL` and
`STELE_BENCH_SUPABASE_ANON_KEY`; the URL is rejected unless it resolves to
localhost. The publishable key is used only to create and sign in the fresh
principal.

Restore imports a canonical graph artifact in one transaction. It disables only
the embedding-enqueue trigger during node insertion, leaving hierarchy and
full-text maintenance enabled, then proves queue stability through the public
restore/snapshot receipts. Snapshot reads the database back, validates every
full binary16 1,536-dimensional vector, hashes the reconstructed canonical
graph, and runs deterministic full-text, semantic, and graph probes selected
from the restored graph itself. Restore consumes the prepared principal and
binds the generated project UUID to its private owner marker, including a digest
of the complete public project handle (project, fixture, run, and graph arm).
Snapshot and destroy require that exact binding, so an arbitrary or altered
local project handle cannot be inspected or removed.
Destroy removes only the marked project, the attempt-owned Auth principal, and
the private host files. A failed restore automatically performs the same
cleanup. `cleanup` is an idempotent finalizer for failures before a public
project handle is returned, and it deletes **every** project the attempt
principal created, not only a restored one — an arm that provisions in-container
makes a project no marker names.

`inspect` is the counterpart to `snapshot` for a project that was never
restored. There is no declared expected state to compare against, so instead of
"does it match" it answers "what did the agent write": per project, the node
counts by type for nodes authored through the agent surface, whether it created
and completed a task for its own work, and whether it touched the seeded welcome
task. Seeded nodes are excluded by write channel (`human`) rather than by count,
so a project where the agent wrote nothing reports zero instead of three. It
takes only a run id — never a project id from the container — so the read is
independent of what the container claims, and the two are cross-checked
afterwards.

Run the opt-in round-trip test from the public package while the local stack is
available:

```sh
cd eval/stele-bench
STELE_BENCH_LIVE=1 \
STELE_BENCH_PROVISIONER=../../scripts/benchmark-provisioner/provision.py \
  uv run pytest -q tests/test_local_provisioner.py
```

## Harbor install-only smoke comparison

`run_install_only_smoke.py` is the private, no-model composition for the first
two Harbor conditions. It creates one fresh local principal, restores the
faithful graph through the public provisioner boundary, runs the stock and
configured Harbor jobs with `--install-only`, proves the configured structural
preflight, snapshots the restored graph, and removes the project/principal in a
`finally` path. It never builds an artifact and does not include a model or
agent-run option in either Harbor command.

Invoke it through the public package environment so its portable imports are
available. The artifact must already be an executable for the target container
(a release artifact or a prebuilt local dirty-worktree artifact), not an
archive:

```sh
cd eval/stele-bench
uv run ../../scripts/benchmark-provisioner/run_install_only_smoke.py \
  /path/to/dotstate a4a5986eafa496dee73a93141588cffaed559f80 \
  fixtures/smoke/fixture.json \
  --attempt-root /private/attempt-state \
  --work-root /private/harbor-smoke \
  --run-id local-smoke-001 \
  --artifact /private/prebuilt/stele-linux-<container-arch> \
  --artifact-sha256 <sha256> \
  --artifact-source-kind local-dirty-worktree \
  --backend-url http://host.docker.internal:54321 \
  --backend-anon-key <local-publishable-key>
```

The raw executable must match the Docker daemon's container architecture, not
the host shell's architecture. For example, Docker Desktop on Apple Silicon
normally needs the Linux ARM64 artifact. The adapter rejects archives; extract
the single executable first and hash those exact bytes.

The only stdout result is a bounded JSON receipt with portable provenance and
condition status; it contains no host paths, tokens, backend key, or project
identifier. Harbor's detailed private logs remain below `--work-root` for
inspection. A successful configured receipt means the zero-token structural
preflight passed; it does not claim that a real Claude session fired lifecycle
hooks or resolved managed-policy precedence.

By default the command runs the matched `no-stele` and `faithful` setup pair.
For a quick configured retry, pass `--condition faithful`; use
`--condition no-stele` for just the stock control. `--condition` is repeatable
and rejects duplicates, so an explicit two-condition invocation remains ordered
and comparable.

## Scored challenge runs

`run_scored_smoke.py` executes one condition per invocation. `--challenge` is
repeatable for `--condition no-stele` and preserves caller order; omitting it
selects only `service-readiness`. The no-Stele route performs no graph prepare,
restore, binding, snapshot, or destroy operation. A faithful invocation accepts
exactly one challenge and uses the configured route to exact-restore the
caller-provided fixture.

For a faithful run, the runner also starts a short-lived semantic-seeds bridge
on an ephemeral host port and supplies its Docker-reachable URL as
`STELE_SEMANTIC_ENDPOINT`. The bridge embeds recall queries with the same pinned
model, validates each new attempt bearer token with local Auth before its first
paid embedding call, and forwards it to the local backend's semantic search
endpoint. Each admitted token has its own bucket of at most 32 paid embedding
attempts; the bridge can defensively admit at most four declared tokens or 128
attempts in total. The current Harbor credential seam uses one token for the
whole job, so the runner permits exactly one challenge in a faithful invocation
instead of pretending a shared token provides independent challenge budgets.
The bridge binds `0.0.0.0` only because
Docker Desktop must reach the host port. Its Docker endpoint contains a fresh,
unguessable per-run secret; that secret, validated attempt tokens, and the
per-token request ceilings are the boundary against other host-network callers.

The bridge returns only bounded node IDs and scores (semantic seeds). The SDK
then hydrates those seeds in a separate backend roundtrip, so benchmark recall
timing includes bridge, embedding, semantic search, and hydration overhead and
must not be presented as production recall latency. The Gateway credential is
already supplied to the scored agent as `ANTHROPIC_AUTH_TOKEN` for Claude model
traffic. The bridge merely retains its host-side reference to the same
`AI_GATEWAY_KEY_EVAL`; it does not make a second copy in the evaluated
container, checkout, fixture, or receipt. The bridge stops when Harbor exits. A
no-Stele run neither starts the bridge nor receives a semantic endpoint,
preserving the control condition.

Every invocation requires `AI_GATEWAY_KEY_EVAL` in the parent environment. The
runner templates that key at process launch rather than serializing it, pins one
model, one attempt per challenge, one concurrent trial, and zero retries. It
requires exactly one Harbor result for every selected challenge and maps results
by the task's `local/<challenge-id>` name, rejecting missing, duplicate, and
extra trials. Raw Harbor, Claude stream, ATIF, verifier, and preflight artifacts
remain under `--work-root`.

```sh
export AI_GATEWAY_KEY_EVAL=...
cd eval/stele-bench
uv run ../../scripts/benchmark-provisioner/run_scored_smoke.py \
  /path/to/dotstate a4a5986eafa496dee73a93141588cffaed559f80 \
  fixtures/dotstate-v1/fixture.json \
  --condition faithful \
  --challenge input-keypress-architecture \
  --attempt-root /private/attempt-state \
  --work-root /private/scored-run \
  --run-id dotstate-faithful-001 \
  --artifact /private/prebuilt/stele-linux-arm64 \
  --artifact-sha256 <sha256> \
  --backend-url http://host.docker.internal:54321 \
  --backend-anon-key <local-publishable-key> \
  --max-turns 3 \
  --max-budget-usd 0.10
```

A no-Stele control needs no fixture, provisioner, artifact, backend, or graph
credentials:

```sh
uv run ../../scripts/benchmark-provisioner/run_scored_smoke.py \
  /path/to/dotstate a4a5986eafa496dee73a93141588cffaed559f80 \
  --condition no-stele \
  --challenge package-cache-is-empty \
  --challenge input-keypress-architecture \
  --attempt-root /private/attempt-state \
  --work-root /private/scored-run \
  --run-id dotstate-control-001
```

The bounded JSON receipt contains ordered per-challenge results with the
canonical `reward`, token/cost/timing evidence, and authenticated challenge
manifest and verifier digests. Its top-level requested budget across trials is
the per-trial Claude CLI cap multiplied by the number of selected challenges.

Claude's `--max-budget-usd` is a per-trial post-response stop condition, not a
hard gateway spend ceiling: the first request can exceed it, and total possible
spend scales with the number of challenges. Receipts therefore keep
Claude-reported cost separate from a model-rate cross-check and never label
either as an authoritative Gateway billing receipt. Use a dedicated Gateway key
and Gateway-side budget for the external ceiling.
