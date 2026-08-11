# Dotstate packages K=5 candidate audit

Date: 2026-07-27

Status: rejected before implementation and before model spend

## Candidate

The proposed chain used five consecutive Dotstate package subcommands:

| Session | Base | Canonical target | Change |
| ---: | --- | --- | --- |
| 1 | `c881a6a` | `958d2ba` | `dot packages list` |
| 2 | `958d2ba` | `5482cb1` | `dot packages add` |
| 3 | `5482cb1` | `8541307` | `dot packages remove` |
| 4 | `8541307` | `f1e6810` | `dot packages check` |
| 5 | `f1e6810` | `256fd2c` | `dot packages install` |

The direct commits and canonical patch digests were audited, and their native
suite passed after normalising the historical lockfile. No model calls were
made.

## Rejection reasons

### 1. Source carries the prior-session answer

Each next canonical base contains the preceding canonical implementation. The
later subcommands operate in the same module and expose the exact patterns,
types, helpers, output conventions, and package-manager decisions that a
cross-session memory system could otherwise preserve. This is equal between
arms, but it turns the sequence into five sibling implementation tasks rather
than a test of knowledge accumulated across sessions.

The intended estimand is the value of durable session-to-session knowledge, not
the value of reinstalling Stele while the source tree already carries nearly all
of that knowledge. A low or absent crossover on this chain would therefore be
ambiguous.

### 2. Removing Git creates an artificial control handicap

The draft stripped `.git` from every session. Real coding work normally retains
history, and control agents can use it to recover prior decisions. Deleting it
would suppress a legitimate non-Stele continuity mechanism while the treatment
keeps its graph.

A future fixture must preserve repository history truncated at the registered
base revision, remove or disable remotes, and prove the target revision is not
reachable. Both arms must receive the same history.

### 3. The canonical targets do not satisfy the full prompt contract

The prompts required focused tests, but the historical feature commits did not
add those tests. Hidden verifier discrimination can still prove behavior, but
the canonical target is not a clean positive fixture for the registered task as
written. Regenerating `Cargo.lock` under a current toolchain also changes the
historical boundary unless the normalised lockfile and its digest are frozen as
fixture input for both base and target.

### 4. Natural memory intake was not established

The prior Class C treatment attempts produced no agent-authored graph nodes even
though delivery occurred. The draft admission explicitly allowed zero useful
memory and zero graph writes, then proposed a cohort ceiling of $400. That could
measure five sessions of installed-surface overhead without exercising the
compounding mechanism.

A future discarded admission must prove that the unprompted, shipped workflow
creates graph writes and that later sessions can receive prior-session node
versions. The cohort analysis must remain intent-to-treat and report the full
per-session write distribution, chains with zero writes, delivery, observable
use, and correctness. It must not exclude quiet graphs after seeing outcomes.

### 5. Cost and inference gates were incomplete

The old v2 receipts have no authoritative billing values. A new runner must
prove an exclusive per-session authoritative spend delta before any cohort.
Model-reported dollars and rate-table reconstructions are cross-checks only.

The proposed $2 per-session cap was not justified against observed long-tail
task cost and left little room below the 90% truncation-risk rule. The cap and
account fence must be fixed from boundary evidence before the cohort.

Five cumulative prefixes create five crossover opportunities. A future
preregistration must use simultaneous or multiplicity-corrected uncertainty,
predeclare a numeric correctness non-inferiority margin, and calculate whether
the proposed sample size can detect any plausible break-even within its spending
ceiling. Reporting the first uncorrected interval below zero is not sufficient.

## Replacement criteria

Do not implement or fund this candidate. A replacement K-session chain must:

1. make later work depend on decisions or constraints learned earlier that are
   not already obvious from the next source checkpoint;
2. preserve identical Git history through each base while making future commits
   unreachable;
3. freeze discriminating base/target fixtures, tests, and lockfile boundaries;
4. exercise unprompted graph writing and prior-version delivery in a discarded
   admission before cohort approval;
5. retain every cohort chain in an intent-to-treat analysis, including zero-write
   chains;
6. produce authoritative, exclusive spend receipts; and
7. preregister multiplicity-corrected crossover inference, correctness margin,
   feasibility, and defensible spending fences.

The contiguous packages history is useful as runner test material, but it is
not a valid compounding crossover benchmark.
