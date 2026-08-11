# Fieldnote continuity v4 preregistration

**Status:** frozen before any v4 model call. Do not edit after a paid result
exists; create a new version instead.

## Revision from v3

The v3 admission ran all eight Sonnet sessions and settled $2.92931762. Its
control chain was invalidated after session 4 because the external-source
detector matched a local `curl --version || wget --version` capability probe.
No URL was supplied and no source was fetched by that command. V3 remains
terminal, discarded, and uninterpreted as a paired result.

V4 excludes version-only and help-only invocations of `curl`, `wget`,
`httpie`, and `lynx` from external-source access. Actual network-client
requests, web tools, remote Git access, and unapproved repository access remain
fail-closed. The exact observed capability probe is covered by a model-free
regression test, and the full zero-model qualification still runs every task
through Harbor's real `nop` agent plus every hidden grader at base and target.

Prompts, source checkpoints, graders, Sonnet 4.6, graph and source carryover
boundaries, caps, admission behavior, and inference remain unchanged. V3 is
not rerun or reinterpreted; v4 is a new registration.

## Question

Does the graph accumulated naturally in one project session help a capable
agent continue a small project across later fresh conversations, surface a
conflicting prior decision, and produce a release-readiness assessment that
recovers constraints absent from the current source checkpoint?

This is an end-to-end product-loop experiment. A treatment chain that writes no
graph state is a valid negative result, not an infrastructure failure and not a
reason to tune or rerun the candidate.

## Arms and boundary

- **Control:** stock Claude Code with no Stele.
- **Treatment:** the shipped Stele artifact with an initially fresh project.
- Both arms use `anthropic/claude-sonnet-4.6`, the same prompts, hidden
  verifiers, source checkpoints, limits, and task image.
- Each milestone starts a fresh conversation.
- Both arms advance to the same canonical source checkpoint. Agent-produced
  code, comments, notes, transcripts, homes, and build outputs do not cross a
  session boundary.
- Only the treatment project graph crosses the boundary.
- Each checkpoint includes only Git objects reachable from its registered base;
  the canonical target commit must be absent and the repository has no remotes.
- The task environment permits the agent's required service traffic. Any
  external source access detected in the trajectory invalidates the chain.

This isolates graph continuity rather than workspace continuity. Code comments
and documentation present in the canonical checkpoint remain an honest
source-only baseline. A separate live-workspace carryover experiment would
answer a broader, noisier question.

## Scenario

Fieldnote is a single-file Python diagnostic summarizer small enough to avoid
bootstrap and framework noise.

### Session 1

The prompt contains ordinary `Implementation constraints` and `Milestones`
sections. It specifies four positive and negative decisions, then asks the agent
to implement milestone 1 only. It never asks the agent to save, remember,
document, ingest, or use Stele.

### Session 2

The entire prompt is: “Let's continue with milestone 2. Implement only that
milestone and verify it locally.” The milestone definition exists in session 1
but is absent from the registered session-2 source tree.

### Session 3

The first turn asks for a self-contained HTML report even though session 1
planned Markdown. It does not announce that this is a change. The fixed second
turn asks for every sanitized event without truncation even when the result
exceeds 50 KiB. Correct behavior may comply, but observable conflict surfacing
and graph supersession are recorded separately from code correctness.

### Session 4

The prompt asks the agent to finish the project and explain release readiness.
The hidden verifier checks the final milestone. The transcript is separately
scanned for the original negative decisions and the approved size exception.

## Deterministic grading

Each session has a hidden, standard-library verifier. Every verifier must fail
at its registered base and pass at its canonical target in the exact pinned
image. The mutation wrapper runs in full and ignores only registered outputs and
interpreter bytecode caches.

Report three memory layers separately:

1. **Written:** agent-authored graph nodes created or updated in an earlier
   evaluated session.
2. **Delivered:** an exact prior `short_id@version` appears in hook or Stele-tool
   evidence in a later session.
3. **Observably used:** the later response or behavior contains one of the
   preregistered lower-bound markers. Absence is “no observable trace,” never
   “ignored.”

Primary per-chain outcomes are session correctness, treatment graph-write rate,
later exact-version delivery, session-3 conflict surfacing, session-4 release
constraint recovery, authoritative Gateway cost, input tokens, and cumulative
cost at K=1 through K=4.

## Admission

One discarded paid pair may run only after zero-model qualification under the
standing approval of at most `$30.00` per consecutive newly preregistered
admission attempt. It is an apparatus check only:

- all eight sessions start and retain distinct conversation identities;
- registered source and graph boundaries hold;
- every hidden verifier executes and returns a valid reward, including zero;
- the model remains pinned;
- no external source is accessed;
- all billed steps have exclusive, settled Gateway receipts.

Behavior is not an admission gate. In particular, no graph write, no delivery,
no pushback, or zero correctness remains a valid result. The admission is never
tuned or rerun.

## Cohort and inference

If the admission is operationally valid, a separate explicit approval is
required for the N=20 cohort. Pair identities and alternating arm order are
frozen before pair 1. Invalid attempts consume their index and are not replaced.

- Per billed step cap: `$3.00`.
- Per pair cap: `$30.00` because session 3 has two billed turns.
- Cohort cap: `$600.00`.
- Continuous cost and token claims wait for all 20 frozen pairs.
- At each cumulative prefix K=1..4, use the paired treatment-minus-control
  vector, an exact one-sided sign test for treatment being cheaper, and Holm
  correction across the four prefixes.
- Report paired medians and bootstrap 95% intervals; never report a point without
  its interval.
- A crossover claim requires at least 50% session correctness in each arm and
  treatment correctness no more than 10 percentage points below control.
- Intake, delivery, conflict surfacing, and release recovery are descriptive
  binary rates with exact intervals. They are not admission filters.

The registered result states the earliest observed cumulative crossover, or
states that none occurred by K=4. It does not extrapolate.
