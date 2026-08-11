# Settings label round-trip preliminary result

Date: 2026-08-11

Run: `settings-label-roundtrip-v4`

Status: valid preliminary null on correctness; mixed efficiency

## Result

Stele's file-boundary reminder fired in both treatment arms and surfaced the
verified display-string contract after the first edit. All four arms still
solved the task:

- no Stele: 2/2, 100% (Wilson 95% CI 34.2% to 100%);
- Stele without prompt-time Auto-RAG: 2/2, 100% (Wilson 95% CI 34.2% to 100%);
- paired correctness difference: 0 percentage points;
- exact one-sided McNemar: `p = 1.0`.

At N=2 this is descriptive only. It does not establish a general product
effect.

## Raw pairs

| Pair | Condition | Reward | Investigative calls | Total calls | Agent time | Wall time | Model-reported cost | Gateway cost |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | no Stele | 1 | 18 | 21 | 88.59s | 114.60s | $1.6239 | $0.32 |
| 1 | Stele, no Auto-RAG | 1 | 11 | 18 | 83.25s | 128.21s | $1.9746 | $0.39 |
| 2 | no Stele | 1 | 18 | 23 | 104.52s | 138.51s | $2.1951 | $0.44 |
| 2 | Stele, no Auto-RAG | 1 | 24 | 31 | 154.74s | 191.07s | $3.8920 | $0.78 |

Treatment-minus-control paired median differences:

- investigative calls: `-0.5` (2.8% fewer);
- total calls: `+2.5`;
- agent execution: `+22.44s`;
- trial wall time: `+33.08s`;
- input tokens: `+601,298.5`;
- authoritative Gateway cost: `+$0.205`.

Gateway spend was $1.93 for the valid cohort: $0.76 control and $1.17
treatment. Treatment cost $0.41 more in total. Including the three excluded
apparatus pilots, observed spend was $7.38 against the approved $30 ceiling.

## Mechanism audit

Both treatment sessions received the registered `stele:nudge recall-file`
after their first edit to `src/screens/settings.rs`. The reminder named
`KNOW-209` and the coupling between `SettingItem::name()` and
`apply_setting_by_name()`.

- Pair 1 launched a Stele read for the node and updated the handler contract.
- Pair 2 fetched the node directly, called it a useful catch, updated the
  handler contract, and added round-trip tests.

This proves delivery and observable use in 2/2 treatment arms. It does not prove
the reminder caused either success. Before the reminder, both treatment agents
had already seen or identified the second string match in source. The controls
also found both literal occurrences and solved the task.

## Interpretation

The registered mechanism and validity predictions passed. The correctness
prediction tied. The registered efficiency prediction did not pass: only the
median investigative-call measure favored treatment; total calls, time, tokens,
and cost did not.

The honest preliminary claim is narrow: anchored memory was delivered at the
intended edit boundary and used, with no observed correctness change on an easy
two-pair task. This cohort does not support a speed or cost benefit. A larger
benchmark needs harder latent contracts where ordinary source inspection does
not reveal both sides immediately.

## Reproducibility

- DotState commit: `a4a5986eafa496dee73a93141588cffaed559f80`
- Model: `anthropic/claude-haiku-4.5`
- Conditions: `no-stele` vs `faithful-norag`
- Challenge manifest SHA-256:
  `90abb78fd844e989db75ef929e6c450a510a67a2d8c9f4f80b7e1cdc26beecd5`
- Verifier SHA-256:
  `7aba6b6088295fbfe28820df64c150f9f95b4640cb97169ce1153becc04fba22`
- Integration artifact SHA-256:
  `90295ac2f8bb26779ee778459e5f82d23fda060ecf47c20090a76a9bcd267500`
- Durable record:
  `results/settings-label-roundtrip/2026-08-11-settings-label-roundtrip-v4.json`
- Self-contained HTML view: `results/settings-label-roundtrip/index.html`

V1 through V3 remain excluded. Their retained preregistrations document the
mutation-allowlist, cold-build, and clean-exit budget failures; none of their
outcomes are pooled into this result.
