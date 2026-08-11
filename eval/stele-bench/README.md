# stele-bench

`stele-bench` is a standalone Python package for describing benchmark fixtures,
declaring challenges, and recording benchmark results. Fixtures are cohort-scoped,
frozen before challenges are authored, and carry evidence-corpus
ordering/truncation provenance. Challenges and results are separate records.
No-Stele and full-context are experiment
conditions, not graph fixtures. Result records retain cohort, fixture, and opaque
run identities so cohorts cannot be mixed accidentally. Graph fixtures pin their
format, backend compatibility, embedding model, and dimensions for safe restores.
This foundation contains contracts, fixture validation, pinned source
materialization, an external provisioner boundary, and no-cost smoke preparation.
It does not invoke models or require a remote service from the public package.

The public, interpreted result summary is available at
[stele-dev.github.io/stele-bench](https://stele-dev.github.io/stele-bench/).

## External provisioner protocol

`ExternalCommandProvisioner` connects the benchmark to any backend without importing
its schema. Configure an argv prefix and a fixture root; it executes the prefix with
one of `restore`, `snapshot`, or `destroy` using `asyncio.create_subprocess_exec` (no
shell). The command receives exactly one compact JSON object on stdin and must write
exactly one JSON object to stdout. Stderr is reserved for bounded operator diagnostics
and is never parsed as data. `restore` receives the manifest, graph arm, opaque run
identity, and artifact paths resolved below the fixture root; it returns a
`RestoreResult` (`project`, queue-before count, and restore timing). `snapshot`
receives a project and returns a `SnapshotResult` (independently canonicalized
state, named FTS/semantic/graph probes, queue-after count, and snapshot timing).
These two receipts form the public, backend-neutral `RestoreObservation`; the
runner never needs backend-private schema access. Legacy minimal responses remain
available for callers that only need a project or canonical state. `destroy`
receives a project and returns `{ "ok": true }`.
The adapter rejects path escapes, malformed/extra JSON, unknown response fields,
timeouts, non-zero exits, and output beyond its configured byte limit.

## Requirements

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/)
- Git
- Docker
- Harbor 0.20.x for containerized agent runs

## Prepare the pinned source and task image

The host-side oracle packages a Git-less source tree into each Docker task. It
does not clone a repository inside the agent container. Prepare the exact public
DotState commit in the managed source cache, then build the pinned task image:

```sh
export STELE_BENCH_SOURCE="$(uv run stele-bench source-prepare \
  https://github.com/serkanyersen/dotstate \
  a4a5986eafa496dee73a93141588cffaed559f80)"

docker build \
  --tag harbor-local/claude-rust:2.1.220-rust-1.88.0 \
  images/claude-rust
```

The Dockerfile pins its Rust base image by digest and Claude Code by exact
version. The image used for v0.1 had content digest
`sha256:20d8ec0332f87b76e704c82e4bad54fd20c69148f60fbf8456bf406c52d955a9`
on Linux ARM64.

## Development

```sh
uv sync --locked
uv run ruff check .
uv run pytest -q
```

## CLI

Validate one fixture:

```sh
uv run stele-bench validate path/to/fixture.json
```

List valid fixture files in a directory:

```sh
uv run stele-bench list path/to/fixtures
```

List the checked-in challenge catalog (id, verifier kind, and title), or inspect a
different catalog directory:

```sh
uv run stele-bench challenge-list
uv run stele-bench challenge-list path/to/challenges
```

Validate the checked-in catalog, a different catalog, or one challenge manifest:

```sh
uv run stele-bench challenge-validate
uv run stele-bench challenge-validate path/to/challenges
uv run stele-bench challenge-validate path/to/challenges/example/challenge.json
```

`doctor` checks only local package and contract readiness. It does not contact a
backend or execute a benchmark.

## Challenge catalog

Questions live in checked-in manifests at
`challenges/<challenge-id>/challenge.json` (from the repository root:
`eval/stele-bench/challenges/<challenge-id>/challenge.json`). Each manifest owns
the visible prompt, title and description, allowed output paths, resource limits,
and a digest-pinned verifier asset. The verifier is runner-only: it is kept outside
the evaluated project and is not included in the prompt or project tree presented
to the agent.

The catalog deliberately spans different kinds of memory value instead of
repeating one easy edit pattern:

- investigation questions such as `input-keypress-architecture` and
  `profile-selection-intake` grade whether the agent reaches a grounded answer
  and how much source exploration it needed;
- focused coding questions such as `settings-label-roundtrip` and
  `profile-selection-live-path` exercise hidden cross-file or string contracts;
- `session-boundary-recall` and the Fieldnote milestones exercise continuity
  across follow-up turns;
- `package-cache-is-empty` and `service-readiness` are low-memory controls that
  expose setup and tool-schema tare instead of being presented as memory wins;
- the Click `CliRunner` sequence exercises accumulated lessons across several
  related regressions in an external codebase.

Run `stele-bench challenge-list` for the complete catalog.

The August 2026 preliminary release reports the accepted investigation win,
fixed-overhead check, and settings-label null/mixed cohort together with every
excluded admission in
[`results/2026-08-preliminary/README.md`](results/2026-08-preliminary/README.md).

An answer-file challenge must name its answer path in both `verifier.answer_path`
and `allowed_output_paths`. The current knowledge question requires the agent to
write UTF-8 Markdown to `/workspace/.submission/answer.md`; its hidden verifier
grades that file. A missing or empty answer fails verification.
`allowed_output_paths` is recorded for runner policy and audit evidence; the
generated Harbor task snapshots the initial workspace and runs a hidden generic
mutation audit before the challenge verifier. Added, removed, changed,
permission-modified, symlink-retargeted, or type-swapped paths outside the
declared output subtrees force reward zero. The mutation receipt is deterministic
and stays in the runner-owned verifier area, outside the agent-visible project.

Challenge selection is explicit and repeatable through the Python
`select_challenges(...)` API. It rejects empty, duplicate, and unknown selections,
and preserves the caller's order exactly. Loading the full catalog instead produces
stable challenge-id order. The local install-only and scored runner adapters expose
the same ordered selection through repeatable `--challenge` flags.

Challenges and fixtures are intentionally independent. A challenge defines what an
agent is asked to do and how that work is checked. A fixture defines the frozen
evidence corpus and graph state supplied to an experiment condition. This lets the
same question run against a no-memory condition and a faithful restored graph
without embedding fixture or condition details in the agent-visible prompt.

### Multi-step challenges

Setting `follow_up_prompt` on a challenge manifest turns it into a two-step
Harbor task: `steps/establish/instruction.md` carries `prompt`,
`steps/follow-up/instruction.md` carries `follow_up_prompt`, both steps share
the one task-level `tests/test.sh` verifier, `multi_step_reward_strategy` is
pinned to `"final"` (only the follow-up step's grade counts), and the
generated job configs set `agents[0].resume_trajectory = true` so the agent
resumes its own native session across the boundary instead of starting a
fresh conversation. This is the shape a session-boundary-recall question
needs: establish real work in one turn, then issue a deliberately vague
follow-up and see whether context injected between steps (or its absence)
changes the outcome. A single-step challenge (no `follow_up_prompt`) is
unaffected and keeps the flat `instruction.md` layout.

## Certification — leakage and benchmark awareness

A comparison only means something if the arms differ in the thing under test.
Two failures quietly destroy that, and neither is visible by reading the task
definition:

- **Leakage.** The answer, or the obligation the challenge grades, already sits
  in the evaluated project tree. A "no memory" control is then not memory-free,
  and the measured effect of a graph shrinks for a reason unrelated to the graph.
- **Benchmark awareness.** The packaged inputs announce the experiment — harness
  names, arm names, a project literally called a fixture. An agent that knows it
  is being measured does not behave like one doing ordinary work.

The easiest of these to miss is a **dynamically auto-loaded** context file. A real
repository ships `CLAUDE.md`, `AGENTS.md`, `.cursorrules` or a `.claude/skills`
tree; a coding harness reads it into the system prompt before the first turn, and
nothing in the task definition mentions it. That file is hand-written project
memory, so packaging one hands a competing memory system to every arm at once.

Certification is therefore a **static, zero-model, fail-closed** gate over every
packaged surface: the corpus, the evidence corpus, every declared graph arm, and
every agent-visible challenge manifest. It classifies findings as
`auto-loaded-context`, `suspicious-metadata`, `benchmark-awareness`,
`patch-or-diff`, or `solution-leak`.

Redaction and certification are deliberately separate. Redaction is the one
sanctioned repair — a pinned corpus belongs to someone else, so its auto-loaded
context is removed while packaging, identically for every arm, and recorded in a
runner-owned `tests/context-redaction.json` receipt naming each removed path and
its digest. Everything else only inspects, so an unredacted or hand-edited tree
cannot reach an agent.

Two marker sets apply, because the surfaces are not equally ours. Harness
identity terms (`benchmark`, `oracle`, `harbor`, the arm names) apply everywhere.
A stricter set (`fixture`, `control arm`, `negative control`) applies only to a
graph's *identity* fields — project, workspace and branch names and descriptions,
which the benchmark authors — and never to node bodies, where a graph backfilled
from a real project says "spin up a temp directory with test fixtures" in
ordinary engineering prose.

A challenge may declare `solution_markers`: distinctive phrases that amount to
knowing its answer. Certification requires each to be absent from the packaged
corpus and from agent-visible challenge text. A graph fixture may legitimately
contain them — that is the memory under test.

```sh
uv run stele-bench certify /path/to/source <full-commit-id> path/to/fixture.json
uv run stele-bench certify /path/to/source <full-commit-id> path/to/fixture.json \
  --challenge session-boundary-recall --receipt-out certification.json
```

It exits non-zero on any finding and prints a deterministic receipt recording the
arms probed, counts, redactions, and findings. A missing corpus, fixture
artifact, or graph is an **error, never a skipped check** — a gate that quietly
covers nothing still reports success, which is worse than not having one. For the
same reason the integration test that certifies the real pinned corpus fails
rather than skips when the sibling checkout is absent; set
`STELE_BENCH_ALLOW_MISSING_CORPUS=1` only to opt out deliberately, and accept
that the corpus went unchecked.

## No-cost smoke preparation

The public smoke slice supports the `no-stele` and `faithful` conditions. It
materializes a pinned Git commit into a neutral evaluated path named `project`,
then audits that tree for context which would reveal the experiment. Its receipt
records portable source, fixture, graph, and integration-artifact provenance but
never host paths. `dry-run` only checks materialization, context blindness, and
fixture artifact digests. `install-only` is available through the Python API: it
calls setup for the no-Stele control and faithful adapter, restores the faithful
graph, records the restore observation, and destroys the temporary project. It
intentionally has no agent-run step, so it cannot make a model request.

The smoke receipt is deliberately limited to setup and restore evidence. It sets
`access_binding_verified` to `false`: this public slice does not claim that the
configured agent credentials can access a restored project.

Generated Harbor tasks use a digest-pinned Ubuntu 24.04 multi-architecture image
rather than a mutable tag. Private orchestration records the resolved Harbor,
Docker, backend, integration-artifact, source, fixture, challenge, and verifier
identities in its durable receipts.

For Harbor agent configuration, use the scalar-only import path
`stele_bench.harbor_entrypoint:ConfiguredSteleClaudeCode`. Its `kwargs` take
`artifact_path`, `artifact_sha256`, `artifact_source_kind`, optional
`artifact_release_version`, `credentials_path`, and `preferences_path`; the
entrypoint reads those two JSON files only on the runner and builds the adapter's
typed artifact and attempt-state values before setup.
Both files must be regular, non-symlink files, at most 1 MiB, and readable only
by their owner (for example, mode `0600`).

The same Harbor entrypoint also requires `project_binding_path`, another
owner-only JSON file with exactly `projectId`, `slug`, `name`, `defaultBranch`,
and `activeWorkspaceIds`. Setup actively binds that slug in the evaluated task,
then checks the rewritten cache and writes `preflight.json` under the Harbor
agent logs. That structural report verifies installation and direct hook-command
compatibility only; it does not prove that Claude fired lifecycle hooks or that a
managed permission policy takes precedence in a real session.

The CLI exposes the dry-run form only:

```sh
uv run stele-bench smoke-dry-run /path/to/source <full-commit-id> \
  /path/to/fixture.json /work/project --condition no-stele
```

The evaluated path must end in `project`; it is separate from the fixture and
runner paths and is never included in the receipt.
