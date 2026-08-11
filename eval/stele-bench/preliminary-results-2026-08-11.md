# Preliminary Stele benchmark evidence

> This was the initial closeout note. The canonical v0.1 release report now
> lives at [`results/2026-08-preliminary/README.md`](results/2026-08-preliminary/README.md)
> and includes all three accepted cohorts plus every excluded admission.

Date: 2026-08-11

These are early paired observations, not a claim of statistical significance.
The controlled results use frozen prompts, verifiers, source, graph fixtures,
conditions, and stopping rules.

## Clear investigation win

Run: `input-keypress-architecture-v3`

Question: why keypress bugs keep recurring in DotState input fields.

Both arms were valid, non-truncated, and earned full reward. With Stele's
faithful graph and prompt-time recall, the agent reached the same grounded
architectural diagnosis with substantially less investigation.

| Condition | Reward | Investigative calls | Total calls | Agent time | Wall time | Input tokens | Recorded cost cross-check |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| no Stele | 1 | 27 | 28 | 179.03s | 193.52s | 1,811,074 | $0.9286 |
| Stele | 1 | 16 | 20 | 92.16s | 116.84s | 519,028 | $0.4268 |

Treatment relative to control:

- 11 fewer investigative calls, a 40.7% reduction;
- 8 fewer total calls, a 28.6% reduction;
- 86.87 seconds less agent execution, a 48.5% reduction;
- 76.68 seconds less wall time, a 39.6% reduction;
- 1,292,046 fewer input tokens, a 71.3% reduction;
- $0.5018 lower recorded cost cross-check, a 54.0% reduction.

The live treatment transcript showed prompt-time recall surface the registered
keypress/focus cluster before source investigation. The portable v3 result was
exported before ordinary no-marker runs retained node-delivery evidence, so it
incorrectly reports zero injections. This mechanism observation is therefore
operator-audited, not independently recoverable from the portable record. The
recorder now falls back to node-bearing Stele delivery detection for graph runs,
with a regression test.

At N=1, this is a concrete product example and a directionally strong result.
It does not estimate how often Stele produces this effect across tasks.
The durable record has no authoritative Gateway receipt and does not retain the
raw first-request context delta, so neither is claimed here.

## Fixed-overhead check

Run: `package-cache-is-empty-v2`

This explicit one-line behavior change was selected because project memory
should not help. Both arms were valid and earned full reward.

| Condition | Reward | Investigative calls | Total calls | Agent time | Wall time | Input tokens | Recorded cost cross-check |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| no Stele | 1 | 5 | 7 | 45.37s | 78.64s | 288,525 | $0.5841 |
| Stele | 1 | 4 | 6 | 67.08s | 102.68s | 372,045 | $0.7102 |

Stele used one fewer tool call but added 21.71 seconds of agent execution,
24.04 seconds of wall time, 83,520 input tokens, and $0.1261 to the recorded
cost cross-check. This is the expected fixed tare on a task with little memory
value, and it is reported beside the value case rather than hidden.

## Real-session comparison

The retained DotState session playbacks provide a separate, uncontrolled field
example using the same user question:

| Session | Time | Tool calls | Subagents | Tokens | Cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| no Stele | 9 min | 7 | 1 | 450K | $1.08 |
| Stele | 47 sec | 3 | 0 | 154K | $0.60 |

Both sessions reached the same structural conclusion. This comparison is useful
as product evidence, but it was not randomized or preregistered and should not
be pooled with the controlled benchmark.

## Stele-side inefficiency found and reduced

Installed Claude/Codex harnesses already receive the operating doctrine through
their native integration files. The MCP server was repeating the full 9,184
character doctrine in its initialization response. Harness-aware initialization
now sends a 651 character operational summary, removing 8,533 repeated
characters, about 13% of the measured Stele MCP bootstrap payload. Manually
configured and hosted MCP clients keep the full instructions.

The remaining dominant fixed payload is the dispatch tool schema surface. It is
larger than the instruction block and needs a separate compatibility-aware design.

## Reproducibility

- DotState commit: `a4a5986eafa496dee73a93141588cffaed559f80`
- Integration artifact SHA-256:
  `5deb3a6046ad0faff880536d2489368f25015bf2ab406eacb3e1e4a1099fd2e6`
- Keypress model: `anthropic/claude-sonnet-4.5`
- Keypress challenge SHA-256:
  `81c7c1e753a21c8993c865c834029703e8f67de898a49a8f77e1a8c2074840a2`
- Keypress verifier SHA-256:
  `61dd13e360344100f9b4e3d38ed3be35da81709b9f887f54aabefa231f2b0860`
- Tare model: `anthropic/claude-haiku-4.5`
- Tare challenge SHA-256:
  `00da98b69521e2ac1b8a7f9baad1debf974c0da60c397358520f38c40d6dd3fc`
- Tare verifier SHA-256:
  `704dc9ee5f56a1c19fb8a106a6073f133583e1ae9ee12b5865763727d9556b7e`
- Keypress record:
  `results/input-keypress-architecture/2026-08-11-input-keypress-architecture-v3.json`
- Keypress HTML:
  `results/input-keypress-architecture/index.html`
- Tare record:
  `results/package-cache-is-empty/2026-08-11-package-cache-is-empty-v2.json`
- Tare HTML:
  `results/package-cache-is-empty/index.html`

V1 and V2 of the keypress experiment and V1 of the tare are retained as
apparatus-only admissions. Their outcomes are not pooled into these results.
