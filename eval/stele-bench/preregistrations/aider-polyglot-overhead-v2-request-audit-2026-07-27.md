# Class C v2 retained-request and delivery audit

Date: 2026-07-27

Run: `aider-overhead-replication-v2`

This is a post-run mechanism audit of retained evidence. It does not change the
frozen result, repair its validity failure, or authorise subtracting a tare from
its gross measurements.

## Retained evidence

- 24 ATIF trajectories, one per arm and attempt
- 280 model calls
- provider-reported prompt, completion, cache-read, and cache-creation usage on
  every call
- identical first user messages within every treatment/control pair
- Claude Code session transcripts, including hook attachments that ATIF omits
- exact Stele artifact digest
  `8708cca650f1b44ea0558c97dccc3d35f42474dc87026d026f1645e9f6fe6874`
- harness/model: Claude Code and `anthropic/claude-haiku-4.5`

The ATIF-normalised trajectories do not retain hook-injected prompt text.
Searching them alone therefore misses delivery. The treatment session JSONLs
do retain `hook_additional_context` attachments. All 12 contain Auto-RAG context
with `KNOW-3` and `TASK-2`; attempts 4 and 8 also contain a second node-bearing
attachment. A replay with the corrected generic detector reports injection in
12/12 treatment attempts.

The result JSON remains immutable and still reports
`memory_uptake.treatment.injected = 0`. That field is now known to be an
instrumentation omission. Future borrowed-suite receipts scan the retained
session JSONLs and count only node-bearing Stele hook attachments.

## First-request observation

The treatment-minus-control first-request prompt-token difference is 17,215 in
8 pairs, 17,214 in 2, and 17,216 in 2. It is not one exact constant across all
12 pairs.

| Attempt | Control first prompt | Control cache read | Control cache create | Treatment first prompt | Treatment cache read | Treatment cache create | Prompt delta | Cache state matched? | Registered status |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 1 | 31,997 | 0 | 30,842 | 49,212 | 0 | 48,057 | 17,215 | yes | analysable |
| 2 | 32,200 | 0 | 30,842 | 49,415 | 44,923 | 3,134 | 17,215 | no | analysable |
| 3 | 35,256 | 30,842 | 0 | 52,471 | 0 | 48,057 | 17,215 | no | excluded |
| 4 | 31,913 | 0 | 30,842 | 49,127 | 44,923 | 3,133 | 17,214 | no | analysable |
| 5 | 31,904 | 30,842 | 0 | 49,119 | 44,923 | 3,134 | 17,215 | yes | analysable |
| 6 | 31,530 | 0 | 30,842 | 48,745 | 44,923 | 3,134 | 17,215 | no | analysable |
| 7 | 31,670 | 30,842 | 0 | 48,885 | 0 | 48,057 | 17,215 | no | analysable |
| 8 | 32,012 | 0 | 30,842 | 49,226 | 44,923 | 3,133 | 17,214 | no | analysable |
| 9 | 35,118 | 30,842 | 0 | 52,333 | 44,923 | 3,134 | 17,215 | yes | analysable |
| 10 | 31,529 | 30,842 | 0 | 48,745 | 44,923 | 3,135 | 17,216 | yes | analysable |
| 11 | 31,628 | 30,842 | 0 | 48,844 | 44,923 | 3,135 | 17,216 | yes | analysable |
| 12 | 31,939 | 0 | 30,842 | 49,154 | 44,923 | 3,134 | 17,215 | no | excluded |

Seven of 12 pairs have different first-request cache states across arms. Prompt
totals remain directly comparable as provider usage, but cache-read and
cache-creation values cannot be paired as if both arms began in the same cache
stratum.

## Cache-block evidence

The retained first requests expose two repeatable treatment blocks:

| Operational block | Observed tokens | Evidence |
| --- | ---: | --- |
| Control stable prefix | 30,842 | either read or created on every control first request |
| Treatment stable prefix | 44,923 | read on warm treatment first requests |
| Treatment prompt-local block | 3,133 to 3,135 | created after the stable treatment prefix on warm first requests |

The stable-prefix difference is 14,081 tokens. Adding the roughly 3,134-token
prompt-local treatment block reproduces the roughly 17,215-token total
difference seen on cold requests.

These are operational cache blocks, not content categories. In particular, the
3,133 to 3,135-token block is the full treatment message-prefix block. It must
not be labelled pure dynamic graph context.

## Reconstructed first-request envelopes

Claude Code `2.1.220` was rerun in the original
`harbor-local/claude-rust:2.1.220-rust-1.88.0` image with the original `/app`
workspace, model, plugin `0.48.1`, and retained hook outputs. Its Anthropic base
URL pointed to a local HTTP recorder, so no model request was made. The replayed
plugin tree, excluding Claude's `.orphaned_at` marker, hashes identically to the
retained tree.

| Envelope property | Control | Treatment |
| --- | ---: | ---: |
| Serialized request bytes | 120,971 | 178,458 |
| Tool definitions | 24 | 43 |
| Serialized tool-array bytes | 83,464 | 135,243 |
| Final system-role attachment characters | 6,305 | 11,905 |

All 24 common tool definitions are byte-identical. The main system arrays and
first user messages are also byte-identical. The treatment adds 19 tools: three
generic MCP resource helpers and 16 Stele meta-tools. It adds 5,600 characters
to the final attachment block through SessionStart context, the Stele search
agent, MCP instructions, plugin skill listings, and the prompt-time hook. The
Auto-RAG portion, including its Claude Code wrapper, is 465 of those characters.

The successful body summaries and hashes are retained in
`aider-polyglot-overhead-v2-envelope-reconstruction-2026-07-27.json`. Raw bodies
are intentionally not committed because they contain volatile session metadata
and duplicate the full shipped tool catalog.

## Zero-generation content decomposition

The Gateway does expose Anthropic's `/v1/messages/count_tokens` route. It rejects
Claude Code's beta mid-conversation `system` role, so the exact request body
cannot be submitted unchanged. For the tool comparison, both envelopes were
counted with the same minimal user message. For the attachment comparison, the
retained attachment text was lifted unchanged into a top-level system block;
the treatment tools, main system, and first user message were held constant.

| Controlled difference | Count A | Count B | Estimated delta |
| --- | ---: | ---: | ---: |
| Control tools to treatment tools | 29,585 | 45,278 | 15,693 |
| Stock attachment to installed treatment attachment without Auto-RAG | 47,531 | 48,914 | 1,383 |
| Treatment attachment without Auto-RAG to full treatment attachment | 48,914 | 49,053 | 139 |

This yields the modal first-request decomposition:

| Content category | Estimated tokens | Share of 17,215 |
| --- | ---: | ---: |
| Added tool catalog | 15,693 | 91.16% |
| Installed message surface excluding Auto-RAG | 1,383 | 8.03% |
| Two-node Auto-RAG delivery | 139 | 0.81% |
| **Total** | **17,215** | **100.00%** |

The independently counted components reconcile exactly to the modal observed
17,215-token first-request difference. This is a content decomposition, while
14,081 + roughly 3,134 is the separate cache-boundary decomposition. They must
not be mixed.

Anthropic documents token counting as an estimate that can differ from actual
usage. The endpoint also required the system-role adaptation above. Therefore
139 is the best zero-generation estimate for this exact two-node delivery, not
an authoritative actual-request token receipt and not a customer graph-size
curve.

References:

- <https://platform.claude.com/docs/en/api/messages/count_tokens>
- <https://platform.claude.com/docs/en/build-with-claude/token-counting>
- <https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching>
- <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>

## Cost evidence boundary

All 24 `cost_metrics.authoritative_receipt` fields in the exported v2 result are
null. The stored dollar figures are model-reported or rate-based cross-checks,
not authoritative Gateway billing deltas. Token usage is provider-reported, but
the v2 dollar comparison does not satisfy the authoritative-receipt requirement
for a future crossover claim.

## What is and is not identified

The retained evidence identifies:

1. a narrow 17,214 to 17,216-token first-request installed-arm differential for
   this artifact, harness, model, prompt, and two-node graph;
2. a 14,081-token stable-prefix difference plus a roughly 3,134-token
   prompt-local treatment cache block;
3. a zero-generation content estimate of 17,076 standing installed tokens and
   139 Auto-RAG tokens for the modal first request; and
4. node-bearing Auto-RAG delivery in all 12 treatment sessions.

It does not identify:

1. an authoritative actual-request count for the 139-token dynamic estimate;
2. a customer-real graph-size curve;
3. a matched behavioral residual after the first request, because later tool,
   edit, and request histories diverge; or
4. authoritative dollar cost for this run.

Therefore the old `+52.53%` median gross input result remains gross,
validity-limited, and uninterpretable as a tare estimate. Nothing in this audit
is subtracted from it.

## Decision

Do not register the paid three-cell tare calibration for this first-request
question. The retained receipts, exact local envelope reconstruction, and
zero-generation controlled counts are sufficient to decide the mechanism:
the modal cold differential is overwhelmingly the installed surface, led by the
tool catalog; the two-node Auto-RAG text is about 0.81% of it.

This does not rescue or permit subtracting from the invalid gross benchmark.
A future paid calibration is justified only for a new claim about full-run
cached cost, larger graph payloads, behavioral residual, or authoritative
dollars, with matched cache states and billing receipts preregistered first.
