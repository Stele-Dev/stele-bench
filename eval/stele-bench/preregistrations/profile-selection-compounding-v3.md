# Profile-selection compounding experiment v3

Status: locked before admission attempt 003. This version inherits
`profile-selection-compounding-v2.md` in full except for the setup correction
below.

## Why a new version exists

The discarded v2 admission, `profile-selection-admission-002`, produced valid
session-1 answers in both arms and a valid session-2 reward in the control arm.
The treatment session-2 model was never invoked. Harbor spent 360 seconds in
agent setup downloading Claude Code, then emitted no trajectory, model, tokens,
cost, or verifier reward.

The v2 admission is discarded and never contributes to the cohort.

## Frozen correction

The task pack now starts from the separately built local image
`harbor-local/claude-rust:2.1.220-rust-1.88.0`, defined by
`eval/stele-bench/images/claude-rust/Dockerfile`. It pins the prior Rust image
digest, installs the native Rust verifier dependencies and Claude Code 2.1.220,
and verifies that Claude version during its one-time build. Harbor therefore
finds `claude` on `PATH` and skips its per-session network bootstrap. Task image
builds only copy the pinned source tree onto that local base.

No prompt, repository revision, model, session limit, budget, graph behavior,
verifier, response grading, outcome rule, metric, or cohort size changes.

## Final gate

Run the Harbor install-only check twice and require both trials to complete
without a Harbor exception, with the second run using the existing image cache.
Then run exactly one new discarded admission pair. If it satisfies the inherited
admission criteria, run the unchanged fixed cohort of 10 paired chains. If it
does not, retire this experiment setup before the cohort. Do not add another
apparatus correction or paid admission attempt.
