# Stele Bench

Open apparatus and evidence for Stele's preliminary controlled coding-agent
memory evaluation.

**[Read the interpreted results](https://stele-dev.github.io/stele-bench/)**,
including the clear investigation win, measured overhead, safety observation,
and the limits of the compounding-cost hypothesis.

The canonical v0.1 report is
[`When does project memory help a coding agent?`](eval/stele-bench/results/2026-08-preliminary/README.md).
It presents all three accepted cohorts together:

| Cohort | N | Result |
| --- | ---: | --- |
| input keypress architecture | 1 pair | Equal correctness; large descriptive investigation-efficiency win for Stele |
| package cache tare | 1 pair | Equal correctness; Stele added time, tokens, and cost |
| settings label round-trip | 2 pairs | Equal correctness; memory was used, but most efficiency measures were worse |

These results are preliminary case-level observations. They are not pooled and
do not establish a general productivity, win-rate, cost, or significance claim.
Negative, null, and excluded outcomes are retained in the same release.

## Repository map

- `eval/stele-bench`: installable contracts, challenges, fixtures,
  preregistrations, result records, publication bundle, and report code
- `scripts/benchmark-provisioner`: the host oracle, Harbor runners, spending
  fences, and reference local graph provisioner
- `eval/stele-bench/results/2026-08-preliminary/accepted`: accepted raw evidence
- `eval/stele-bench/results/2026-08-preliminary/excluded`: excluded raw evidence
- `eval/stele-bench/results/2026-08-preliminary/admissions.json`: machine-readable
  admission decisions and reasons
- `eval/stele-bench/results/2026-08-preliminary/manifest.json`: byte lengths and
  SHA-256 digests for every file in the preliminary evidence bundle

The evidence bundle includes attempt receipts, certification, Harbor configs and
results, grader evidence, mutation audits, logs, and 43 redacted ATIF
trajectories recovered from the original run environment.

## Verify the release

```sh
cd eval/stele-bench
uv sync --locked
uv run ruff check .
STELE_BENCH_ALLOW_MISSING_CORPUS=1 uv run pytest -q
```

The publication tests verify the admission set, every manifest entry and hash,
and the absence of machine-local paths and common secret shapes.

For clean-clone source certification and new-run instructions, see
[`REPRODUCING.md`](REPRODUCING.md). The full protocol and interpretation rules
are in [`METHODOLOGY.md`](eval/stele-bench/METHODOLOGY.md).

## License and system under test

The benchmark apparatus and publication material are MIT licensed. DotState and
Harbor attribution is recorded in [`NOTICE`](NOTICE).

Stele, Claude Code, model access, and the exact Stele executables identified in
the results are systems under test or external runtime dependencies. They are
not relicensed or distributed by this repository.
