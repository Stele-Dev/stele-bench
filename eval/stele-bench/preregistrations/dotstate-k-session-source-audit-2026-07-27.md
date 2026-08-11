# Dotstate K-session source audit

Date: 2026-07-27

Status: no qualifying K=5 chain selected; no model spend

## Scope

This audit looked for a five-session Dotstate sequence where later work depends
on a decision, incident, or constraint learned earlier that is not already
recoverable from the next source checkpoint. It inspected the 367-object local
history, the plausible related issue threads 31, 34, 37, 43, 45, and 47, and the
profile-inheritance work in pull request 46.

This was a candidate audit, not an exhaustive claim that no conceivable
Dotstate benchmark can exist. It covers the related histories with enough real
stages to appear capable of supporting K=5.

## Candidate families

### Packages CLI

The contiguous `list` → `add` → `remove` → `check` → `install` sequence was
rejected separately in
`dotstate-packages-k5-candidate-audit-2026-07-27.md`. Every next base contains
the previous canonical implementation in the same module, and the five tasks
are sibling variants rather than knowledge-transfer stages.

### Common files and symlink repair

The strongest alternative was the real common-files evolution:

| Commit | Change |
| --- | --- |
| `182521c` | initial common-files checkpoint |
| `d721204` | common symlinks after remote sync |
| `0a77ad2` | move-to-common validation and dialog |
| `f28ced0` | common-path matching repair |
| `de1feca` | common activation and doctor repair |
| `92f7822` | common unsync and UI/sync fixes |

Issue 34 supplies a real cross-device history, including the distinction
between correct CLI/symlink state and stale UI state. It still fails the K=5
question contract:

1. the initial checkpoint is a 1,200-line mixed implementation, not a bounded
   task with a clean canonical target;
2. later issue reports state the immediate symptom and often the diagnosis, so
   the answer needed for that session arrives in the current prompt rather than
   through prior-session memory;
3. prior common-file behavior, path matching, activation, and tracking logic is
   present in the next canonical source and truncated Git history; and
4. the stages mix feature work, UI state, migration repair, doctor behavior,
   remote sync, and unsync. A cost difference could not be attributed to one
   accumulating knowledge path.

Issues 31 and 37 provide valuable safety constraints around nested paths,
broken links, circular links, external roots, and large targets. They are
standalone incidents. Joining them to the common-files sequence would create a
thematic anthology, not a natural five-stage dependency chain.

### Profile inheritance

Issue 45 and pull request 46 contain a genuine product decision, inheritance
rather than arbitrary profile groups, followed by concrete fixes for atomic
creation, cached resolution, rename propagation, stable package deduplication,
and rollback on validation failure.

The usable commits are review cleanups on one already-implemented feature. The
source and tests after the first implementation expose the inheritance model,
cycle rules, override semantics, and mutation patterns. The later fixes are
mostly independent review findings, not consequences that require knowledge
from the preceding session. Selecting five of them would measure repeated work
inside one large pull request, with the same source-self-similarity problem as
the packages chain.

Issues 43 and 47 are later isolated feature/documentation requests. They do not
provide the missing dependent stages.

## Decision

Do not write a Dotstate K=5 preregistration from these histories. None of the
audited families combines all four required properties:

1. five bounded, deterministically verifiable checkpoints;
2. real dependency between sessions;
3. a transferred fact absent from the later source and honest truncated Git
   baseline; and
4. enough natural graph intake to make a crossover cohort feasible.

The next source search should target a repository with a multi-release incident
or design reversal whose rationale lives in issue/review history and changes
later implementation choices without being restated in the tree. Another valid
shape is a real maintainer conversation that introduces a future constraint in
one session and exercises it later, provided the prompt does not instruct the
agent to save memory and the sequence has deterministic code oracles.

Even after source selection, a paid admission remains mandatory. It must prove
natural graph writes, later delivery, and an exclusive authoritative spend
receipt before any cohort is authorised.
