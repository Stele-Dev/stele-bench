# Fieldnote continuity v7 preregistration

**Status:** frozen before any v7 model call. Do not edit after a paid result
exists; create a new version instead.

## Revision from v6

V6 ran all eight sessions and settled $3.11798799. Both arms produced
`[0, 0, 1, 0]`; treatment produced no agent-authored graph nodes in any
session. V6 is nevertheless terminal and invalid because treatment session 4
was falsely marked as external source access.

The executable command was only a permitted capability probe:

```text
curl --version 2>/dev/null | head -1 || wget --version 2>/dev/null | head -1 || echo "no downloader"
```

The Bash tool call also carried the inert description `Check if curl or wget is
available`. The detector serialized the entire arguments object, so it matched
the client names in the description even though its command regex correctly
excluded the `--version` invocations.

V7 changes only that scorer boundary: for Bash-like tools, external-source
detection inspects executable `command`, `cmd`, or `script` fields rather than
descriptive metadata. The exact v6 tool-call shape is covered by a model-free
regression; actual URLs, web tools, remote Git access, arbitrary network-client
commands, and mixed package-bootstrap URLs remain fail-closed.

The treatment product artifact is unchanged from v6: clean source commit
`fa373b1b7840776dccbd1853bda506cbfcc9af30`, SHA-256
`d3c9fcd14d9d60cf518e265395312c8864d3b7a3556c30ea085b6d6785739961`,
target `linux-arm64`. Prompts, source checkpoints, graders, Sonnet 4.6, graph
and source boundaries, caps, admission behavior, and inference are unchanged.
V6 is not rerun or reinterpreted; v7 is a new registration.

## Question and scenario

Does naturally accumulated graph state help a capable agent continue Fieldnote
across four fresh conversations, surface a conflicting prior decision, and
recover release constraints absent from the current source checkpoint?

Session 1 contains ordinary `Implementation constraints` and `Milestones`
sections and asks for milestone 1 only, without asking the agent to save or
remember anything. Session 2 says only to continue milestone 2. Session 3 asks
for HTML instead of the originally planned Markdown, followed by a fixed request
to include every sanitized event even beyond 50 KiB. Session 4 asks the agent to
finish and assess release readiness.

Control is stock Claude Code. Treatment is the exactly bound Stele artifact
with a fresh project. Each milestone is a fresh conversation; both arms receive
the same canonical source checkpoint, while only the treatment graph crosses
sessions. Transcripts, homes, workspace mutations, build outputs, and future Git
objects never cross the boundary.

## Deterministic grading

Every hidden verifier must fail at its registered base and pass at its canonical
target in the pinned image. Report written graph state, exact later delivery,
observable use markers, correctness, authoritative Gateway cost, tokens, and
cumulative cost at K=1 through K=4 separately.

One discarded pair may run under the standing `$30.00` approval after zero-model
qualification. Admission is apparatus-only: eight distinct sessions, exact
source and graph boundaries, valid verifier rewards including zero, pinned
model, no actual external-source access, and exclusive settled receipts. No
write, no delivery, no pushback, or zero correctness is a valid behavioral
outcome and never permits tuning or rerunning v7.

An N=20 cohort still requires separate explicit approval. Its unchanged caps
are `$3.00` per billed step, `$30.00` per pair, and `$600.00` total. Crossover
inference, Holm correction across four prefixes, bootstrap intervals, and the
correctness guard remain exactly as registered in v6.
