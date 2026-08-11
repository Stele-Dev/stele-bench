# Pre-registration: input-keypress-architecture v2

**Status:** frozen before any paid v2 model call. Do not edit after the paid run
starts; append a dated result section only.

- Challenge: `input-keypress-architecture`
- Corpus: DotState at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful` with prompt-time recall enabled
- Model: `anthropic/claude-sonnet-4.5`
- Design: one paired admission attempt, control then treatment
- Limits: 60 turns and $5.00 requested Claude cap per arm; $10.00 maximum pair
  cap
- Verifier SHA-256:
  `cd12b267ef87928bc92c20e8f2c59b2ac29fa758bd1f897bdadab0b381bdb3de`

## Changes from apparatus-only v1

The question, source, fixture, graph, conditions, and five-point oracle remain
unchanged. V2 makes two disclosed changes:

1. the answer verifier normalizes Markdown newlines before matching related
   clauses and accepts `recur`/`reintroduced` paraphrases; regression tests cover
   the two faithful answer shapes that v1 falsely scored 0.8;
2. the model changes from Haiku 4.5 to Sonnet 4.5. Both v1 controls exhausted a
   $3 cap without writing an answer, so v2 tests the real technical question
   with a stronger coding model instead of tuning the prompt with answer hints.

V1 remains frozen and apparatus-only. Do not pool it with v2.

## Question and mechanism

The agent must explain why keypress failures recur across DotState input fields,
cite source evidence, and leave source untouched. The faithful graph contains
the global input-mode gate, manual per-screen focus reporting,
char-before-keymap ordering, and duplicated routing cluster. V1 proved that
prompt-time recall surfaces this cluster before the agent acts.

This is an investigation-efficiency question. Correctness may tie because the
control can derive the architecture from source. Report deterministic reward,
investigative and total calls, agent and wall time, token classes, Gateway cost,
and first-request context delta from raw JSONL.

## Predictions and stopping rule

1. Both arms finish valid, non-truncated, and score 1.
2. Treatment receives the registered recall cluster before its first action.
3. Treatment uses fewer investigative calls and less agent time than control.
4. Gross token and cost deltas are reported even if fixed Stele tare outweighs
   the saved investigation.

Run one pair only. Publish the raw result whether positive, null, or negative.
Do not tune or rerun after observing it. Any invalid arm makes v2
apparatus-only.

## Registered invocation

```text
oracle.py run-repeat input-keypress-architecture-v2 \
  --attempts 1 \
  --control-condition no-stele \
  --treatment-condition faithful \
  --source "$STELE_BENCH_SOURCE" \
  --revision a4a5986eafa496dee73a93141588cffaed559f80 \
  --fixture fixtures/dotstate-v1/fixture.json \
  --challenge input-keypress-architecture \
  --model anthropic/claude-sonnet-4.5 \
  --max-turns 60 \
  --max-budget-usd 5 \
  --acknowledge-paid-repeat-cap-usd 10
```

## Result (2026-08-11)

V2 is apparatus-only. Both agents completed, but both independently ran
`git log --all --grep=...`; the corpus audit therefore marked both arms invalid
for forbidden repository access. Asking why a bug “keeps recurring” made
history inspection a reasonable agent action even though `.git` is outside the
certified source corpus.

The corrected verifier scored the control 1. The faithful answer described all
five concepts but scored 0.4 because the lexical oracle still omitted bounded
paraphrases present in the answer: `app-level`, `event handler`, and Vim
bindings that `eat` characters. These are additional verifier false negatives.

The invalid trajectories show directional efficiency only: faithful used 13
versus 41 investigative calls, 35 versus 43 total calls, 207 versus 227 agent
seconds, 874,067 versus 1,421,646 input tokens, and reported $0.76 versus $1.15.
None is a benchmark claim. A final v3 may fix only the explicit history boundary
and these verified synonyms; it must not pool v1 or v2.
