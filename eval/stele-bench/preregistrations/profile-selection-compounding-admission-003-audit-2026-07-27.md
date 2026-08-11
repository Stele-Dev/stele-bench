# Profile-selection compounding admission 003 audit

Date: 2026-07-27

Status: admission 003 invalid; the v3/v4 experiment setup is retired before
any cohort pair.

This note records a pre-cohort audit. It does not edit the locked v1-v4
pre-registrations, and admission 003 remains discarded from every analysis.

## Retained verifier evidence

The session-2 reward was zero in both arms, but neither zero was behavioral.
The retained verifier evidence says:

| Arm | `live_profile_picker_contract` | `library_tests` | Verifier outcome |
| --- | --- | --- | --- |
| control | not emitted | not emitted | `undeclared workspace mutation`; `Cargo.lock` existed only in the workspace |
| treatment | not emitted | not emitted | `undeclared workspace mutation`; `Cargo.lock` existed only in the workspace |

The pinned source commit does not contain `Cargo.lock`. The challenge permits
the two candidate source files and `target`, but not `Cargo.lock`. The mutation
wrapper therefore returned reward zero before
`challenges/profile-selection-live-path/verifier.sh` could run either Cargo
test command. Admission 003 did not establish executable behavioral scoring.

Under the v1 criteria, this violates the requirement that all sessions finish
without infrastructure error and that the frozen verifier produce a valid
behavioral reward for both arms. Under v3's final gate, a failed admission
retires this experiment setup before the cohort and does not authorize another
apparatus correction or paid admission. No cohort pair may run under v4.

## Admission-rule interpretation fixed before cohort data

For this experiment, the stopping rule is additional to the six mechanical
admission checks. A syntactically valid reward of zero is admissible for one
arm, consistent with the statement that treatment need not win. A clean 0-0
session-2 pair, however, means neither arm completed the second task within the
fixed limits and retires the candidate/model combination before the cohort.

This strict reading prevents a floor tie plus lower treatment cost from being
reported as observed payback when neither arm accomplished the reuse task.
Admission 003 does not reach this behavioral interpretation because its two
zeros were produced by the mutation wrapper.

## Resumable cohort requirement

Any future one-pair batching implementation must persist all ten pair IDs and
their arm orders before the first paid pair. Resume must pass the persisted
assignment into execution. Recomputing alternation from an attempt index is
not sufficient, even if it currently yields the same order.

The treatment's failure to write graph memory in admissions 001 and 003 is an
admissible behavioral observation and is not an apparatus defect. If that
pattern appears in a future valid cohort, it is reported as an intake failure
and product finding, not tuned away and not framed as disappointing payback.
