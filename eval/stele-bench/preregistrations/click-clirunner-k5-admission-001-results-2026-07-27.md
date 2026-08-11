# Click `CliRunner` K=5 admission 001 result

Date: 2026-07-27

Status: failed discarded admission; cohort not authorized; no rerun

## Registered decision

The v1 candidate fails admission. Both arms recorded 0/5 rewards, the control
chain was invalid, three sessions ended in infrastructure error, treatment did
not write graph state until session 5, and no exact prior node version was
delivered in a later session. The registered 20-pair cohort must not start.

This result does prove one previously missing boundary: all ten sessions have
exclusive, settled, authoritative Gateway deltas obtained while the runner held
the dedicated-key lock. Total observed spend was `$10.32053212`, below the
approved `$30.00` cap.

The durable machine-readable summary is
`eval/stele-bench/results/click-clirunner-k5/2026-07-27-click-clirunner-k5-admission-001.json`.
The retained local pair receipt has SHA-256
`fc8fc640f6f9ce940a51b09831877df89f945670c3b589976f58f49b2af88c6a`.

## Admission gates

| Gate | Result | Evidence |
| --- | --- | --- |
| Ten pinned sessions without infrastructure, network, or source-boundary failure | Fail | Three output-token API errors; control session 4 attempted external `curl` |
| Valid behavioral rewards and at least 4/5 passes per arm | Fail | Recorded reward was 0/5 in both arms; several zeros were validity-limited by the mutation wrapper |
| Exclusive authoritative cost for all ten sessions | Pass | Ten settled Gateway deltas, each marked exclusive under the attested key lock |
| Treatment graph is the only state crossing sessions | Pass | All four treatment graph-boundary checks passed; source reset passed 5/5 |
| Natural treatment write before a reuse opportunity | Fail | No agent-authored node through session 4; one completed task was created in session 5 |
| Exact prior node version delivered later | Fail | No session named a version written in an earlier session |
| K=5 cost remains arithmetically feasible after session 2 | Pass | `$0.84349139` deficit was below the remaining control cost of `$2.85175920` |

## Authoritative cost

| Prefix | Control | Treatment | Treatment minus control |
| ---: | ---: | ---: | ---: |
| 1 | `$1.23099345` | `$1.37078613` | `+$0.13979268` |
| 2 | `$1.39488105` | `$2.23837244` | `+$0.84349139` |
| 3 | `$1.94577030` | `$3.12048879` | `+$1.17471849` |
| 4 | `$2.99971860` | `$4.35786627` | `+$1.35814767` |
| 5 | `$4.24664025` | `$6.07389187` | `+$1.82725162` |

Treatment cost 43.03% more than control in this discarded pair. Treatment also
used 9,532,217 input tokens versus 6,511,996 control tokens, a 46.38% increase.
These are descriptive admission observations only. They are not a crossover
estimate and cannot be promoted into an N=20 claim.

## Validity audit

### Mutation-wrapper defect

Six sessions produced `undeclared workspace mutation` evidence containing
Python `__pycache__` files created by ordinary imports. Four sessions were
force-zeroed solely by those runtime files: control 1, treatment 1, control 4,
and control 5. Treatment 4 and treatment 5 also changed the undeclared
`tests/test_stream_lifecycle.py`, so removing bytecode caches would not make
those two mutations valid.

The generic wrapper now excludes `__pycache__`, `.pyc`, and `.pyo` symmetrically
from its baseline and live snapshot. That repair was made after spend and does
not change this receipt or authorize a v1 rerun.

### Infrastructure errors

Control session 3 and treatment sessions 2 and 3 ended with:

> API Error: Claude's response exceeded the configured output token maximum.

They retain their exclusive cost but are infrastructure errors, not behavioral
zeros.

### External-source attempt

Control session 4 attempted:

```text
curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11
```

`curl` was unavailable, but the attempted external trajectory correctly makes
that source boundary invalid.

## Graph behavior

Treatment preserved one graph across all five fresh conversations. Auto-RAG
injected the fixture's existing `KNOW-3` and `TASK-2` nodes in every session,
but these were not created by an earlier evaluated session. Sessions 1 through
4 made no graph write. Session 5 created and completed one task, too late for
later reuse. Explicit Stele reads occurred in sessions 4 and 5, but neither
returned an exact prior version from this chain.

## Final interpretation

The live billing receipt exists and the treatment state boundary works. The
product loop required by the experiment does not: no natural pre-reuse write
and no later exact delivery occurred. The correctness result is additionally
invalidated by bytecode-cache handling, infrastructure errors, and one external
source attempt.

The locked rule says a candidate with no pre-reuse graph write or later
delivery is not tuned or rerun. Any future paid K-session design therefore
requires a new preregistration version, fresh zero-model qualification that
includes the full mutation wrapper, and new explicit approval. No v1 cohort is
authorized.
