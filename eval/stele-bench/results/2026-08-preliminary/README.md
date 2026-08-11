# When does project memory help a coding agent?

Preliminary controlled results from Stele, August 2026.

These are case-level observations, not a general productivity estimate. The
three accepted cohorts use different tasks, models, and memory mechanisms. They
are reported together for transparency and are never pooled into a single
effect size.

## Executive summary

| Cohort | N | Correctness | Efficiency result | Interpretation |
| --- | ---: | --- | --- | --- |
| `input-keypress-architecture-v3` | 1 pair | 1/1 vs 1/1 | Stele used 40.7% fewer investigative calls, 48.5% less agent time, 39.6% less wall time, 71.3% fewer input tokens, and 54.0% lower model-reported cost | Large descriptive efficiency win on one investigation task |
| `package-cache-is-empty-v2` | 1 pair | 1/1 vs 1/1 | Stele added 21.71s agent time, 24.04s wall time, 83,520 input tokens, and $0.1261 to the recorded cost cross-check | Fixed overhead on a task where memory should not help |
| `settings-label-roundtrip-v4` | 2 pairs | 2/2 vs 2/2 | Median investigative calls improved by 0.5, while total calls, time, tokens, and cost were worse | Memory was delivered and used, but did not improve correctness or overall efficiency |

No row establishes a stable average effect. The positive, negative, and null
results are equally part of the preliminary finding.

## What was tested

Each controlled pair held the task, DotState source commit, model, harness,
budget, verifier, and resource envelope fixed. The control ran without Stele.
The treatment ran with a frozen DotState graph restored into Stele. The
settings-label treatment disabled prompt-time Auto-RAG so it could isolate an
edit-boundary reminder.

The intended question is narrow: does project memory change correctness or the
amount of investigation required for this task under this frozen protocol?

## Accepted evidence

### Input architecture investigation

Run: `input-keypress-architecture-v3`

Question: why keypress bugs keep recurring in DotState input fields.

| Condition | Reward | Investigative calls | Total calls | Agent time | Wall time | Input tokens | Model-reported cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| no Stele | 1 | 27 | 28 | 179.03s | 193.52s | 1,811,074 | $0.9286 |
| Stele | 1 | 16 | 20 | 92.16s | 116.84s | 519,028 | $0.4268 |

The Stele arm reached the same grounded architectural diagnosis with 11 fewer
investigative calls and 1,292,046 fewer input tokens. This is a valid N=1
descriptive result. It does not estimate how often this effect occurs.

The retained trajectory shows prompt-time recall surfacing the relevant
keypress and focus knowledge before source investigation. The original portable
summary was exported before the fallback delivery recorder existed, so its
`memory_uptake` field incorrectly reports zero injections. The trajectory is the
recoverable evidence for the mechanism claim. There is no authoritative Gateway
billing receipt or retained raw first-request context delta, so neither is
claimed.

### Low-memory fixed-overhead check

Run: `package-cache-is-empty-v2`

This one-line behavior change was selected because project memory should not
help. Both arms were valid and correct.

| Condition | Reward | Investigative calls | Total calls | Agent time | Wall time | Input tokens | Recorded cost cross-check |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| no Stele | 1 | 5 | 7 | 45.37s | 78.64s | 288,525 | $0.5841 |
| Stele | 1 | 4 | 6 | 67.08s | 102.68s | 372,045 | $0.7102 |

Stele used one fewer tool call but added time, tokens, and cost. This damaging
result stays beside the value case because it measures the fixed cost paid when
memory contributes little.

### Settings label round-trip

Run: `settings-label-roundtrip-v4`

All four arms solved the hidden cross-file string contract:

- no Stele: 2/2, Wilson 95% interval 34.2% to 100%;
- Stele without prompt-time Auto-RAG: 2/2, same interval;
- paired correctness difference: 0 percentage points;
- exact one-sided McNemar: `p = 1.0`.

| Pair | Condition | Reward | Investigative calls | Total calls | Agent time | Wall time | Gateway cost |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | no Stele | 1 | 18 | 21 | 88.59s | 114.60s | $0.32 |
| 1 | Stele, no Auto-RAG | 1 | 11 | 18 | 83.25s | 128.21s | $0.39 |
| 2 | no Stele | 1 | 18 | 23 | 104.52s | 138.51s | $0.44 |
| 2 | Stele, no Auto-RAG | 1 | 24 | 31 | 154.74s | 191.07s | $0.78 |

The reminder was delivered and observably used in both treatment runs. The
controls also recovered the contract from source and solved the task. Treatment
cost $0.41 more across the cohort. This is a valid null correctness result with
mixed, mostly negative efficiency evidence.

## Excluded runs and why

Excluded outcomes are retained, not repaired or reinterpreted. They do not
contribute to the accepted effect descriptions above.

| Run | Reason | What remains informative |
| --- | --- | --- |
| `drift-r1` | The question allowed multiple reasonable interpretations. The apparent N=1 signal did not survive N=5. | Demonstrates why ambiguous markers cannot support a memory claim |
| keypress v1 | Turn-cap, ATIF, and whitespace-sensitive verifier defects | Directional behavior only; no paired claim |
| keypress v2 | Both agents inspected forbidden repository history and the verifier rejected valid terminology | Directional behavior only; no paired claim |
| package-cache v1 | Ordinary Cargo execution created a lockfile excluded by the mutation allowlist | Exposes approximate fixed overhead, but not an admitted comparison |
| settings-label v1 | Mutation allowlist rejected valid outputs and two arms exceeded the requested ceiling | Apparatus debugging only |
| settings-label v2 | Cold builds caused both controls to exceed the ceiling and exit nonzero | Apparatus debugging only |
| settings-label v3 | The first control was correct but exceeded the clean-exit budget | Isolates remaining budget headroom defect |

The machine-readable source of this table is
[`admissions.json`](admissions.json).

## Qualitative field evidence

The retained uncontrolled DotState session playback reached the same structural
keypress conclusion in 9 minutes without Stele and 47 seconds with Stele. It was
not randomized or preregistered and is not pooled with the controlled result.

The separate tui-forge discovery is also field evidence. It is not a registered
benchmark endpoint.

## Reproducibility

Shared inputs:

- DotState repository: `https://github.com/serkanyersen/dotstate`
- DotState commit: `a4a5986eafa496dee73a93141588cffaed559f80`
- source tree SHA-256: `c4f229ea2229a58dfdedea75b69e9c196e63d6eb9d62ace2f0383bec1b98d5b3`
- graph fixture: `dotstate-faithful-20260723-v1`
- Harbor: `0.20.0`
- Claude Code: `2.1.220`
- task image content digest: `sha256:20d8ec0332f87b76e704c82e4bad54fd20c69148f60fbf8456bf406c52d955a9`

Each accepted evidence directory contains the normalized result, attempt
receipts, certification receipt, Harbor config and result, verifier evidence,
mutation audit, logs, and redacted ATIF trajectory. `manifest.json` hashes every
published file. Host paths are replaced with `$HOME` or `$STELE_BENCH_STATE`;
credentials and local backend identifiers are not exported.

The exact Stele integration executable is identified by SHA-256 in each result.
The v0.1 public release must attach those Linux ARM64 executables or document a
public release with matching bytes before claiming one-command reproduction.

## Validity controls

- Prompts, verifiers, source commits, graph fixtures, budgets, conditions, and
  stopping rules are frozen before paid runs.
- An apparatus correction creates a new preregistered version. Earlier outcomes
  stay retained and excluded.
- Deterministic hidden graders inspect the completed workspace.
- Source inspection is the honest control baseline. The graph must provide value
  beyond what an agent can discover normally.
- Trajectories distinguish memory delivery, observable use, and scored outcome.
- Runtime resources, model, harness, and image are part of the experiment.
- Unintended answer paths, including prohibited repository-history access, make
  the run invalid when the preregistration says so.

See [`METHODOLOGY.md`](../../METHODOLOGY.md) for the full protocol and external
evaluation references.

## Limitations

- Stele's creator designed and ran the benchmark.
- No result has been independently replicated.
- The accepted sample is tiny: two N=1 cohorts and one N=2 cohort.
- Agent behavior is stochastic.
- Cohorts use different models and memory mechanisms.
- The results do not establish statistical significance, a general win rate, or
  a stable average productivity effect.
- The keypress portable export predates the no-marker delivery-recorder fix.
- The fixed-overhead estimate is itself N=1.

## Versioning and next evidence

This report is v0.1 and will not be overwritten by later results.

- v0.2: repeat the frozen keypress v3 protocol for 5 to 10 additional pairs;
- v1.0: expand to task-diverse cohorts with repeated trials.

Repeats establish reliability on one task. Different tasks establish
generalizability. They are separate axes and will be reported separately.
