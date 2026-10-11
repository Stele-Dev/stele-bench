from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import gateway_reserve
import pytest

_MODULE_PATH = Path(__file__).with_name("oracle.py")
_SPEC = importlib.util.spec_from_file_location("benchmark_oracle", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
oracle = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = oracle
_SPEC.loader.exec_module(oracle)


def _artifact_manifest(root: Path, *, target: str = "linux-arm64") -> Path:
    artifact = root / "artifact"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"executable")
    artifact.chmod(0o755)
    manifest = root / "artifact.json"
    oracle._atomic_json(
        manifest,
        {
            "schema_version": 1,
            "source": {"head": "f" * 40, "dirty": True},
            "artifact": {
                "path": os.fspath(artifact),
                "sha256": oracle._sha256_file(artifact),
                "target": target,
            },
        },
    )
    return manifest


def _scored_receipt(
    condition: str,
    *,
    reward: float,
    cost: float,
    tokens: int,
    requested_cap: float = 1.0,
    billed_steps: int = 2,
) -> dict:
    # billed_steps defaults to 2 because every memory challenge on this oracle
    # is two-step; a single-step default would let a wrong per-arm-vs-per-trial
    # cap denominator pass here and only misfire on real data.
    # A faithful arm's runner scans the graded session transcript and reports
    # whether the fixture's memory reached the agent; a no-stele control has no
    # graph and reports nothing.
    memory_evidence = (
        None
        if condition == "no-stele"
        else {
            "marker_sha256": "a" * 64,
            "injected": True,
            "channels": ["nudge:recall-file", "rag"],
            "occurrences": 2,
        }
    )
    return {
        "schema_version": 3,
        "run_id": f"pair-{condition}",
        "condition": condition,
        "model": oracle.DEFAULT_MODEL,
        "challenge_ids": [oracle.DEFAULT_CHALLENGE],
        "max_turns": 60,
        "requested_budget_cap_usd_per_trial": "5.00",
        "outcome": "passed" if reward == 1 else "failed",
        "results": [
            {
                "reward": reward,
                "tokens": {
                    "input": tokens,
                    "output": 10,
                    "cache_read": 20,
                    "cache_write": 30,
                },
                "cost": {
                    "claude_reported_amount_usd": cost,
                    "gateway_rate_cross_check_usd": cost / 5,
                    "requested_claude_cli_cap_usd": requested_cap,
                    "billed_step_count": billed_steps,
                    "claude_report_exceeded_requested_cap": cost
                    > requested_cap * billed_steps,
                },
                "timings": {"agent_execution": 10.0, "invocation_wall": 20.0},
                "memory_evidence": memory_evidence,
            }
        ],
    }


def _borrowed_receipt(
    task: str,
    condition: str,
    *,
    reward: float,
    cost: float = 0.2,
    tokens: int = 1000,
    outcome: str | None = None,
    agent_nodes: dict | None = None,
) -> dict:
    """A borrowed-task arm receipt, as run_borrowed_task.py would write it."""

    graph_uptake = None
    if condition == "stele-fresh":
        graph_uptake = {
            "projects": [{"project_id": f"p-{task}"}],
            "project_count": 1,
            "reported_project": {"project_id": f"p-{task}"},
            "reported_project_observed": True,
            "observed": {
                "project_id": f"p-{task}",
                "slug": f"workspace-{task}",
                "agent_nodes_by_type": agent_nodes if agent_nodes is not None else {"task": 1},
                "agent_tasks": {"total": 1, "claimed": 1, "completed": 1},
                "seeded_tasks_touched": 0,
            },
        }
    memory_evidence = (
        {
            "injected": True,
            "channels": ["rag"],
            "occurrences": 1,
            "node_ids": ["KNOW-3", "TASK-2"],
        }
        if graph_uptake is not None
        else None
    )
    resolved = outcome or ("passed" if reward == 1.0 else "failed")
    return {
        "schema_version": 1,
        "run_id": f"suite-{task}-{condition}",
        "condition": condition,
        "model": oracle.DEFAULT_MODEL,
        "challenge_ids": [task],
        "max_turns": 60,
        "requested_budget_cap_usd_per_trial": "0.50",
        "outcome": resolved,
        "graph_uptake": graph_uptake,
        "results": [
            {
                "challenge_id": task,
                "outcome": resolved,
                "reward": reward,
                "tokens": {"input": tokens, "output": 200, "cache_read": 0, "cache_write": 0},
                "cost": {
                    "claude_reported_amount_usd": cost,
                    "gateway_rate_cross_check_usd": cost / 5,
                    "requested_claude_cli_cap_usd": 0.5,
                    "billed_step_count": 1,
                    "claude_report_exceeded_requested_cap": cost > 0.5,
                },
                "timings": {"agent_execution": 30.0, "invocation_wall": 45.0},
                "evidence": {"tool_calls": {"total": 12, "stele": 3 if graph_uptake else 0}},
                "memory_evidence": memory_evidence,
            }
        ],
    }


_CERTIFICATION_RECEIPT = {
    "schema_version": 1,
    "certified": True,
    "zero_model": True,
    "corpus_tree_sha256": "d" * 64,
    "fixture_id": "dotstate-faithful-20260723-v1",
    "cohort_id": "dotstate-foundation",
    "arms": ["faithful"],
    "challenge_ids": [oracle.DEFAULT_CHALLENGE],
    "surfaces_probed": ["corpus", "evidence-corpus", "graph", "challenge"],
    "files_probed": 149,
    "graph_values_probed": 5305,
    "redactions": [
        {
            "path": "CLAUDE.md",
            "sha256": "e" * 64,
            "kind": "auto-loaded-context",
            "pattern": "CLAUDE.md",
        }
    ],
    "findings": [],
}


class FakeReserveChecker:
    """Stand in for the shared-Gateway fence so tests never reach the network."""

    def __init__(self, *, failure: str | None = None) -> None:
        self.calls: list[Decimal] = []
        self.failure = failure

    def __call__(self, *, acknowledged_pair_cap_usd, config_path):
        self.calls.append(Decimal(str(acknowledged_pair_cap_usd)))
        if self.failure is not None:
            raise gateway_reserve.GatewayReserveError(self.failure)
        return {
            "schema_version": 1,
            "checked_at": "2026-07-23T00:00:00Z",
            "key_fingerprint": "a1b2c3d4e5f6",
            "key_id": "key_eval",
            "key_name": "stele-eval",
            "shared_balance_usd": "120.00",
            "shared_total_used_usd": "30.00",
            "production_reserve_usd": "50.00",
            "acknowledged_pair_cap_usd": f"{Decimal(str(acknowledged_pair_cap_usd)):.2f}",
            "headroom_after_reserve_usd": "70.00",
            "headroom_after_pair_usd": "60.00",
            "key_budget": {
                "limit_amount_usd": "25.00",
                "current_spend_usd": "1.00",
                "remaining_usd": "24.00",
                "refresh_period": "monthly",
                "active": True,
            },
            "calibrated_at": "2026-07-20",
            "calibration_age_days": 3,
            "calibration_authority": "vercel-dashboard",
            "advisory": gateway_reserve.ADVISORY,
        }


class FakeDrainChecker:
    """Stand in for the between-arms tripwire; tests never reach the network."""

    def __init__(self, *, drained_usd: str = "1.50", tripped: bool = False) -> None:
        self.calls: list[dict] = []
        self.drained_usd = drained_usd
        self.tripped = tripped

    def __call__(self, *, preflight):
        self.calls.append(preflight)
        observation = {
            "checked_at": "2026-07-23T00:05:00Z",
            "observed": True,
            "reason": None,
            "balance_usd": "118.50",
            "drained_usd": self.drained_usd,
        }
        if self.tripped:
            raise gateway_reserve.GatewayDrainError(
                f"the shared Gateway balance has dropped {self.drained_usd} USD since this "
                "pair's preflight, past its acknowledged 10.00 USD ceiling",
                observation=observation,
            )
        return observation


class FakeRunner:
    def __init__(
        self,
        *,
        faithful_failure: bool = False,
        preflight_failure: bool = False,
        certification_failure: bool = False,
        scored_receipt: Callable[[str, str], dict] | None = None,
        borrowed_receipt: Callable[[str, str], dict] | None = None,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], dict]] = []
        self.faithful_failure = faithful_failure
        self.preflight_failure = preflight_failure
        self.certification_failure = certification_failure
        # (run_id, condition) -> receipt, so a repeat's attempts can differ from
        # each other; the default fake returns the same numbers every time,
        # which would make every aggregate trivially unanimous.
        self.scored_receipt = scored_receipt
        # (task, condition) -> receipt, the same idea for a borrowed suite,
        # where the variation that matters is across tasks rather than repeats.
        self.borrowed_receipt = borrowed_receipt

    def __call__(self, argv, **kwargs):
        args = tuple(str(value) for value in argv)
        self.calls.append((args, kwargs))
        if args[:3] == ("supabase", "status", "-o"):
            return oracle.CommandResult(
                0,
                'API_URL="http://127.0.0.1:54321"\nPUBLISHABLE_KEY="private-test-key"\n',
            )
        if args == ("supabase", "--version"):
            return oracle.CommandResult(0, "2.109.0\n")
        if args[:3] == ("docker", "info", "--format"):
            return oracle.CommandResult(0, "arm64 28.3.0\n")
        if args == ("harbor", "--version"):
            return oracle.CommandResult(0, "0.20.0\n")
        if args == ("uv", "--version"):
            return oracle.CommandResult(0, "uv 0.8.0\n")
        if args == ("git", "--version"):
            return oracle.CommandResult(0, "git version 2.50.0\n")
        if args[:3] == ("git", "rev-parse", "--verify"):
            return oracle.CommandResult(0, f"{oracle.DEFAULT_REVISION}\n")
        if args[:2] == ("git", "rev-parse"):
            return oracle.CommandResult(0, f"{'f' * 40}\n")
        if args[:2] == ("git", "status"):
            return oracle.CommandResult(0, " M packages/example.ts\n")
        if args[:2] == ("git", "diff"):
            return oracle.CommandResult(0, "diff --git a/x b/x\n")
        if args[:3] == ("git", "ls-files", "--others"):
            return oracle.CommandResult(0, "")
        if args == ("node", "--version"):
            return oracle.CommandResult(0, "v24.0.0\n")
        if args == ("pnpm", "--version"):
            return oracle.CommandResult(0, "10.0.0\n")
        if args == ("bun", "--version"):
            return oracle.CommandResult(0, "1.2.0\n")
        if args[1:4] == ("-m", "stele_bench.cli", "certify"):
            assert args[args.index("--challenge") + 1]  # a challenge is always named
            if self.certification_failure:
                # The real CLI still prints the (uncertified) receipt to stdout
                # and the message to stderr before exiting non-zero.
                failed = {**_CERTIFICATION_RECEIPT, "certified": False}
                return oracle.CommandResult(
                    1,
                    json.dumps(failed, indent=2, sort_keys=True) + "\n",
                    "certification failed with 1 finding(s): CLAUDE.md",
                )
            # Indented, multi-line JSON exactly like the real `stele-bench certify`
            # CLI — a compact single line would hide the stdout-parsing path.
            return oracle.CommandResult(
                0, json.dumps(_CERTIFICATION_RECEIPT, indent=2, sort_keys=True) + "\n"
            )
        if len(args) >= 3 and args[1] == os.fspath(oracle.RUNNER):
            condition = args[args.index("--condition") + 1]
            receipt_out = Path(args[args.index("--receipt-out") + 1])
            parent = next(
                (
                    receipt_out.with_name(name)
                    for name in ("pair.json", "quad.json", "repeat.json")
                    if receipt_out.with_name(name).is_file()
                ),
                None,
            )
            if parent is not None:
                assert oracle._read_json(parent)["status"] == "in_progress"
            if condition == "faithful":
                assert args[args.index("--backend-url") + 1] == "http://host.docker.internal:54321"
            receipt = (
                self.scored_receipt(args[args.index("--run-id") + 1], condition)
                if self.scored_receipt is not None
                else _scored_receipt(
                    condition,
                    reward=0.6 if condition == "no-stele" else 1.0,
                    cost=3.0 if condition == "no-stele" else 0.75,
                    tokens=1000 if condition == "no-stele" else 250,
                )
            )
            if "--receipt-out" in args:
                path = Path(args[args.index("--receipt-out") + 1])
                oracle._atomic_json(path, receipt)
            failed = condition == "faithful" and self.faithful_failure
            return oracle.CommandResult(
                1 if failed else 0,
                json.dumps({"ok": not failed, "receipt": receipt}) + "\n",
            )
        if len(args) >= 3 and args[1] == os.fspath(oracle.BORROWED_RUNNER):
            condition = args[args.index("--condition") + 1]
            task = args[3]
            # The control arm must be launched with no Stele inputs at all: a
            # runner that quietly accepted them would let a mis-launched arm
            # look like a valid control.
            if condition == "no-stele":
                assert "--artifact" not in args
                assert "--backend-url" not in args
            else:
                assert args[args.index("--backend-url") + 1] == (
                    "http://host.docker.internal:54321"
                )
            receipt = (
                self.borrowed_receipt(task, condition)
                if self.borrowed_receipt is not None
                else _borrowed_receipt(task, condition, reward=1.0)
            )
            path = Path(args[args.index("--receipt-out") + 1])
            oracle._atomic_json(path, {"ok": True, "receipt": receipt})
            return oracle.CommandResult(0, json.dumps({"ok": True, "receipt": receipt}) + "\n")
        if len(args) >= 3 and args[1] == os.fspath(oracle.INSTALL_ONLY_RUNNER):
            assert "AI_GATEWAY_KEY_EVAL" not in kwargs["env"]
            assert args.count("--condition") == 2
            assert args[args.index("--challenge") + 1] == oracle.DEFAULT_CHALLENGE
            assert args[args.index("--backend-url") + 1] == "http://host.docker.internal:54321"
            envelope = {
                "ok": not self.preflight_failure,
                "failure": (
                    {"code": "configured_structural_preflight_failed", "message": "failed"}
                    if self.preflight_failure
                    else None
                ),
                "receipt": {
                    "mode": "install-only",
                    "conditions": [
                        {"condition": "no-stele", "harbor_exit_code": 0},
                        {
                            "condition": "faithful",
                            "harbor_exit_code": 1 if self.preflight_failure else 0,
                        },
                    ],
                },
            }
            path = Path(args[args.index("--receipt-out") + 1])
            oracle._atomic_json(path, envelope)
            return oracle.CommandResult(
                1 if self.preflight_failure else 0, json.dumps(envelope) + "\n"
            )
        if len(args) >= 3 and args[1] == os.fspath(oracle.PROVISIONER):
            assert "private-test-key" not in (kwargs.get("input_text") or "")
            return oracle.CommandResult(0, '{"ok":true}\n')
        raise AssertionError(f"unexpected command: {args}")


def test_state_root_and_atomic_receipts_are_private(tmp_path: Path) -> None:
    root = oracle._state_root(tmp_path / "state")
    path = root / "runs" / "pair" / "pair.json"
    oracle._atomic_json(path, {"ok": True})
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_custom_provisioner_is_an_executable_public_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provisioner = tmp_path / "adapter"
    provisioner.write_text("#!/bin/sh\nexit 0\n")
    provisioner.chmod(0o755)
    monkeypatch.setenv("STELE_BENCH_PROVISIONER", os.fspath(provisioner))

    assert oracle._provisioner_executable() == provisioner

    provisioner.chmod(0o644)
    with pytest.raises(oracle.OracleError, match="executable regular file"):
        oracle._provisioner_executable()


@pytest.mark.parametrize(
    ("architecture", "target"),
    [("arm64", "linux-arm64"), ("aarch64", "linux-arm64"), ("amd64", "linux-x64")],
)
def test_docker_architecture_mapping(architecture: str, target: str) -> None:
    assert oracle._target_for_arch(architecture) == target


def test_archive_extraction_rejects_extra_members(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name in ("stele-linux-arm64", "unexpected"):
            payload = b"x"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    with pytest.raises(oracle.OracleError, match="exactly"):
        oracle._extract_built_artifact(archive, "stele-linux-arm64", tmp_path / "out")


def test_archive_extraction_ignores_exact_regular_appledouble_member(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "appledouble.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, payload in (
            ("._stele-linux-arm64", b"apple metadata"),
            ("stele-linux-arm64", b"executable bytes"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    output = tmp_path / "out"
    oracle._extract_built_artifact(archive, "stele-linux-arm64", output)
    assert output.read_bytes() == b"executable bytes"
    assert os.access(output, os.X_OK)


def test_archive_extraction_rejects_nonregular_appledouble_member(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "appledouble-link.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        executable = b"executable bytes"
        info = tarfile.TarInfo("stele-linux-arm64")
        info.size = len(executable)
        bundle.addfile(info, io.BytesIO(executable))
        decoy = tarfile.TarInfo("._stele-linux-arm64")
        decoy.type = tarfile.SYMTYPE
        decoy.linkname = "stele-linux-arm64"
        bundle.addfile(decoy)
    with pytest.raises(oracle.OracleError, match="exactly"):
        oracle._extract_built_artifact(archive, "stele-linux-arm64", tmp_path / "out")


def test_doctor_is_zero_cost_and_never_returns_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    runner = FakeRunner()
    result = oracle.doctor(
        source=source,
        state_root=tmp_path / "state",
        runner=runner,
        reserve_checker=FakeReserveChecker(),
    )
    serialized = json.dumps(result)
    assert result["zero_cost"] is True
    assert result["checks"]["harbor"] == "0.20.0"
    assert result["checks"]["docker"]["architecture"] == "arm64"
    assert (
        result["checks"]["supabase"]["docker_reachable_url"] == "http://host.docker.internal:54321"
    )
    assert "gateway-secret" not in serialized
    assert "private-test-key" not in serialized
    assert not any(call[0][1:2] == (os.fspath(oracle.RUNNER),) for call in runner.calls)


@pytest.mark.parametrize(
    ("local_url", "docker_url"),
    [
        ("http://127.0.0.1:54321", "http://host.docker.internal:54321"),
        ("https://localhost:8443/auth/v1", "https://host.docker.internal:8443/auth/v1"),
        ("http://[::1]:54321/rest/v1", "http://host.docker.internal:54321/rest/v1"),
    ],
)
def test_docker_url_translation_is_strict_and_preserves_endpoint(
    local_url: str, docker_url: str
) -> None:
    assert oracle._docker_reachable_url(local_url) == docker_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:54321",
        "http://127.0.0.2:54321",
        "http://user@localhost:54321",
        "http://localhost:54321/path?query=1",
    ],
)
def test_docker_url_translation_never_rewrites_non_loopback_or_ambiguous_url(
    url: str,
) -> None:
    with pytest.raises(oracle.OracleError, match="loopback URL"):
        oracle._docker_reachable_url(url)


def test_doctor_rejects_wrong_harbor_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "secret")
    source = tmp_path / "dotstate"
    source.mkdir()

    def runner(argv, **kwargs):
        if tuple(argv) == ("harbor", "--version"):
            return oracle.CommandResult(0, "0.21.0\n")
        return FakeRunner()(argv, **kwargs)

    with pytest.raises(oracle.OracleError, match="0.20.x"):
        oracle.doctor(
            source=source,
            state_root=tmp_path / "state",
            runner=runner,
            reserve_checker=FakeReserveChecker(),
        )


def test_build_local_artifact_records_dirty_provenance(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    archive_directory = repository / "apps" / "cli" / "dist" / "bin"
    archive_directory.mkdir(parents=True)
    monkeypatch.setattr(oracle, "REPOSITORY_ROOT", repository)
    runner = FakeRunner()
    untracked = repository / "new-source.ts"
    untracked.write_bytes(b"new source bytes")

    def build_runner(argv, **kwargs):
        args = tuple(str(value) for value in argv)
        if args == ("git", "ls-files", "--others", "--exclude-standard", "-z"):
            return oracle.CommandResult(0, "new-source.ts\0")
        if args == ("pnpm", "--filter", "./apps/cli", "run", "build:binaries"):
            assert kwargs["env"]["STELE_BUILD_TARGETS"] == "linux-arm64"
            archive = archive_directory / "stele-linux-arm64.tar.gz"
            payload = b"local-binary"
            info = tarfile.TarInfo("stele-linux-arm64")
            info.size = len(payload)
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.addfile(info, io.BytesIO(payload))
            return oracle.CommandResult(0)
        return runner(argv, **kwargs)

    result = oracle.build_local_artifact(state_root=tmp_path / "state", runner=build_runner)
    manifest = oracle._read_json(Path(result["manifest"]))
    assert manifest["source"]["dirty"] is True
    assert manifest["source"]["status_entries"] == 1
    assert manifest["source"]["untracked_files"] == 1
    assert manifest["source"]["untracked_bytes"] == len(b"new source bytes")
    assert len(manifest["source"]["untracked_content_sha256"]) == 64
    assert manifest["artifact"]["target"] == "linux-arm64"
    assert Path(manifest["artifact"]["path"]).read_bytes() == b"local-binary"
    assert stat.S_IMODE(Path(result["manifest"]).stat().st_mode) == 0o600


def test_untracked_provenance_is_sorted_and_content_sensitive(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "a.txt").write_bytes(b"alpha")
    (repository / "z.txt").write_bytes(b"omega")
    monkeypatch.setattr(oracle, "REPOSITORY_ROOT", repository)
    base = FakeRunner()

    def provenance_runner(argv, **kwargs):
        if tuple(argv) == ("git", "ls-files", "--others", "--exclude-standard", "-z"):
            return oracle.CommandResult(0, "z.txt\0a.txt\0")
        return base(argv, **kwargs)

    first = oracle._git_build_provenance(provenance_runner)
    second = oracle._git_build_provenance(provenance_runner)
    assert first["untracked_content_sha256"] == second["untracked_content_sha256"]
    assert first["untracked_files"] == 2
    (repository / "a.txt").write_bytes(b"changed")
    changed = oracle._git_build_provenance(provenance_runner)
    assert changed["untracked_content_sha256"] != first["untracked_content_sha256"]


def test_untracked_provenance_rejects_symlinks(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    target = repository / "target"
    target.write_text("target")
    (repository / "link").symlink_to(target)
    monkeypatch.setattr(oracle, "REPOSITORY_ROOT", repository)
    base = FakeRunner()

    def provenance_runner(argv, **kwargs):
        if tuple(argv) == ("git", "ls-files", "--others", "--exclude-standard", "-z"):
            return oracle.CommandResult(0, "link\0")
        return base(argv, **kwargs)

    with pytest.raises(oracle.OracleError, match="regular files"):
        oracle._git_build_provenance(provenance_runner)


def test_untracked_provenance_is_bounded(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "large").write_bytes(b"1234")
    monkeypatch.setattr(oracle, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(oracle, "MAX_UNTRACKED_BYTES", 3)
    base = FakeRunner()

    def provenance_runner(argv, **kwargs):
        if tuple(argv) == ("git", "ls-files", "--others", "--exclude-standard", "-z"):
            return oracle.CommandResult(0, "large\0")
        return base(argv, **kwargs)

    with pytest.raises(oracle.OracleError, match="size limit"):
        oracle._git_build_provenance(provenance_runner)


def test_run_pair_writes_comparable_receipt_without_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    manifest = _artifact_manifest(tmp_path)
    runner = FakeRunner()
    result = oracle.run_pair(
        pair_id="pair-001",
        source=source,
        artifact_manifest=manifest,
        acknowledge_paid_pair_cap_usd="10.00",
        reserve_checker=FakeReserveChecker(),
        drain_checker=FakeDrainChecker(),
        state_root=tmp_path / "state",
        runner=runner,
    )
    receipt_path = Path(result["pair_receipt"])
    receipt = oracle._read_json(receipt_path)
    persisted = receipt_path.read_text()
    assert result["ok"] is True
    assert receipt["comparable"] is True
    assert receipt["configuration"]["acknowledged_paid_pair_cap_usd"] == "10.00"
    assert receipt["deltas"]["faithful_reduction_percent"]["tokens_input"] == 75.0
    assert receipt["billing"]["gateway_total_usd"] is None
    assert receipt["billing"]["claude_reported_usd_by_condition"] == {
        "control": 3.0,
        "faithful": 0.75,
    }
    assert "gateway-secret" not in persisted
    assert "private-test-key" not in persisted
    conditions = [
        args[args.index("--condition") + 1] for args, _ in runner.calls if "--condition" in args
    ]
    assert conditions == ["no-stele", "faithful"]
    certification = receipt["provenance"]["certification"]
    assert certification["certified"] is True
    assert certification["redacted_paths"] == ["CLAUDE.md"]
    assert Path(certification["path"]).is_file()


def _repeat_receipts(
    *,
    cited_faithful: dict[int, bool],
    cited_control: dict[int, bool] | None = None,
    investigative: dict[int, tuple[float, float]] | None = None,
    cost: dict[int, tuple[float, float]] | None = None,
    reward: dict[int, tuple[float, float]] | None = None,
) -> Callable[[str, str], dict]:
    """Build a per-attempt receipt factory keyed on the arm's run id.

    Attempts must be able to disagree with each other, otherwise every
    aggregate this exercises is unanimous by construction and the statistics
    are never really tested.
    """

    def factory(run_id: str, condition: str) -> dict:
        # run ids look like "<repeat-id>-attempt-03-treatment". The ROLE comes
        # from the run id, not the condition: once a control can itself be a
        # graph condition (faithful-norag), "which arm is this?" is no longer
        # answerable from the condition alone.
        attempt_part = run_id.split("-attempt-")[1]
        attempt = int(attempt_part.split("-")[0])
        role = attempt_part.split("-", 1)[1]
        label = "control" if role == "control" else "faithful"
        index = 0 if role == "control" else 1
        cited = (
            (cited_control or {}).get(attempt, False)
            if role == "control"
            else cited_faithful.get(attempt, False)
        )
        rewards = (reward or {}).get(attempt, (1.0, 1.0))
        costs = (cost or {}).get(attempt, (3.0, 0.75))
        depths = (investigative or {}).get(attempt, (12.0, 11.0))
        receipt = _scored_receipt(
            condition,
            reward=rewards[index],
            cost=costs[index],
            tokens=1000 if label == "control" else 250,
        )
        receipt["results"][0]["evidence"] = {
            "response_evidence": {"cited": cited},
            "investigation_depth": {"investigative": depths[index], "total": depths[index]},
        }
        return receipt

    return factory


def _run_repeat(
    tmp_path: Path,
    *,
    attempts: int = 5,
    runner: FakeRunner,
    repeat_id: str = "repeat-001",
    max_budget_usd: str = "1.00",
    acknowledgement: str | None = None,
    control_condition: str = "no-stele",
    treatment_condition: str = "faithful",
) -> dict:
    source = tmp_path / "dotstate"
    source.mkdir(exist_ok=True)
    return oracle.run_repeat(
        repeat_id=repeat_id,
        attempts=attempts,
        control_condition=control_condition,
        treatment_condition=treatment_condition,
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        max_budget_usd=max_budget_usd,
        acknowledge_paid_repeat_cap_usd=(
            acknowledgement
            if acknowledgement is not None
            else str(Decimal(max_budget_usd) * 2 * attempts)
        ),
        reserve_checker=FakeReserveChecker(),
        drain_checker=FakeDrainChecker(),
        state_root=tmp_path / "state",
        runner=runner,
    )


def test_run_repeat_reports_a_rate_with_an_interval_never_a_bare_point(
    tmp_path: Path, monkeypatch
) -> None:
    """The whole reason a repeat exists: 5/5 vs 0/5 is a rate, not a coin flip."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(cited_faithful=dict.fromkeys(range(1, 6), True))
    )
    result = _run_repeat(tmp_path, runner=runner)

    assert result["ok"] is True
    receipt = oracle._read_json(Path(result["repeat_receipt"]))
    summary = receipt["summary"]
    assert receipt["comparable"] is True
    assert summary["attempts_run"] == 5
    assert summary["attempts_analysed"] == 5
    assert summary["attempts_excluded"] == []

    cited = summary["binary_measures"]["cited_decision"]
    assert cited["treatment"]["rate"] == 1.0
    assert cited["control"]["rate"] == 0.0
    assert cited["fisher_one_sided_p"] < 0.05
    # A rate never travels alone, and the interval is not degenerate at 5/5.
    assert cited["treatment"]["wilson_95"][0] < 1.0
    assert cited["control"]["wilson_95"][1] > 0.0


def test_run_repeat_does_not_call_a_suggestive_split_established(
    tmp_path: Path, monkeypatch
) -> None:
    """3/5 vs 0/5 must fail the registered threshold, not squeak past it."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(
            cited_faithful={1: True, 2: True, 3: True, 4: False, 5: False}
        )
    )
    result = _run_repeat(tmp_path, runner=runner)
    summary = oracle._read_json(Path(result["repeat_receipt"]))["summary"]
    cited = summary["binary_measures"]["cited_decision"]
    assert cited["treatment"]["successes"] == 3
    assert cited["fisher_one_sided_p"] > 0.05


def test_run_repeat_alternates_arm_order_so_arm_is_not_confounded_with_position(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(cited_faithful=dict.fromkeys(range(1, 5), True))
    )
    result = _run_repeat(tmp_path, attempts=4, runner=runner)

    receipt = oracle._read_json(Path(result["repeat_receipt"]))
    assert receipt["configuration"]["attempt_arm_order"] == {
        "1": ["control", "treatment"],
        "2": ["treatment", "control"],
        "3": ["control", "treatment"],
        "4": ["treatment", "control"],
    }
    conditions = [
        args[args.index("--condition") + 1] for args, _ in runner.calls if "--condition" in args
    ]
    assert conditions == [
        "no-stele",
        "faithful",
        "faithful",
        "no-stele",
        "no-stele",
        "faithful",
        "faithful",
        "no-stele",
    ]
    # Each arm still recorded which order its own attempt ran in.
    assert [record["arm_order"] for record in receipt["attempts"]] == [
        ["control", "treatment"],
        ["treatment", "control"],
        ["control", "treatment"],
        ["treatment", "control"],
    ]


def test_every_repeat_arm_gets_its_own_run_id_so_no_attempt_inherits_graph_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(cited_faithful=dict.fromkeys(range(1, 4), True))
    )
    _run_repeat(tmp_path, attempts=3, runner=runner)
    run_ids = [
        args[args.index("--run-id") + 1] for args, _ in runner.calls if "--run-id" in args
    ]
    assert len(run_ids) == 6
    assert len(set(run_ids)) == 6


def test_a_repeat_acknowledgement_must_cover_every_arm_in_the_whole_repeat(
    tmp_path: Path, monkeypatch
) -> None:
    """The fence spans the repeat; acknowledging one pair's cap must not run ten arms."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner()
    with pytest.raises(oracle.OracleError, match="10-arm requested cap"):
        _run_repeat(tmp_path, runner=runner, acknowledgement="2.00")
    # Refused before anything ran, so there is nothing to clean up.
    assert runner.calls == []
    assert not (tmp_path / "state" / "runs" / "repeat-001").exists()


def test_a_repeat_excludes_an_incomplete_attempt_and_names_the_reason(
    tmp_path: Path, monkeypatch
) -> None:
    """A repeat that silently analyses 4 of 5 reads exactly like one that ran 4."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        faithful_failure=True,
        scored_receipt=_repeat_receipts(cited_faithful=dict.fromkeys(range(1, 4), True)),
    )
    result = _run_repeat(tmp_path, attempts=3, runner=runner)

    assert result["ok"] is False
    receipt = oracle._read_json(Path(result["repeat_receipt"]))
    summary = receipt["summary"]
    assert receipt["comparable"] is False
    assert summary["attempts_analysed"] == 0
    assert [record["reason"] for record in summary["attempts_excluded"]] == [
        "arm_did_not_complete"
    ] * 3
    assert summary["binary_measures"]["cited_decision"] is None


def test_a_repeat_flags_an_arm_that_ran_up_against_its_own_budget_ceiling(
    tmp_path: Path, monkeypatch
) -> None:
    """A truncated answer scores like a failure to recall for a non-memory reason.

    The ceiling for a two-step challenge is the per-trial cap times the billed
    steps ($1.00 x 2 = $2.00), so the warning line is $1.80 — NOT $0.90. An arm
    at $1.20 is entirely normal and must not be flagged; that false positive is
    exactly what a per-arm denominator produced on the real drift-r1 receipts.
    """

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(
            cited_faithful={1: True, 2: True},
            cost={1: (1.20, 0.10), 2: (1.90, 0.10)},
        )
    )
    result = _run_repeat(tmp_path, attempts=2, runner=runner)
    attempts = oracle._read_json(Path(result["repeat_receipt"]))["attempts"]
    assert attempts[0]["budget_truncation_risk"] == []
    assert attempts[1]["budget_truncation_risk"] == [
        {
            "arm": "control",
            "claude_reported_usd": 1.90,
            "effective_cap_usd": "2.0",
            "exceeded_requested_cap": False,
            "above_warning_threshold": True,
        }
    ]


def test_a_repeat_trusts_the_runners_own_cap_verdict_over_the_warning_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    """A cheap arm that still hit its cap is a truncation the 90% rule misses."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    factory = _repeat_receipts(cited_faithful={1: True}, cost={1: (0.10, 0.10)})

    def exceeded(run_id: str, condition: str) -> dict:
        receipt = factory(run_id, condition)
        if condition == "faithful":
            receipt["results"][0]["cost"]["claude_report_exceeded_requested_cap"] = True
        return receipt

    runner = FakeRunner(scored_receipt=exceeded)
    result = _run_repeat(tmp_path, attempts=1, runner=runner)
    risk = oracle._read_json(Path(result["repeat_receipt"]))["attempts"][0][
        "budget_truncation_risk"
    ]
    assert risk == [
        {
            "arm": "treatment",
            "claude_reported_usd": 0.10,
            "effective_cap_usd": "2.0",
            "exceeded_requested_cap": True,
            "above_warning_threshold": False,
        }
    ]


def test_a_repeat_excludes_an_attempt_whose_arm_the_runner_marked_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    """drift-r1 analysed an attempt whose control came back `outcome: invalid`.

    The pre-registration promised to exclude those; the code only checked exit
    codes, so an arm the apparatus itself disowned was quietly counted.
    """

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    factory = _repeat_receipts(cited_faithful={1: True, 2: True})

    def with_invalid_control(run_id: str, condition: str) -> dict:
        receipt = factory(run_id, condition)
        if condition == "no-stele" and "attempt-02" in run_id:
            receipt["outcome"] = "invalid"
        return receipt

    runner = FakeRunner(scored_receipt=with_invalid_control)
    result = _run_repeat(tmp_path, attempts=2, runner=runner)
    receipt = oracle._read_json(Path(result["repeat_receipt"]))
    summary = receipt["summary"]

    assert summary["attempts_analysed"] == 1
    assert summary["attempts_excluded"] == [
        {"attempt": 2, "reason": "arm_outcome_invalid:control"}
    ]
    assert receipt["comparable"] is False
    assert summary["binary_measures"]["cited_decision"]["treatment"]["trials"] == 1


def test_a_repeat_excludes_an_attempt_whose_arm_ended_in_an_infrastructure_error(
    tmp_path: Path, monkeypatch
) -> None:
    """A clean exit can still carry an infrastructure error.

    Claude Code aborts with OutputTokenExceededError when a turn passes the
    output cap; the verifier then scores an unfinished workspace as 0, which
    would be analysed as a genuine failure.
    """

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    factory = _repeat_receipts(cited_faithful={1: True, 2: True})

    def with_broken_treatment(run_id: str, condition: str) -> dict:
        receipt = factory(run_id, condition)
        if condition == "faithful" and "attempt-02" in run_id:
            receipt["outcome"] = "infrastructure-error"
        return receipt

    runner = FakeRunner(scored_receipt=with_broken_treatment)
    result = _run_repeat(tmp_path, attempts=2, runner=runner)
    summary = oracle._read_json(Path(result["repeat_receipt"]))["summary"]

    assert summary["attempts_analysed"] == 1
    assert summary["attempts_excluded"] == [
        {"attempt": 2, "reason": "arm_infrastructure_error:treatment"}
    ]


def test_a_repeat_reports_a_continuous_measure_as_a_paired_delta_with_no_p_value(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(
            cited_faithful=dict.fromkeys(range(1, 4), True),
            investigative={1: (12.0, 8.0), 2: (10.0, 9.0), 3: (11.0, 13.0)},
        )
    )
    result = _run_repeat(tmp_path, attempts=3, runner=runner)
    paired = oracle._read_json(Path(result["repeat_receipt"]))["summary"]["paired_measures"]
    depth = paired["investigative_tool_calls"]
    assert depth["differences"] == [-4.0, -1.0, 2.0]
    assert depth["attempts_favouring_treatment"] == 2
    assert depth["test"] is None


def test_a_partial_reward_is_not_counted_as_a_pass(tmp_path: Path, monkeypatch) -> None:
    """`bool(0.6)` is True; a 0.6 reward is not."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(
            cited_faithful={1: True, 2: True},
            reward={1: (0.6, 1.0), 2: (1.0, 1.0)},
        )
    )
    result = _run_repeat(tmp_path, attempts=2, runner=runner)
    reward = oracle._read_json(Path(result["repeat_receipt"]))["summary"]["binary_measures"][
        "reward"
    ]
    assert reward["control"]["successes"] == 1
    assert reward["treatment"]["successes"] == 2


def test_a_repeat_receipt_is_inspectable_and_billable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(cited_faithful=dict.fromkeys(range(1, 3), True))
    )
    _run_repeat(tmp_path, attempts=2, runner=runner)
    state = tmp_path / "state"

    inspected = oracle.inspect_pair("repeat-001", state_root=state)
    assert inspected["status"] == "completed"
    assert inspected["summary"]["attempts_analysed"] == 2
    assert len(inspected["attempts"]) == 2

    billed = oracle.record_billing("repeat-001", "1.75", state_root=state)
    assert billed["ok"] is True
    receipt = oracle._read_json(state / "runs" / "repeat-001" / "repeat.json")
    assert receipt["billing"]["gateway_total_usd"] == "1.75"


def test_cleaning_a_repeat_cleans_every_attempt_not_just_control_and_faithful(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(cited_faithful=dict.fromkeys(range(1, 3), True))
    )
    _run_repeat(tmp_path, attempts=2, runner=runner)

    cleaner = FakeRunner()
    oracle.clean_pair("repeat-001", state_root=tmp_path / "state", runner=cleaner)
    cleaned = [
        json.loads(kwargs["input_text"])["run_id"]
        for args, kwargs in cleaner.calls
        if kwargs.get("input_text")
    ]
    assert sorted(cleaned) == sorted(
        [
            "repeat-001",
            "repeat-001-attempt-01-control",
            "repeat-001-attempt-01-treatment",
            "repeat-001-attempt-02-control",
            "repeat-001-attempt-02-treatment",
        ]
    )


COHORT_ROOT = (
    oracle.REPOSITORY_ROOT / "eval" / "stele-bench" / "fixtures" / "session-boundary-recall-v1"
)
SESSION_CHALLENGE = "session-boundary-recall"


def test_run_quad_runs_one_shared_control_and_all_variants_in_one_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    runner = FakeRunner()
    result = oracle.run_quad(
        quad_id="quad-001",
        cohort_root=COHORT_ROOT,
        challenge=SESSION_CHALLENGE,
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        max_budget_usd="1.00",
        acknowledge_paid_quad_cap_usd="4.00",  # 1.00 * (1 control + 3 variants)
        reserve_checker=FakeReserveChecker(),
        drain_checker=FakeDrainChecker(),
        state_root=tmp_path / "state",
        runner=runner,
    )
    receipt = oracle._read_json(Path(result["quad_receipt"]))

    assert result["ok"] is True
    assert receipt["status"] == "completed"
    assert receipt["comparable"] is True
    # Four arms: one control plus one faithful arm per variant.
    assert set(receipt["arms"]) == {"no-stele", "correct-anchor", "wrong-anchor", "no-anchor"}
    assert all(receipt["arms"][label]["exit_code"] == 0 for label in receipt["arms"])
    # The control ran exactly once, shared across the variants.
    conditions = [
        args[args.index("--condition") + 1]
        for args, _ in runner.calls
        if "--condition" in args and args[1] == os.fspath(oracle.RUNNER)
    ]
    assert conditions == ["no-stele", "faithful", "faithful", "faithful"]
    # A delta versus the shared control for every variant.
    assert set(receipt["deltas"]) == {"correct-anchor", "wrong-anchor", "no-anchor"}
    assert all(receipt["deltas"][v] is not None for v in receipt["deltas"])
    # Certification recorded per variant; memory_evidence is lifted from each
    # faithful arm's receipt (the runner decides injection, the quad surfaces it).
    assert set(receipt["provenance"]["certification"]["by_variant"]) == set(receipt["deltas"])
    for variant in ("correct-anchor", "wrong-anchor", "no-anchor"):
        assert receipt["memory_evidence"][variant]["injected"] is True
        assert receipt["memory_evidence"][variant]["channels"] == ["nudge:recall-file", "rag"]
    assert set(receipt["billing"]["claude_reported_usd_by_condition"]) == set(receipt["arms"])
    assert "gateway-secret" not in Path(result["quad_receipt"]).read_text()


def test_run_quad_requires_the_exact_four_arm_cap_before_any_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    runner = FakeRunner()
    with pytest.raises(oracle.OracleError, match="4-arm requested cap"):
        oracle.run_quad(
            quad_id="quad-badcap",
            cohort_root=COHORT_ROOT,
            challenge=SESSION_CHALLENGE,
            source=source,
            artifact_manifest=_artifact_manifest(tmp_path),
            max_budget_usd="1.00",
            acknowledge_paid_quad_cap_usd="2.00",  # should be 4.00
            reserve_checker=FakeReserveChecker(),
            drain_checker=FakeDrainChecker(),
            state_root=tmp_path / "state",
            runner=runner,
        )
    assert runner.calls == []
    assert not (tmp_path / "state" / "runs" / "quad-badcap").exists()


def test_run_quad_refuses_to_start_when_a_variant_fixture_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    with pytest.raises(oracle.OracleError, match="missing the 'ghost-anchor' variant"):
        oracle.run_quad(
            quad_id="quad-missing",
            cohort_root=COHORT_ROOT,
            challenge=SESSION_CHALLENGE,
            variants=("correct-anchor", "ghost-anchor"),
            source=source,
            artifact_manifest=_artifact_manifest(tmp_path),
            max_budget_usd="1.00",
            acknowledge_paid_quad_cap_usd="3.00",  # 1.00 * (1 + 2)
            reserve_checker=FakeReserveChecker(),
            drain_checker=FakeDrainChecker(),
            state_root=tmp_path / "state",
            runner=FakeRunner(),
        )
    assert not (tmp_path / "state" / "runs" / "quad-missing").exists()


def test_run_quad_stops_before_the_next_variant_when_the_balance_drains(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    runner = FakeRunner()
    result = oracle.run_quad(
        quad_id="quad-drained",
        cohort_root=COHORT_ROOT,
        challenge=SESSION_CHALLENGE,
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        max_budget_usd="1.00",
        acknowledge_paid_quad_cap_usd="4.00",
        reserve_checker=FakeReserveChecker(),
        drain_checker=FakeDrainChecker(drained_usd="20.00", tripped=True),
        state_root=tmp_path / "state",
        runner=runner,
    )
    receipt = oracle._read_json(Path(result["quad_receipt"]))

    assert result["ok"] is False
    assert receipt["status"] == "failed"
    assert receipt["error"] == "gateway_drain_tripped"
    # Control ran; the drain check fires before the first faithful arm, so no
    # variant is started.
    assert receipt["arms"]["no-stele"]["exit_code"] == 0
    assert receipt["arms"]["correct-anchor"]["error"] == "gateway_drain_tripped"
    conditions = [
        args[args.index("--condition") + 1]
        for args, _ in runner.calls
        if "--condition" in args and args[1] == os.fspath(oracle.RUNNER)
    ]
    assert conditions == ["no-stele"]
    assert "dropped 20.00 USD" in receipt["billing"]["gateway_drain_stop"]


def test_a_paid_pair_refuses_to_start_when_shared_gateway_headroom_is_unverified(
    tmp_path: Path, monkeypatch
) -> None:
    """A per-pair cap bounds this run; it says nothing about production's float."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    checker = FakeReserveChecker(
        failure="shared Gateway balance 55.00 USD leaves 5.00 USD above the 50.00 USD reserve"
    )
    state_root = tmp_path / "state"
    with pytest.raises(oracle.OracleError, match="does not cover|leaves 5.00"):
        oracle.run_pair(
            pair_id="pair-no-headroom",
            source=source,
            artifact_manifest=_artifact_manifest(tmp_path),
            acknowledge_paid_pair_cap_usd="10.00",
            reserve_checker=checker,
            state_root=state_root,
            runner=FakeRunner(),
        )
    assert not (state_root / "runs" / "pair-no-headroom").exists()
    # The fence was asked about the exact acknowledged two-arm ceiling.
    assert checker.calls == [Decimal("10.00")]


def test_the_pair_receipt_records_the_reserve_preflight_without_the_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    result = oracle.run_pair(
        pair_id="pair-reserve",
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        acknowledge_paid_pair_cap_usd="10.00",
        reserve_checker=FakeReserveChecker(),
        drain_checker=FakeDrainChecker(),
        state_root=tmp_path / "state",
        runner=FakeRunner(),
    )
    receipt_path = Path(result["pair_receipt"])
    preflight = oracle._read_json(receipt_path)["billing"]["gateway_reserve_preflight"]
    assert preflight["shared_balance_usd"] == "120.00"
    assert preflight["production_reserve_usd"] == "50.00"
    assert preflight["acknowledged_pair_cap_usd"] == "10.00"
    assert preflight["calibration_authority"] == "vercel-dashboard"
    assert "gateway-secret" not in receipt_path.read_text()


def test_the_second_arm_is_not_started_when_the_balance_drains_past_the_ceiling(
    tmp_path: Path, monkeypatch
) -> None:
    """Staying under its own budget is exactly what the 2026-07-12 incident did."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    runner = FakeRunner()
    result = oracle.run_pair(
        pair_id="pair-drained",
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        acknowledge_paid_pair_cap_usd="10.00",
        reserve_checker=FakeReserveChecker(),
        drain_checker=FakeDrainChecker(drained_usd="20.00", tripped=True),
        state_root=tmp_path / "state",
        runner=runner,
    )
    receipt = oracle._read_json(Path(result["pair_receipt"]))

    assert result["ok"] is False
    assert receipt["status"] == "failed"
    assert receipt["error"] == "gateway_drain_tripped"
    # The control arm ran and is retained; the faithful arm never started.
    assert receipt["arms"]["control"]["exit_code"] == 0
    assert receipt["arms"]["faithful"]["error"] == "gateway_drain_tripped"
    conditions = [
        args[args.index("--condition") + 1]
        for args, _ in runner.calls
        if "--condition" in args and args[1] == os.fspath(oracle.RUNNER)
    ]
    assert conditions == ["no-stele"]
    assert "dropped 20.00 USD" in receipt["billing"]["gateway_drain_stop"]
    assert receipt["billing"]["gateway_drain_checks"][0]["drained_usd"] == "20.00"
    assert receipt["comparable"] is False


def test_a_healthy_pair_records_its_between_arm_drain_observation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    drain = FakeDrainChecker()
    result = oracle.run_pair(
        pair_id="pair-drain-ok",
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        acknowledge_paid_pair_cap_usd="10.00",
        reserve_checker=FakeReserveChecker(),
        drain_checker=drain,
        state_root=tmp_path / "state",
        runner=FakeRunner(),
    )
    receipt = oracle._read_json(Path(result["pair_receipt"]))

    assert result["ok"] is True
    # Checked once, between the two arms — not before the first, which the
    # preflight already covered.
    assert len(drain.calls) == 1
    assert drain.calls[0]["shared_balance_usd"] == "120.00"
    assert receipt["billing"]["gateway_drain_checks"] == [
        {
            "checked_at": "2026-07-23T00:05:00Z",
            "observed": True,
            "reason": None,
            "balance_usd": "118.50",
            "drained_usd": "1.50",
        }
    ]
    assert receipt["billing"]["gateway_drain_stop"] is None


def test_doctor_reports_the_fence_zero_cost_and_still_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    checker = FakeReserveChecker()
    result = oracle.doctor(
        source=source,
        state_root=tmp_path / "state",
        runner=FakeRunner(),
        reserve_checker=checker,
    )
    # Zero cap: readiness only reports the state, but it still verifies identity,
    # balance, budget and calibration.
    assert checker.calls == [Decimal("0")]
    assert result["checks"]["gateway_reserve"]["headroom_after_reserve_usd"] == "70.00"

    with pytest.raises(oracle.OracleError, match="not the configured dedicated eval key"):
        oracle.doctor(
            source=source,
            state_root=tmp_path / "state",
            runner=FakeRunner(),
            reserve_checker=FakeReserveChecker(
                failure="the exported AI_GATEWAY_KEY_EVAL is not the configured dedicated eval key"
            ),
        )


def test_a_paid_pair_refuses_to_start_when_certification_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Leakage must stop the run before any state exists or any token is spent."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    runner = FakeRunner(certification_failure=True)
    state_root = tmp_path / "state"
    with pytest.raises(oracle.OracleError, match="failed certification"):
        oracle.run_pair(
            pair_id="pair-uncertified",
            source=source,
            artifact_manifest=_artifact_manifest(tmp_path),
            acknowledge_paid_pair_cap_usd="10.00",
            reserve_checker=FakeReserveChecker(),
            drain_checker=FakeDrainChecker(),
            state_root=state_root,
            runner=runner,
        )
    assert not (state_root / "runs" / "pair-uncertified").exists()
    invoked = [args[0] for args, _ in runner.calls]
    assert os.fspath(oracle.RUNNER) not in [
        args[1] for args, _ in runner.calls if len(args) > 1
    ]
    assert invoked  # the gate ran; it did not silently pass


def test_certification_failure_also_blocks_the_zero_model_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("AI_GATEWAY_KEY_EVAL", raising=False)
    source = tmp_path / "dotstate"
    source.mkdir()
    with pytest.raises(oracle.OracleError, match="failed certification"):
        oracle.preflight(
            run_id="preflight-uncertified",
            source=source,
            artifact_manifest=_artifact_manifest(tmp_path),
            state_root=tmp_path / "state",
            runner=FakeRunner(certification_failure=True),
        )


def test_preflight_runs_matched_install_only_pair_without_gateway_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("AI_GATEWAY_KEY_EVAL", raising=False)
    source = tmp_path / "dotstate"
    source.mkdir()
    runner = FakeRunner()
    result = oracle.preflight(
        run_id="preflight-001",
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        state_root=tmp_path / "state",
        runner=runner,
    )
    assert result["ok"] is True
    assert result["zero_model"] is True
    assert result["mode"] == "install-only"
    assert Path(result["receipt_path"]).is_file()
    inspected = oracle.inspect_pair("preflight-001", state_root=tmp_path / "state")
    assert inspected["mode"] == "install-only"
    assert inspected["status"] == "completed"
    assert inspected["receipt"]["conditions"][1]["condition"] == "faithful"


def test_preflight_returns_persisted_failure_diagnostics_without_gateway_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("AI_GATEWAY_KEY_EVAL", raising=False)
    source = tmp_path / "dotstate"
    source.mkdir()
    result = oracle.preflight(
        run_id="preflight-failed",
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        state_root=tmp_path / "state",
        runner=FakeRunner(preflight_failure=True),
    )
    assert result["ok"] is False
    assert result["failure"] == {
        "code": "configured_structural_preflight_failed",
        "message": "failed",
    }
    assert oracle._read_json(Path(result["receipt_path"]))["ok"] is False


def test_preflight_persists_diagnostic_when_runner_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("AI_GATEWAY_KEY_EVAL", raising=False)
    source = tmp_path / "dotstate"
    source.mkdir()
    base = FakeRunner()

    def unavailable_runner(argv, **kwargs):
        if len(argv) >= 2 and str(argv[1]) == os.fspath(oracle.INSTALL_ONLY_RUNNER):
            raise oracle.OracleError("command unavailable: install-only")
        return base(argv, **kwargs)

    result = oracle.preflight(
        run_id="preflight-unavailable",
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        state_root=tmp_path / "state",
        runner=unavailable_runner,
    )
    assert result["ok"] is False
    assert result["failure"]["code"] == "install_only_runner_unavailable"
    assert oracle._read_json(Path(result["receipt_path"]))["failure"] == result["failure"]


def test_run_pair_persists_both_arms_when_one_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    runner = FakeRunner(faithful_failure=True)
    result = oracle.run_pair(
        pair_id="pair-failed",
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        acknowledge_paid_pair_cap_usd="10.00",
        reserve_checker=FakeReserveChecker(),
        drain_checker=FakeDrainChecker(),
        state_root=tmp_path / "state",
        runner=runner,
    )
    receipt = oracle._read_json(Path(result["pair_receipt"]))
    assert result["ok"] is False
    assert receipt["arms"]["control"]["receipt"] is not None
    assert receipt["arms"]["faithful"]["receipt"] is not None
    assert receipt["arms"]["faithful"]["exit_code"] == 1
    assert receipt["comparable"] is False
    assert receipt["deltas"] is None


@pytest.mark.parametrize("acknowledgement", (None, "9.99", "not-a-number"))
def test_run_pair_requires_exact_paid_cap_acknowledgement_before_checks_or_calls(
    tmp_path: Path, acknowledgement: str | None
) -> None:
    runner = FakeRunner()

    with pytest.raises(oracle.OracleError, match="paid run"):
        oracle.run_pair(
            pair_id="unacknowledged",
            source=tmp_path / "missing-source",
            artifact_manifest=tmp_path / "missing-artifact.json",
            acknowledge_paid_pair_cap_usd=acknowledgement,
            state_root=tmp_path / "state",
            runner=runner,
        )

    assert runner.calls == []
    assert not (tmp_path / "state").exists()


def test_run_pair_atomically_finalizes_interruption_and_remains_inspectable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    base = FakeRunner()

    def interrupt_faithful(argv, **kwargs):
        args = tuple(str(value) for value in argv)
        if len(args) >= 3 and args[1] == os.fspath(oracle.RUNNER):
            condition = args[args.index("--condition") + 1]
            if condition == "faithful":
                in_progress = oracle._read_json(
                    Path(args[args.index("--receipt-out") + 1]).with_name("pair.json")
                )
                assert in_progress["status"] == "in_progress"
                assert in_progress["arms"]["control"]["exit_code"] == 0
                raise oracle.OracleInterrupted(oracle.signal.SIGTERM)
        return base(argv, **kwargs)

    state = tmp_path / "state"
    with pytest.raises(oracle.OracleInterrupted):
        oracle.run_pair(
            pair_id="pair-interrupted",
            source=source,
            artifact_manifest=_artifact_manifest(tmp_path),
            acknowledge_paid_pair_cap_usd="10.00",
            reserve_checker=FakeReserveChecker(),
            drain_checker=FakeDrainChecker(),
            state_root=state,
            runner=interrupt_faithful,
        )

    receipt = oracle._read_json(state / "runs/pair-interrupted/pair.json")
    assert receipt["status"] == "interrupted"
    assert receipt["error"] == "runner_interrupted"
    assert receipt["completed_at"] is not None
    assert receipt["arms"]["control"]["exit_code"] == 0
    assert receipt["arms"]["faithful"]["error"] == "runner_interrupted"
    inspected = oracle.inspect_pair("pair-interrupted", state_root=state)
    assert inspected["status"] == "interrupted"


@pytest.mark.parametrize(
    "candidate",
    tuple(
        value
        for value in (
            getattr(oracle.signal, "SIGINT", None),
            getattr(oracle.signal, "SIGTERM", None),
            getattr(oracle.signal, "SIGHUP", None),
        )
        if value is not None
    ),
)
def test_controlled_signal_handler_raises_and_restores(candidate: int) -> None:
    previous = oracle.signal.getsignal(candidate)

    with pytest.raises(oracle.OracleInterrupted) as raised:
        with oracle._controlled_termination_signals():
            handler = oracle.signal.getsignal(candidate)
            assert callable(handler)
            handler(candidate, None)

    assert raised.value.signum == candidate
    assert oracle.signal.getsignal(candidate) == previous


def test_record_billing_preserves_claude_reported_costs(tmp_path: Path) -> None:
    root = oracle._state_root(tmp_path / "state")
    path = root / "runs" / "pair" / "pair.json"
    oracle._atomic_json(
        path,
        {
            "billing": {
                "gateway_total_usd": None,
                "source": None,
                "recorded_at": None,
                "claude_reported_usd_by_condition": {"control": 3.0, "faithful": 0.75},
            }
        },
    )
    result = oracle.record_billing("pair", "3.41", state_root=root)
    assert result["billing"]["gateway_total_usd"] == "3.41"
    assert result["billing"]["source"] == "manual-vercel-dashboard"
    assert result["billing"]["claude_reported_usd_by_condition"] == {
        "control": 3.0,
        "faithful": 0.75,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_clean_is_idempotent_and_optionally_deletes_work(tmp_path: Path) -> None:
    root = oracle._state_root(tmp_path / "state")
    work = root / "runs" / "pair" / "work"
    work.mkdir(parents=True)
    (work / "evidence").write_text("retained")
    runner = FakeRunner()
    first = oracle.clean_pair("pair", delete_work=True, state_root=root, runner=runner)
    second = oracle.clean_pair("pair", delete_work=True, state_root=root, runner=runner)
    assert first == {
        "ok": True,
        "pair_id": "pair",
        "attempts": {"control": True, "faithful": True, "preflight": True},
        "work_deleted": True,
    }
    assert second["ok"] is True
    assert second["work_deleted"] is False


def test_clean_deletes_retained_preflight_work(tmp_path: Path) -> None:
    root = oracle._state_root(tmp_path / "state")
    work = root / "preflights" / "preflight" / "work"
    work.mkdir(parents=True)
    (work / "evidence").write_text("retained")
    result = oracle.clean_pair("preflight", delete_work=True, state_root=root, runner=FakeRunner())
    assert result["ok"] is True
    assert result["attempts"]["preflight"] is True
    assert result["work_deleted"] is True
    assert not work.exists()


@pytest.mark.parametrize("value", ["../escape", "has space", "", "x" * 81])
def test_pair_id_rejects_path_traversal(value: str) -> None:
    with pytest.raises(oracle.OracleError):
        oracle._safe_id(value)


# ── Class B: auto-RAG as an arm dimension (TASK-2497) ─────────────────────────


def test_a_repeat_can_pair_two_graph_conditions_for_a_class_b_question(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(
        scored_receipt=_repeat_receipts(cited_faithful=dict.fromkeys(range(1, 3), True))
    )

    result = _run_repeat(
        tmp_path,
        attempts=2,
        runner=runner,
        control_condition="faithful-norag",
        treatment_condition="faithful",
    )

    receipt = oracle._read_json(Path(result["repeat_receipt"]))
    assert receipt["configuration"]["control_condition"] == "faithful-norag"
    assert receipt["configuration"]["treatment_condition"] == "faithful"
    conditions = [
        args[args.index("--condition") + 1] for args, _ in runner.calls if "--condition" in args
    ]
    # Alternating order still holds, now between two graph conditions.
    assert conditions == [
        "faithful-norag",
        "faithful",
        "faithful",
        "faithful-norag",
    ]


def test_both_arms_keep_their_memory_evidence_when_both_have_a_graph(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(scored_receipt=_repeat_receipts(cited_faithful={1: True}))

    result = _run_repeat(
        tmp_path,
        attempts=1,
        runner=runner,
        control_condition="faithful-norag",
        treatment_condition="faithful",
    )

    evidence = oracle._read_json(Path(result["repeat_receipt"]))["attempts"][0][
        "memory_evidence"
    ]
    # Whether the arm WITHOUT injection still reached the memory is the entire
    # Class B measurement, so its evidence has to survive into the receipt.
    assert set(evidence) == {"control", "treatment"}


def test_only_the_graph_arm_carries_memory_evidence_in_a_class_a_pair(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(scored_receipt=_repeat_receipts(cited_faithful={1: True}))

    result = _run_repeat(tmp_path, attempts=1, runner=runner)

    evidence = oracle._read_json(Path(result["repeat_receipt"]))["attempts"][0][
        "memory_evidence"
    ]
    assert set(evidence) == {"treatment"}


def test_a_repeat_refuses_two_identical_arms(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(scored_receipt=_repeat_receipts(cited_faithful={}))

    # This would measure run-to-run variance and report it in a receipt shaped
    # exactly like a comparison against a control.
    with pytest.raises(oracle.OracleError, match="conditions must differ"):
        _run_repeat(
            tmp_path,
            attempts=1,
            runner=runner,
            control_condition="faithful",
            treatment_condition="faithful",
        )


def test_a_repeat_refuses_an_unknown_condition(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(scored_receipt=_repeat_receipts(cited_faithful={}))

    with pytest.raises(oracle.OracleError, match="control condition must be one of"):
        _run_repeat(
            tmp_path,
            attempts=1,
            runner=runner,
            control_condition="no-such-arm",
        )


def test_a_norag_arm_is_launched_with_the_graph_it_needs(tmp_path: Path, monkeypatch) -> None:
    """The norag control still installs Stele — it just gets nothing pushed at it.

    Launching it without the fixture and artifact would make it a `no-stele` arm
    wearing a different name, which is the exact confound the condition exists
    to avoid.
    """

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(scored_receipt=_repeat_receipts(cited_faithful={1: True}))

    _run_repeat(
        tmp_path,
        attempts=1,
        runner=runner,
        control_condition="faithful-norag",
        treatment_condition="faithful",
    )

    norag = next(
        args
        for args, _ in runner.calls
        if "--condition" in args and args[args.index("--condition") + 1] == "faithful-norag"
    )
    assert "--artifact" in norag
    assert "--backend-url" in norag
    assert "--backend-anon-key" in norag
    # The fixture is a bare positional before the flags, exactly as `faithful`
    # receives it.
    assert any(arg.endswith(".json") for arg in norag[: norag.index("--condition")])


def test_a_no_stele_arm_is_still_launched_without_a_graph(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    runner = FakeRunner(scored_receipt=_repeat_receipts(cited_faithful={1: True}))

    _run_repeat(tmp_path, attempts=1, runner=runner)

    control = next(
        args
        for args, _ in runner.calls
        if "--condition" in args and args[args.index("--condition") + 1] == "no-stele"
    )
    assert "--artifact" not in control
    assert "--backend-url" not in control


def _sanitised_dataset(tmp_path: Path, *names: str) -> Path:
    """A minimal borrowed dataset, put through the real sanitiser."""

    import prepare_external_dataset

    source = tmp_path / "borrowed-source"
    source.mkdir(exist_ok=True)
    for name in names:
        task = source / name
        workspace = task / "environment" / "workspace"
        workspace.mkdir(parents=True)
        (task / "task.toml").write_text(f'[task]\nname = "{name}"\n', encoding="utf-8")
        (task / "environment" / "Dockerfile").write_text(
            "FROM buildpack-deps:jammy\nWORKDIR /app\nCOPY workspace/ ./\n", encoding="utf-8"
        )
        (workspace / "solve.py").write_text(f"# {name}\n", encoding="utf-8")
        payload = workspace / ".oracle"
        payload.mkdir()
        (payload / "solution.enc").write_bytes(b"Salted__\x00encrypted")
        meta = task / "tests" / ".meta"
        meta.mkdir(parents=True)
        (meta / "example.py").write_text(f"# reference {name}\n", encoding="utf-8")
        (task / "tests" / "test.sh").write_text(
            "#!/bin/bash\nset -euo pipefail\npytest\n"
            "echo $? > /logs/verifier/reward.txt\n",
            encoding="utf-8",
        )
    destination = tmp_path / "borrowed-clean"
    prepare_external_dataset.sanitise_dataset(source, destination, overwrite=True)
    return destination


def _run_suite(
    tmp_path: Path,
    *,
    runner: FakeRunner,
    dataset: Path,
    tasks: tuple[str, ...] | None = None,
    suite_id: str = "suite-001",
    max_budget_usd: str = "0.50",
    acknowledgement: str | None = None,
    non_inferiority_margin: float = 0.10,
    reserve_checker: FakeReserveChecker | None = None,
) -> dict:
    source = tmp_path / "dotstate"
    source.mkdir(exist_ok=True)
    task_count = len(tasks) if tasks else len(_dataset_task_names(dataset))
    return oracle.run_suite(
        suite_id=suite_id,
        dataset=dataset,
        tasks=tasks,
        non_inferiority_margin=non_inferiority_margin,
        source=source,
        artifact_manifest=_artifact_manifest(tmp_path),
        max_budget_usd=max_budget_usd,
        acknowledge_paid_suite_cap_usd=(
            acknowledgement
            if acknowledgement is not None
            else str(Decimal(max_budget_usd) * 2 * task_count)
        ),
        reserve_checker=reserve_checker or FakeReserveChecker(),
        drain_checker=FakeDrainChecker(),
        state_root=tmp_path / "state",
        runner=runner,
    )


def _dataset_task_names(dataset: Path) -> list[str]:
    return sorted(
        path.name for path in dataset.iterdir() if (path / "task.toml").is_file()
    )


def test_run_suite_pairs_every_task_and_grades_with_the_borrowed_verifier(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker", "two-fer", "bowling")
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(
            task, condition, reward=1.0 if task in ("clock", "poker") else 0.0
        )
    )

    result = _run_suite(tmp_path, runner=runner, dataset=dataset)

    assert result["ok"] is True
    receipt = oracle._read_json(Path(result["suite_receipt"]))
    assert receipt["status"] == "completed"
    assert receipt["comparable"] is True
    summary = receipt["summary"]
    assert summary["attempts_run"] == 4
    assert summary["attempts_analysed"] == 4
    # The sample unit is the task, not a repetition of one question.
    assert summary["sample_unit"] == "task"
    assert summary["solve_rate"]["treatment_rate"] == 0.5
    assert summary["solve_rate"]["control_rate"] == 0.5
    assert summary["solve_rate"]["difference"] == 0.0
    assert summary["memory_uptake"]["treatment"] == {
        "attempts": 4,
        "injected": 4,
        "marker": 0,
    }


def test_run_suite_declares_its_margin_and_refuses_to_overclaim(
    tmp_path: Path, monkeypatch
) -> None:
    """Four tasks cannot establish a 10% margin, however clean the result."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker", "two-fer", "bowling")
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(task, condition, reward=1.0)
    )

    result = _run_suite(tmp_path, runner=runner, dataset=dataset)
    receipt = oracle._read_json(Path(result["suite_receipt"]))

    assert receipt["configuration"]["non_inferiority_margin"] == 0.10
    verdict = receipt["summary"]["non_inferiority"]
    assert verdict["margin"] == 0.10
    assert verdict["verdict"] == "inconclusive"


def test_run_suite_reports_a_real_regression_as_inferior(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    names = tuple(f"task-{index:02d}" for index in range(1, 11))
    dataset = _sanitised_dataset(tmp_path, *names)
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(
            task, condition, reward=0.0 if condition == "stele-fresh" else 1.0
        )
    )

    result = _run_suite(tmp_path, runner=runner, dataset=dataset)
    verdict = oracle._read_json(Path(result["suite_receipt"]))["summary"]["non_inferiority"]
    assert verdict["verdict"] == "inferior"
    assert verdict["difference"] == -1.0


def test_run_suite_alternates_arm_order_across_tasks(tmp_path: Path, monkeypatch) -> None:
    """Being second in the queue must not be confounded with being the treatment."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker", "two-fer", "bowling")
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(task, condition, reward=1.0)
    )

    result = _run_suite(tmp_path, runner=runner, dataset=dataset)
    receipt = oracle._read_json(Path(result["suite_receipt"]))
    assert receipt["configuration"]["task_arm_order"] == {
        "1": ["control", "treatment"],
        "2": ["treatment", "control"],
        "3": ["control", "treatment"],
        "4": ["treatment", "control"],
    }
    launched = [
        (args[3], args[args.index("--condition") + 1])
        for args, _ in runner.calls
        if len(args) >= 3 and args[1] == os.fspath(oracle.BORROWED_RUNNER)
    ]
    tasks = receipt["configuration"]["tasks"]
    assert launched[:2] == [(tasks[0], "no-stele"), (tasks[0], "stele-fresh")]
    assert launched[2:4] == [(tasks[1], "stele-fresh"), (tasks[1], "no-stele")]


def test_run_suite_gives_every_task_its_own_fresh_project(
    tmp_path: Path, monkeypatch
) -> None:
    """One project per task: no task inherits what the last one wrote."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker")
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(task, condition, reward=1.0)
    )

    _run_suite(tmp_path, runner=runner, dataset=dataset)

    run_ids = [
        args[args.index("--run-id") + 1]
        for args, _ in runner.calls
        if len(args) >= 3 and args[1] == os.fspath(oracle.BORROWED_RUNNER)
    ]
    assert len(run_ids) == len(set(run_ids)) == 4


def test_run_suite_records_what_the_agent_actually_left_in_the_graph(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker")
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(
            task,
            condition,
            reward=1.0,
            agent_nodes={"knowledge": 2, "task": 1} if task == "clock" else {},
        )
    )

    result = _run_suite(tmp_path, runner=runner, dataset=dataset)
    attempts = oracle._read_json(Path(result["suite_receipt"]))["attempts"]
    by_task = {record["task"]: record["graph_uptake"] for record in attempts}

    assert by_task["clock"]["agent_nodes_total"] == 3
    assert by_task["clock"]["agent_tasks"]["completed"] == 1
    # An agent that wrote nothing is a result, not a gap in the record.
    assert by_task["poker"]["agent_nodes_total"] == 0
    assert by_task["poker"]["observed"] is True


def test_run_suite_drops_a_task_whose_arm_reached_the_answer(
    tmp_path: Path, monkeypatch
) -> None:
    """A cheat excludes the whole pair; it is never scored on one honest arm."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker", "two-fer", "bowling")
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(
            task,
            condition,
            reward=1.0,
            outcome="invalid" if task == "poker" and condition == "stele-fresh" else None,
        )
    )

    result = _run_suite(tmp_path, runner=runner, dataset=dataset)
    receipt = oracle._read_json(Path(result["suite_receipt"]))
    summary = receipt["summary"]

    poker = receipt["configuration"]["tasks"].index("poker") + 1
    assert summary["attempts_run"] == 4
    assert summary["attempts_analysed"] == 3
    assert receipt["comparable"] is False
    assert summary["attempts_excluded"] == [
        {"attempt": poker, "reason": "arm_outcome_invalid:treatment"}
    ]
    assert summary["solve_rate"]["pairs"] == 3


def test_run_suite_refuses_a_dataset_that_was_never_sanitised(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    raw = tmp_path / "raw"
    (raw / "clock").mkdir(parents=True)
    (raw / "clock" / "task.toml").write_text("[task]\n", encoding="utf-8")

    with pytest.raises(oracle.OracleError, match="not safe to run"):
        _run_suite(tmp_path, runner=FakeRunner(), dataset=raw)


def test_run_suite_refuses_before_spending_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch
) -> None:
    """A refusal has to happen before any run state exists to clean up."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock")
    dataset_restored = dataset / "clock" / "environment" / "workspace" / ".oracle"
    dataset_restored.mkdir()
    (dataset_restored / "solution.enc").write_bytes(b"Salted__\x00encrypted")

    with pytest.raises(oracle.OracleError, match="survived sanitisation"):
        _run_suite(tmp_path, runner=FakeRunner(), dataset=dataset)
    assert not (tmp_path / "state" / "runs" / "suite-001").exists()


def test_run_suite_refuses_a_task_the_dataset_does_not_have(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock")

    with pytest.raises(oracle.OracleError, match="no such tasks: poker"):
        _run_suite(tmp_path, runner=FakeRunner(), dataset=dataset, tasks=("clock", "poker"))


def test_run_suite_acknowledgement_must_match_the_whole_suite_cap(
    tmp_path: Path, monkeypatch
) -> None:
    """The cap scales with the task count, which is where a suite gets expensive."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker", "two-fer")

    with pytest.raises(oracle.OracleError, match="3.00 USD"):
        _run_suite(
            tmp_path,
            runner=FakeRunner(),
            dataset=dataset,
            max_budget_usd="0.50",
            acknowledgement="1.00",
        )


def test_run_suite_reserve_sees_the_whole_suite_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker", "two-fer")
    reserve = FakeReserveChecker()
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(task, condition, reward=1.0)
    )

    _run_suite(tmp_path, runner=runner, dataset=dataset, reserve_checker=reserve)
    assert reserve.calls == [Decimal("3.00")]


def test_run_suite_records_the_borrowed_inputs_by_content(
    tmp_path: Path, monkeypatch
) -> None:
    """We do not own the tasks or the verifier, so both are pinned by digest."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker")
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(task, condition, reward=1.0)
    )

    result = _run_suite(tmp_path, runner=runner, dataset=dataset)
    provenance = oracle._read_json(Path(result["suite_receipt"]))["provenance"]

    assert len(provenance["challenge_manifest_sha256"]) == 64
    assert len(provenance["verifier_sha256"]) == 64
    assert provenance["challenge_manifest_sha256"] != provenance["verifier_sha256"]
    assert provenance["dataset"]["tasks"] == ["clock", "poker"]


def test_run_suite_conditions_must_differ(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock")
    with pytest.raises(oracle.OracleError, match="must differ"):
        oracle.run_suite(
            suite_id="suite-001",
            dataset=dataset,
            control_condition="no-stele",
            treatment_condition="no-stele",
            acknowledge_paid_suite_cap_usd="1.00",
            state_root=tmp_path / "state",
            runner=FakeRunner(),
        )


@pytest.mark.parametrize("margin", [0.0, 1.0, -0.5])
def test_run_suite_margin_must_be_a_rate(tmp_path: Path, monkeypatch, margin: float) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock")
    with pytest.raises(oracle.OracleError, match="margin must be a rate"):
        _run_suite(
            tmp_path, runner=FakeRunner(), dataset=dataset, non_inferiority_margin=margin
        )


def test_run_suite_reports_network_access_per_arm_without_dropping_trials(
    tmp_path: Path, monkeypatch
) -> None:
    """The gap between arms is the signal; a filter would decide for us."""

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker")

    def receipt(task: str, condition: str) -> dict:
        built = _borrowed_receipt(task, condition, reward=1.0)
        built["results"][0]["evidence"]["external_source_access_detected"] = (
            condition == "stele-fresh"
        )
        return built

    result = _run_suite(tmp_path, runner=FakeRunner(borrowed_receipt=receipt), dataset=dataset)
    summary = oracle._read_json(Path(result["suite_receipt"]))["summary"]

    assert summary["external_source_access"] == {
        "trials": 2,
        "control": 0,
        "treatment": 2,
    }
    # Recorded, not excluded — both tasks still count.
    assert summary["attempts_analysed"] == 2


def test_run_suite_identifies_its_corpus_by_the_dataset_not_an_unrelated_repo(
    tmp_path: Path, monkeypatch
) -> None:
    """A borrowed run builds nothing from our corpus; claiming one would mislead.

    The dataset's sanitisation digest is stable across builds — unlike the
    artifact, which is the variable under test — so it identifies the corpus
    without turning every build into its own trend line.
    """

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    dataset = _sanitised_dataset(tmp_path, "clock", "poker")
    runner = FakeRunner(
        borrowed_receipt=lambda task, condition: _borrowed_receipt(task, condition, reward=1.0)
    )

    result = _run_suite(tmp_path, runner=runner, dataset=dataset)
    receipt = oracle._read_json(Path(result["suite_receipt"]))
    manifest_digest = receipt["provenance"]["dataset"]["sanitisation_manifest_sha256"]

    assert receipt["configuration"]["source_commit"] == manifest_digest
    assert receipt["configuration"]["source_commit"] != "f" * 40
    # And no checkout of the unrelated corpus was ever consulted.
    assert not any(
        args[:2] == ("git", "rev-parse") for args, _ in runner.calls
    )


def test_doctor_on_the_anthropic_provider_checks_its_own_fence(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    monkeypatch.setenv("STELE_BENCH_MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY_EVAL", raising=False)
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY_EVAL", "admin-secret")
    source = tmp_path / "dotstate"
    source.mkdir()
    with pytest.raises(oracle.OracleError, match="ANTHROPIC_API_KEY_EVAL is not exported"):
        oracle.doctor(
            source=source,
            state_root=tmp_path / "state",
            runner=FakeRunner(),
            reserve_checker=FakeReserveChecker(),
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY_EVAL", "direct-secret")
    seen: list[Path] = []

    def checker(*, acknowledged_pair_cap_usd, config_path):
        seen.append(config_path)
        return {"model_provider": "anthropic", "acknowledged_pair_cap_usd": "0.00"}

    result = oracle.doctor(
        source=source, state_root=tmp_path / "state", runner=FakeRunner(), reserve_checker=checker
    )
    assert result["checks"]["model_provider"] == "anthropic"
    assert result["checks"]["anthropic_reserve"]["model_provider"] == "anthropic"
    assert "gateway_reserve" not in result["checks"]
    assert seen == [tmp_path / "state" / "anthropic-reserve.json"]
    assert "direct-secret" not in json.dumps(result)


def test_the_default_fence_follows_the_selected_provider(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        oracle.gateway_reserve, "check_gateway_reserve", lambda **_: calls.append("gateway")
    )
    monkeypatch.setattr(
        oracle.anthropic_reserve, "check_anthropic_reserve", lambda **_: calls.append("anthropic")
    )
    monkeypatch.setattr(
        oracle.anthropic_reserve, "check_anthropic_drain", lambda **_: calls.append("drain")
    )
    oracle._provider_reserve_check(acknowledged_pair_cap_usd=Decimal(1), config_path=Path("x"))
    monkeypatch.setenv("STELE_BENCH_MODEL_PROVIDER", "anthropic")
    oracle._provider_reserve_check(acknowledged_pair_cap_usd=Decimal(1), config_path=Path("x"))
    oracle._provider_drain_check(preflight={})
    assert calls == ["gateway", "anthropic", "drain"]


def test_compounding_runs_refuse_the_anthropic_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STELE_BENCH_MODEL_PROVIDER", "anthropic")
    with pytest.raises(oracle.OracleError, match="require the gateway model provider"):
        oracle.run_compounding(
            run_id="compounding-001",
            admission=True,
            attempts=1,
            state_root=tmp_path / "state",
            runner=FakeRunner(),
            reserve_checker=FakeReserveChecker(),
        )
    assert not (tmp_path / "state").exists()


def test_the_cli_refuses_a_model_the_direct_provider_cannot_run(tmp_path: Path, capsys) -> None:
    status = oracle.main(
        [
            "--state-root",
            os.fspath(tmp_path / "state"),
            "--model-provider",
            "anthropic",
            "run-pair",
            "pair-001",
            "--model",
            "openai/gpt-5",
            "--acknowledge-paid-pair-cap-usd",
            "10.00",
        ]
    )
    assert status == 1
    assert "cannot run model" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()
