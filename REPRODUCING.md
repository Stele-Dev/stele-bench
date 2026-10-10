# Reproducing the preliminary benchmark

This repository supports two separate operations: auditing the exact v0.1
evidence and running the frozen protocols with a compatible Stele integration.

## Audit the published evidence

```sh
git clone https://github.com/Stele-Dev/stele-bench
cd stele-bench/eval/stele-bench
uv sync --locked
STELE_BENCH_ALLOW_MISSING_CORPUS=1 uv run pytest tests/test_publication.py -q
```

`tests/test_publication.py` checks admission completeness, result/admission
agreement, manifest coverage and hashes, and the publication redaction policy.

## Prepare the exact source

The agent never receives a Git checkout. The host clones or refreshes a managed
object cache, validates the full commit, then materializes a Git-free tree into
each Docker task.

```sh
cd eval/stele-bench
export STELE_BENCH_SOURCE="$(uv run stele-bench source-prepare \
  https://github.com/serkanyersen/dotstate \
  a4a5986eafa496dee73a93141588cffaed559f80)"

uv run stele-bench certify \
  "$STELE_BENCH_SOURCE" \
  a4a5986eafa496dee73a93141588cffaed559f80 \
  fixtures/dotstate-v1/fixture.json \
  --challenge input-keypress-architecture \
  --challenge package-cache-is-empty \
  --challenge settings-label-roundtrip
```

The expected source tree digest is
`c4f229ea2229a58dfdedea75b69e9c196e63d6eb9d62ace2f0383bec1b98d5b3`.
Certification also records the auto-loaded context removed identically from
both conditions.

## Build the pinned task image

```sh
docker build \
  --tag harbor-local/claude-rust:2.1.220-rust-1.88.0 \
  images/claude-rust
```

The v0.1 Linux ARM64 task image had content digest
`sha256:20d8ec0332f87b76e704c82e4bad54fd20c69148f60fbf8456bf406c52d955a9`.
The Dockerfile pins its base image digest, Rust toolchain, and Claude Code
version.

That image reproduces v0.1. A new run with a current Stele build or a model
newer than Claude Code 2.1.220 needs a newer CLI. That CLI version cannot
install current Stele plugins, and it prices unknown models at a fallback rate
that trips `--max-budget-usd` early. Build the same Dockerfile with a newer
Claude Code and select it for the run:

```sh
docker build --build-arg CLAUDE_CODE_VERSION=2.1.296 \
  --tag harbor-local/claude-rust:2.1.296-rust-1.88.0 images/claude-rust
export STELE_BENCH_TASK_IMAGE=harbor-local/claude-rust:2.1.296-rust-1.88.0
```

Run receipts record the image under `configuration.task_image`. The registered
compounding, K=5 and continuity runs refuse the override.

Docker Desktop and plain Docker on Linux both work. On Linux the configured
arm maps `host.docker.internal` to the container's gateway itself; the control
arm is left untouched.

## Connect a Stele integration

New treatment runs require:

1. an executable Stele integration for the Docker architecture;
2. a reachable compatible backend and its public client key;
3. an executable graph adapter implementing the public JSON-over-stdin
   `prepare`, `restore`, `snapshot`, `destroy`, and `cleanup` operations;
4. Harbor 0.20.x, Docker, and a dedicated model credential: a Vercel AI
   Gateway key (the default), or a first-party Anthropic API key with
   `--model-provider anthropic` (setup and its spending fence are in
   `scripts/benchmark-provisioner/README.md`);
5. an artifact manifest whose `artifact.path`, `artifact.sha256`, and
   `artifact.target` identify the integration executable.

The reusable graph contract is implemented by
`stele_bench.provisioning.ExternalCommandProvisioner`. The included
`scripts/benchmark-provisioner/provision.py` is the reference adapter for a
compatible local Stele/Supabase backend. A different implementation can be
selected without the Stele source repository:

```sh
export STELE_BENCH_PROVISIONER=/absolute/path/to/benchmark-provisioner

uv run ../../scripts/benchmark-provisioner/oracle.py doctor \
  --source "$STELE_BENCH_SOURCE" \
  --artifact-manifest /absolute/path/to/artifact-manifest.json

uv run ../../scripts/benchmark-provisioner/oracle.py run-pair keypress-replication-01 \
  --source "$STELE_BENCH_SOURCE" \
  --artifact-manifest /absolute/path/to/artifact-manifest.json \
  --challenge input-keypress-architecture \
  --acknowledge-paid-pair-cap-usd 10.00
```

`doctor` and `run-pair` fail closed on source, fixture, challenge, verifier,
image, integration artifact, backend, runtime, and spending-fence mismatches.
The paid acknowledgement is a safety control, not a guarantee of provider
billing.

## Exact v0.1 versus a replication

The public evidence identifies every accepted system-under-test executable by
SHA-256. Those Stele executables are not MIT-licensed benchmark artifacts and
are not distributed here. A run with another Stele build is a replication of
the frozen protocol, not a byte-for-byte replay of the original system under
test. Record the new executable digest and do not overwrite the v0.1 results.
