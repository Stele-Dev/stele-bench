# Pre-registration: input-keypress-architecture v1

**Status:** frozen before any paid v1 model call. Do not edit after a paid run
starts; append a dated result section only.

- Challenge: `input-keypress-architecture`
- Corpus: DotState at `a4a5986eafa496dee73a93141588cffaed559f80`
- Fixture: `dotstate-faithful-20260723-v1`
- Control: `no-stele`
- Treatment: `faithful` with prompt-time recall enabled
- Model: `anthropic/claude-haiku-4.5`
- Design: 2 paired attempts with alternating order
- Limits: 30 turns and $3.00 requested Claude cap per arm; $12.00 maximum
  whole-repeat cap

## Question and value mechanism

The agent must explain why keypress failures recur across DotState input fields,
cite source evidence, and leave the source untouched. The faithful graph contains
the prior architectural cluster around the global input-mode gate, per-screen
focus reporting, char-before-keymap ordering, and duplicated routing. Prompt-time
recall should surface that cluster before the agent starts exploring.

This is an investigation-efficiency question. Correctness may tie because a
control can derive the same architecture from source. The registered benefit is
reaching the complete explanation with fewer investigative calls and less active
time, not claiming hidden knowledge that the repository itself contains.

## Oracle and measures

The answer-file verifier awards five deterministic points: global focus gate,
manual per-screen reporting, char-before-keymap ordering, duplicated routing,
and concrete Rust source evidence. The mutation audit permits only
`/workspace/.submission/answer.md`.

Report every raw pair and separate:

1. prompt-time recall injection and the node cluster surfaced;
2. deterministic reward;
3. investigative and total tool calls;
4. agent execution time and trial wall time;
5. input, output, cache-read, and cache-write tokens;
6. authoritative Gateway cost;
7. the first-request context delta when raw JSONL is retained.

## Predictions and stopping rule

1. Both treatment arms receive the registered prompt-time recall cluster.
2. Treatment reward is not lower than control reward.
3. The paired median favors treatment on investigative calls and agent time.
4. Gross token and cost results are reported even if fixed Stele tare outweighs
   the saved investigation.
5. All four arms finish valid and non-truncated.

Run exactly two pairs. Do not tune the prompt, verifier, fixture, graph, model,
or budget after observing an outcome, and do not rerun an invalid arm. An
invalid arm makes the result apparatus-only.

## Registered invocation

```text
oracle.py run-repeat input-keypress-architecture-v1 \
  --attempts 2 \
  --control-condition no-stele \
  --treatment-condition faithful \
  --source "$STELE_BENCH_SOURCE" \
  --revision a4a5986eafa496dee73a93141588cffaed559f80 \
  --fixture fixtures/dotstate-v1/fixture.json \
  --challenge input-keypress-architecture \
  --model anthropic/claude-haiku-4.5 \
  --max-turns 30 \
  --max-budget-usd 3 \
  --acknowledge-paid-repeat-cap-usd 12
```

## Result (2026-08-11)

V1 is apparatus-only and makes no paired memory-value claim. Both controls hit
the $3 Claude cap before writing a valid answer. Both faithful arms completed
and wrote grounded answers, but the registered verifier scored each 0.8. One
control was also ATIF-invalid after truncation, so only one pair was analysable.

Prompt-time recall worked as intended: before the faithful agent acted, it
surfaced nine memories including the registered focus-gate, per-screen focus,
and char-before-keymap cluster. The two faithful trajectories then fetched that
cluster directly. Attempt 2 used 14 investigative calls versus the truncated
control's 25, and reported $0.93 versus $3.06, but invalidity prevents treating
that directional difference as a result. Attempt 1 used 47 versus 28 calls due
to an immediate Explore subagent, showing substantial agent-policy variance.

Post-run audit found two verifier false negatives. The first answer explicitly
described char insertion before keymap lookup; the second explicitly described
duplicated character insertion across five screen files. The shell verifier's
line-oriented regexes split the related clauses at Markdown newlines. V2 must
use a separately pinned, whitespace-normalized verifier and must not pool V1.
