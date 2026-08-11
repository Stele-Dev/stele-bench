# Click `CliRunner` K=5 candidate audit

Date: 2026-07-27

Status: zero-model source qualification passed; discarded paid admission failed;
cohort not authorized

## Decision

Use the five-stage Click `CliRunner` incident lineage as the source sequence for
the next compounding preregistration. Unlike the rejected Dotstate package
sequence, this is not five sibling feature implementations. It is one evolving
stream-capture contract where each later repair must preserve a constraint
created or exposed by an earlier incident.

The frozen machine-readable boundary is
`click-clirunner-k5-v1.json`. The five checked-in challenge verifiers fail at
their registered bases and pass at their canonical targets. No model call was
made during selection or qualification.

## Source lineage

| Session | Base | Canonical target | Primary incident and repair |
| ---: | --- | --- | --- |
| 1 | `948d7a707a0abda8ce0a1b39ba87390f11ffcd5e` | `0e0c00324a5af35f8995c7ff88df67b0f5594a58` | [#2522](https://github.com/pallets/click/issues/2522), [#2523](https://github.com/pallets/click/pull/2523): separate stdout and stderr while preserving terminal order |
| 2 | `7b72bfb8cd3a0cec38197f6786ed583b5b3737e7` | `737bfbd3122d96d41924323a3f45a8021c30d4c8` | [#2993](https://github.com/pallets/click/issues/2993), [#2991](https://github.com/pallets/click/pull/2991): free-threaded finalization order race |
| 3 | `b6afbf05ea6dbc8a4e7cdd448dd345c783973137` | `d8e987eae723fbb0ccc355d125d0e179a1bf5fd8` | [#3110](https://github.com/pallets/click/issues/3110), [#3139](https://github.com/pallets/click/pull/3139): logging writes to buffers closed by competing owners |
| 4 | `878de46100e9c29aea9dab5b385b8116863cb5e3` | `8762c7752cf1632d082c76922b15fef06ac3a2f1` | [#2865](https://github.com/pallets/click/issues/2865), [#3244](https://github.com/pallets/click/pull/3244): `fileno()` compatibility for descriptor-level consumers |
| 5 | `0039359443e73ab1034c63a1f6d58aba22c0ebc8` | `ad39d749be85af02bf191636062eef51323ad5ab` | [#3384](https://github.com/pallets/click/issues/3384), [#3391](https://github.com/pallets/click/pull/3391): default descriptor exposure damages host capture; add explicit capture modes |

Every target is an ancestor of the next base. The intervening gaps are 232,
253, 45, and 72 commits. The sequence therefore follows real chronological
history without pretending the five repairs were consecutive.

PR #3139 records the central causal chain explicitly: #2523 introduced
`StreamMixer` and `NamedTextIOWrapper`; #2991 added finalization behavior for
#2993; that repair caused #3110; #3139 replaced competing buffer owners with a
single-owner design. The two later stages then expose and constrain the same
wrapper boundary at the file-descriptor layer.

## Why knowledge can transfer

The next source checkpoint contains the prior implementation, because that is
honest repository history. It does not co-locate the full incident rationale:

1. session 2 benefits from the dependency topology established while separating
   the streams in session 1;
2. session 3 must know why finalization behavior exists before removing the
   behavior that caused the logging regression;
3. session 4 must preserve the single-owner buffer boundary while exposing an
   original descriptor;
4. session 5 must preserve intentional descriptor-level compatibility while
   withdrawing unsafe default descriptor exposure.

The control may recover this through code, `git log`, blame, or the reachable
historical commits. That is the correct no-memory baseline. The treatment's
claim is only that a naturally accumulated graph makes the same historical
constraints cheaper to recover, not that the facts are impossible to discover
without Stele.

## Honest Git boundary

Each session workspace must be a real Git repository whose only object graph is
the set reachable from that session's base commit. It must have no remotes.
The registered canonical target commit must fail `git cat-file -e`, while the
base commit, its tree, log, blame, and earlier history remain available.

This avoids both invalid alternatives:

- a full clone checked out at the base leaks the future target object; and
- an exported tree without `.git` suppresses the strongest honest substitute
  for graph memory.

The manifest freezes each base tree, reachable commit count, reachable object
count, and the SHA-256 of the sorted reachable object IDs. A prototype built
with `git pack-objects --revs` and `git index-pack --fix-thin` preserved all five
registered commit IDs and worktrees while making every future target absent.

The production materializer and Harbor packer now preserve that boundary. Ten
generated project packs, one base and one target for each session, were then
rehearsed in the exact pinned image. Every pack had the registered `HEAD`, no
remote, a clean worktree, and no registered future target object. All five base
packs produced reward 0 and all five target packs produced reward 1.

## Verifier qualification

The verifiers use only POSIX shell and Python's standard library with Click
loaded directly from `/workspace/src`. They do not require installation,
network access, pytest, or a generated lockfile.

Two boundary checks were run:

1. twenty host repetitions per base and target produced 0/20 passes at every
   base and 20/20 at every target; and
2. the exact checked-in shell assets ran once per base and target in
   `harbor-local/claude-rust:2.1.220-rust-1.88.0`, with networking disabled,
   producing reward 0 at every base and reward 1 at every target.

The target evidence was respectively `stream_contract`,
`dependency_close_order`, `single_buffer_owner`,
`fileno_consumer_compatible`, and both `safe_default` plus `fd_capture`.

## Runner and receipt qualification

The K=5 runner is manifest-driven and creates five fresh conversations per
arm. Both arms receive the same registered history checkpoint at each session.
Only the treatment graph persists. Local `git log`, `show`, `blame`,
`cat-file`, and `rev-list` are permitted because honest source history is part
of the frozen baseline; remotes, external source fetches, and web trajectories
remain invalid.

The pair and cohort oracles now enforce the preregistered boundaries:

- odd pairs run control then treatment and even pairs reverse that order;
- all 20 pair identities and orders are frozen before the cohort begins;
- each session is capped at $3, each pair at $30, and the cohort at $600;
- each completed pair consumes its frozen index even if behavior is invalid;
- a settled cost is accepted only while the runner holds the configured
  cross-process Gateway-key lock and the before/after observations have the
  same key fingerprint;
- a pair missing any exclusive session cost is retained, consumes the index,
  and blocks the ledger with no inference;
- graph deltas record natural writes, and delivery requires an exact prior
  `short_id@version` to appear in both the pre-session graph and hook or tool
  evidence;
- cumulative paired cost differences, exact one-sided sign tests, Holm
  adjustment across all five prefixes, and the correctness guard are withheld
  until all 20 pairs exist.

Zero-model tests cover the locked third-session crossover, no-claim behavior
for incomplete cohorts, correctness-guard failure, immutable order, settled
spend accounting, and the missing-exclusive-cost failure path. The actual
Gateway reserve configuration is not modified by this work. It must explicitly
attest exclusive key use before a paid K=5 command can start.

## Paid admission result

The one approved discarded admission ran as
`click-clirunner-k5-admission-001`. It failed and authorizes no cohort. Both
arms recorded 0/5 rewards, three sessions ended in output-token infrastructure
errors, and control session 4 attempted an external source trajectory.

The billing boundary did work: all ten intervals were exclusive and settled.
Authoritative cost was `$4.24664025` control and `$6.07389187` treatment,
`$10.32053212` total. Treatment cost `$1.82725162` more, or 43.03%, in this
discarded pair.

The registered product loop failed independently of the correctness defects.
Treatment wrote no agent-authored graph state in sessions 1 through 4. It
created and completed one task in session 5, too late for a reuse opportunity,
and no later session received an exact prior node version. The candidate must
not be tuned or rerun.

Six sessions also exposed an apparatus defect: ordinary Python bytecode caches
were reported as `undeclared workspace mutation`. Four sessions were
force-zeroed solely by `__pycache__`; two others also changed an undeclared test
file. The generic mutation wrapper now ignores `__pycache__`, `.pyc`, and `.pyo`
symmetrically, but that after-spend repair does not alter the registered receipt
or rescue v1.

The full result is recorded in:

- `click-clirunner-k5-admission-001-results-2026-07-27.md`
- `../results/click-clirunner-k5/2026-07-27-click-clirunner-k5-admission-001.json`

Any future K-session attempt requires a new preregistration, complete zero-model
rehearsal of the full wrapper, and new explicit approval.
