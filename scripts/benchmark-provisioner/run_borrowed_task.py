#!/usr/bin/env python3
"""Run one budget-capped arm against a task borrowed from another benchmark.

The scored runner next door asks *our* question: a challenge we wrote, graded by
a verifier we wrote, against a graph we froze. That is the right shape for
measuring whether memory helps, and the wrong shape for measuring what memory
*costs*, because a question we wrote is a question we could have written to
favour the thing under test.

So this runner gives up authorship. The task, the workspace and the verifier all
come from a dataset someone else built for a different purpose, and the only
thing we contribute is the agent configuration. Two differences follow from that
and drive everything below:

**There is no fixture.** A borrowed task has no memory intent, so there is no
prior knowledge that could help and nothing to restore. The treatment arm makes
its project in-container the way a new user does (`stele project create`), which
means the graph it starts from is whatever a real day-one project seeds — not a
hand-built approximation of one. What the agent then writes into it is read back
from the database afterwards, because a trajectory shows what was attempted and
only the graph shows what stuck.

**Grading is theirs, not ours.** The verifier ships with the task. We do not
inspect it, wrap it, or add a mutation audit on top: the borrowed benchmark's
notion of "solved" is the whole point of borrowing it, and a verdict we adjusted
is a verdict we authored.

What we *do* add is a refusal. A trial whose trajectory touches the reference
solution is excluded rather than scored — the dataset is sanitised before it
gets here, so a reference to that payload means either the sanitiser missed a
task or the model invented the path, and neither produces a number worth
keeping.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC_SOURCE = _REPOSITORY_ROOT / "eval" / "stele-bench" / "src"
sys.path.insert(0, os.fspath(_PUBLIC_SOURCE))
sys.path.insert(0, os.fspath(Path(__file__).resolve().parent))

import run_install_only_smoke as support  # noqa: E402
import run_scored_smoke as scored  # noqa: E402
from prepare_external_dataset import (  # noqa: E402
    FORBIDDEN_NAMES,
    FORBIDDEN_TEXT,
    MANIFEST_NAME,
    WORKSPACE_DIRECTORY,
    _tree_digest,
)
from semantic_bridge import SemanticBridgeConfig, start_semantic_bridge  # noqa: E402

from stele_bench.harbor_entrypoint import HARBOR_AGENT_IMPORT_PATH  # noqa: E402
from stele_bench.preflight import CREATED_PROJECT_REPORT_NAME  # noqa: E402

GATEWAY_URL = scored.GATEWAY_URL
DEFAULT_MODEL = scored.DEFAULT_MODEL
ScoredSmokeError = scored.ScoredSmokeError

# The treatment arm creates its own project rather than restoring one, so the
# condition name says so: this is not `faithful` with a different fixture, it is
# a different provisioning mode and must never be pooled with one.
Condition = Literal["no-stele", "stele-fresh"]
_CONDITIONS: tuple[Condition, ...] = ("no-stele", "stele-fresh")
_GRAPH_CONDITIONS: tuple[Condition, ...] = ("stele-fresh",)

_JOB_NAME = "borrowed-task"
_CONFIG_NAME = "borrowed-task.json"
# The name the container gives its project. Deliberately generic: it is visible
# to the agent, and a name that says "benchmark" tells it what it is inside.
DEFAULT_PROJECT_NAME = "Workspace"

_SAFE_TASK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Harbor's default agent-setup ceiling is 360s, and on a borrowed image that is
# not enough: the stock `claude-code` install alone (apt-get, then downloading
# the Claude Code bootstrap) has been measured over it, dropping trials from
# BOTH arms with `AgentSetupTimeoutError`.
#
# Raising it is not a convenience — it is a bias fix. The treatment arm's setup
# does strictly more work (upload a ~96 MB artifact, install the plugin, create
# a project, run the structural preflight), so a ceiling either arm can graze
# will be crossed by the treatment arm more often. Trials would then vanish
# asymmetrically, and a paired comparison that silently loses more treatment
# trials than control trials is not a comparison.
#
# So: the SAME generous ceiling on both arms. Setup runs no model, so a high
# ceiling costs wall clock and nothing else.
AGENT_SETUP_TIMEOUT_SECONDS = 1800.0
# Tool names the Stele MCP server exposes. Counting these separates "the agent
# spent turns on memory" from "the agent spent turns on the exercise", which is
# the overhead this experiment exists to price.
_STELE_TOOL_PREFIXES = ("mcp__stele__", "mcp__plugin_stele_stele__", "stele__")

# Reaching the reference solution. The names come from the sanitiser so there is
# one definition of what the payload is called; the command pattern is here
# because a trajectory can name the act without naming the file.
_ORACLE_COMMAND = re.compile(r"openssl\s+enc\b[^\n]*?\s-d\b", re.IGNORECASE)


@dataclass(frozen=True)
class BorrowedInputs:
    """Everything one arm of one borrowed task needs, and nothing more."""

    dataset: Path
    task_name: str
    attempt_root: Path
    work_root: Path
    run_id: str
    condition: Condition = "no-stele"
    provisioner_executable: Path | None = None
    artifact_path: Path | None = None
    artifact_sha256: str | None = None
    backend_url: str | None = None
    backend_anon_key: str | None = None
    artifact_source_kind: Literal["release", "local-dirty-worktree"] = "local-dirty-worktree"
    artifact_release_version: str | None = None
    project_name: str = DEFAULT_PROJECT_NAME
    harbor_executable: str = "harbor"
    model: str = DEFAULT_MODEL
    max_turns: int = 60
    max_budget_usd: str = "0.50"
    timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not self.run_id or any(char.isspace() for char in self.run_id):
            raise ValueError("run_id must be non-blank and contain no whitespace")
        if self.condition not in _CONDITIONS:
            raise ValueError(f"condition must be one of: {', '.join(_CONDITIONS)}")
        if not _SAFE_TASK_NAME.fullmatch(self.task_name):
            raise ValueError("task_name must be a plain directory name")
        if self.max_turns < 1 or self.max_turns > 200:
            raise ValueError("max_turns must be between 1 and 200")
        try:
            budget = Decimal(self.max_budget_usd)
        except InvalidOperation as error:
            raise ValueError("max_budget_usd must be a decimal") from error
        if not budget.is_finite() or budget <= 0 or budget > 5:
            raise ValueError("max_budget_usd must be greater than zero and at most five")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.condition in _GRAPH_CONDITIONS:
            required = {
                "provisioner_executable": self.provisioner_executable,
                "artifact_path": self.artifact_path,
                "artifact_sha256": self.artifact_sha256,
                "backend_url": self.backend_url,
                "backend_anon_key": self.backend_anon_key,
            }
            missing = sorted(name for name, value in required.items() if value in (None, ""))
            if missing:
                raise ValueError(f"{self.condition} condition requires: {', '.join(missing)}")

    @property
    def task_directory(self) -> Path:
        return self.dataset.resolve(strict=True) / self.task_name


@dataclass(frozen=True)
class BorrowedTrialReceipt:
    """One borrowed task's outcome, in the shape every comparison already reads."""

    challenge_id: str
    outcome: str
    reward: float | None
    tokens: dict[str, int | None]
    cost: dict[str, object]
    timings: dict[str, float | None]
    evidence: dict[str, object]
    provenance: dict[str, str | None]
    memory_evidence: dict[str, object] | None = None


@dataclass(frozen=True)
class BorrowedReceipt:
    schema_version: int
    classification: Literal["exploratory"]
    run_id: str
    condition: Condition
    model: str
    challenge_ids: tuple[str, ...]
    requested_budget_cap_usd_per_trial: str
    requested_budget_cap_usd_across_trials: str
    max_turns: int
    harbor_job_name: str | None
    harbor_exit_code: int | None
    timings: dict[str, float | None]
    outcome: str
    results: tuple[BorrowedTrialReceipt, ...]
    cleanup_completed: bool
    error: str | None
    failure_code: str | None = None
    failure_message: str | None = None
    harbor_diagnostics: tuple[dict[str, str | None], ...] = ()
    graph_uptake: dict[str, object] | None = None
    dataset: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _validate_task_directory(inputs: BorrowedInputs) -> Path:
    """Prove the task is a real, contained, sanitised task before Harbor sees it."""

    dataset = inputs.dataset.resolve(strict=True)
    if not (dataset / MANIFEST_NAME).is_file():
        # Running straight off a registry download would put the reference
        # solution back in the agent's workspace.
        raise ScoredSmokeError("dataset_was_not_sanitised")
    task = dataset / inputs.task_name
    if task.is_symlink() or not task.is_dir():
        raise ScoredSmokeError("borrowed_task_is_not_a_directory")
    if task.resolve().parent != dataset:
        raise ScoredSmokeError("borrowed_task_escapes_its_dataset")
    if not (task / "task.toml").is_file():
        raise ScoredSmokeError("borrowed_task_has_no_task_toml")
    return task


def _job_config(inputs: BorrowedInputs, task: Path) -> dict[str, object]:
    """The Harbor job for one borrowed task, on the stock or configured agent.

    The task is referenced by path rather than through a registry dataset id, so
    the run is pinned to a tree we have audited and can hash. A dataset tag can
    move; a directory we sanitised cannot.
    """

    if inputs.condition == "no-stele":
        agent: dict[str, object] = {"name": "claude-code"}
    else:
        assert inputs.artifact_path is not None
        assert inputs.artifact_sha256 is not None
        assert inputs.backend_url is not None
        assert inputs.backend_anon_key is not None
        attempt_directory = support._attempt_directory(inputs.attempt_root, inputs.run_id)
        kwargs: dict[str, object] = {
            "artifact_path": os.fspath(inputs.artifact_path.absolute()),
            "artifact_sha256": inputs.artifact_sha256.lower(),
            "artifact_source_kind": inputs.artifact_source_kind,
            "credentials_path": os.fspath(attempt_directory / "credentials.json"),
            "preferences_path": os.fspath(attempt_directory / "preferences.json"),
            # No binding to hand over: the container makes the project itself.
            "project_provisioning": "create",
            "project_name": inputs.project_name,
            "auto_rag_injection": "on",
        }
        if inputs.artifact_release_version:
            kwargs["artifact_release_version"] = inputs.artifact_release_version
        agent = {
            "import_path": HARBOR_AGENT_IMPORT_PATH,
            "kwargs": kwargs,
            "env": {
                "SUPABASE_URL": inputs.backend_url,
                "SUPABASE_ANON_KEY": inputs.backend_anon_key,
            },
        }
    # Applied to whichever agent was built above, never to one of them.
    agent["override_setup_timeout_sec"] = AGENT_SETUP_TIMEOUT_SECONDS
    return {
        "job_name": _JOB_NAME,
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "agents": [agent],
        "tasks": [{"path": os.fspath(task)}],
    }


def _run_harbor(inputs: BorrowedInputs, config: Path, jobs_root: Path) -> tuple[int, float]:
    if not os.environ.get("AI_GATEWAY_KEY_EVAL"):
        raise ScoredSmokeError("ai_gateway_key_eval_is_not_exported")
    argv = support.scored_harbor_argv(
        config, jobs_root, harbor_executable=inputs.harbor_executable
    )
    started = time.monotonic()
    try:
        completed = support._managed_harbor_command(
            argv,
            timeout_seconds=inputs.timeout_seconds,
            diagnostic_path=jobs_root.parent / "harbor.diagnostic.json",
            include_gateway=True,
        )
    except support.InstallOnlySmokeError as error:
        code = {
            "harbor_install_only_timeout": "harbor_borrowed_run_timeout",
            "harbor_install_only_output_exceeded_limit": "harbor_borrowed_output_exceeded_limit",
        }.get(error.code, "harbor_borrowed_run_unavailable")
        raise ScoredSmokeError(code) from error
    return completed.exit_code, completed.wall_seconds or (time.monotonic() - started)


def _configure_and_run(
    inputs: BorrowedInputs, config: Path, jobs_root: Path
) -> tuple[int, float]:
    """Attach the semantic bridge only for the arm that has Stele installed."""

    if inputs.condition == "no-stele":
        _configure(inputs, config)
        return _run_harbor(inputs, config, jobs_root)
    gateway_key = os.environ.get("AI_GATEWAY_KEY_EVAL")
    if not gateway_key:
        raise ScoredSmokeError("ai_gateway_key_eval_is_not_exported")
    assert inputs.backend_url is not None
    assert inputs.backend_anon_key is not None
    bridge_config = SemanticBridgeConfig(
        gateway_key=gateway_key,
        supabase_url=inputs.backend_url,
        supabase_anon_key=inputs.backend_anon_key,
        timeout_seconds=min(inputs.timeout_seconds, 15.0),
        expected_token_count=1,
    )
    # Without the bridge the agent's writes cannot be embedded, and the arm
    # would be measuring a degraded install rather than the shipped one.
    with start_semantic_bridge(bridge_config) as bridge:
        _configure(inputs, config, semantic_endpoint=bridge.container_endpoint)
        return _run_harbor(inputs, config, jobs_root)


def _configure(
    inputs: BorrowedInputs, config: Path, *, semantic_endpoint: str | None = None
) -> None:
    try:
        support.configure_agent_job(
            config,
            model=inputs.model,
            max_turns=inputs.max_turns,
            max_budget_usd=inputs.max_budget_usd,
            gateway_url=GATEWAY_URL,
            semantic_endpoint=semantic_endpoint,
        )
    except support.InstallOnlySmokeError as error:
        raise ScoredSmokeError(error.code) from error


def _find_trial(jobs_root: Path, task_name: str) -> tuple[Path, dict[str, Any]]:
    """Locate the single trial for this task, refusing anything ambiguous."""

    found: list[tuple[Path, dict[str, Any]]] = []
    for result_path in sorted(jobs_root.rglob("result.json")):
        result = scored._safe_json(result_path)
        name = result.get("task_name")
        trial_dir = result_path.parent
        if not isinstance(name, str) or not (trial_dir / "agent").is_dir():
            continue
        # A borrowed dataset names its tasks however it likes; the directory
        # name is the only thing we control, so match on its tail.
        if name == task_name or name.rsplit("/", 1)[-1] == task_name:
            found.append((trial_dir, result))
    if not found:
        raise ScoredSmokeError("missing_trial_result")
    if len(found) > 1:
        raise ScoredSmokeError("duplicate_trial_result")
    return found[0]


def _oracle_access_detected(trajectory: Mapping[str, Any] | None) -> list[str]:
    """Every reference to the reference solution, by name or by act.

    The whole trajectory is scanned as text rather than field by field. A cheat
    can arrive as a shell argument, a file-read path, a tool result, or a
    sentence the model wrote about what it just found, and a scanner that only
    reads tool arguments misses three of those four.
    """

    if trajectory is None:
        return []
    try:
        text = json.dumps(trajectory, ensure_ascii=False)
    except (TypeError, ValueError):
        return ["untraversable_trajectory"]
    hits = [name for name in (*FORBIDDEN_NAMES, *FORBIDDEN_TEXT) if name in text]
    if _ORACLE_COMMAND.search(text):
        hits.append("openssl_decrypt")
    return sorted(set(hits))


def _tool_call_counts(trajectory: Mapping[str, Any] | None) -> dict[str, int]:
    if trajectory is None:
        return {"total": 0, "stele": 0}
    calls = scored._tool_calls(trajectory)
    stele = sum(
        1
        for call in calls
        if isinstance(call.get("function_name"), str)
        and call["function_name"].startswith(_STELE_TOOL_PREFIXES)
    )
    return {"total": len(calls), "stele": stele}


def _created_project(agent_dir: Path) -> dict[str, object] | None:
    """What the container said it provisioned, if anything."""

    path = agent_dir / CREATED_PROJECT_REPORT_NAME
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_graph_uptake(
    inputs: BorrowedInputs, reported: Mapping[str, object] | None
) -> dict[str, object]:
    """Ask the database what the agent actually left behind.

    Host-side and by run id, never by the project id the container wrote: the
    point of an independent read is that it does not take the container's word
    for which project to look at. The container's claim is then checked against
    what was found, and a disagreement invalidates the trial — it would mean the
    graph being reported is not the graph the agent used.
    """

    assert inputs.provisioner_executable is not None
    response = support._private_command(
        inputs.provisioner_executable.resolve(strict=True),
        "inspect",
        {"run_id": inputs.run_id},
        timeout_seconds=inputs.timeout_seconds,
    )
    projects = response.get("projects")
    if response.get("ok") is not True or not isinstance(projects, list):
        raise ScoredSmokeError("private_inspect_invalid_response")
    claimed = str(reported.get("project_id")) if isinstance(reported, Mapping) else None
    matched = [
        project
        for project in projects
        if isinstance(project, Mapping) and str(project.get("project_id")) == claimed
    ]
    return {
        "projects": projects,
        "project_count": len(projects),
        "reported_project": dict(reported) if isinstance(reported, Mapping) else None,
        "reported_project_observed": bool(matched),
        "observed": matched[0] if matched else None,
    }


def _trial_receipt(
    inputs: BorrowedInputs,
    trial_dir: Path,
    trial: Mapping[str, Any],
    trajectory: Mapping[str, Any] | None,
    trajectory_error: str | None,
    graph_uptake: Mapping[str, object] | None,
    dataset_provenance: Mapping[str, str],
) -> BorrowedTrialReceipt:
    agent_dir = trial_dir / "agent"
    raw_verifier = trial.get("verifier_result")
    verifier = raw_verifier if isinstance(raw_verifier, dict) else {}
    rewards = verifier.get("rewards") if isinstance(verifier.get("rewards"), dict) else {}
    reward_value = rewards.get("reward")
    reward = float(reward_value) if isinstance(reward_value, (int, float)) else None

    agent_result = (
        trial.get("agent_result") if isinstance(trial.get("agent_result"), Mapping) else {}
    )
    raw_path = agent_dir / "claude-code.txt"
    raw_result = scored._parse_raw_result(raw_path)
    raw_init = scored._parse_raw_init(raw_path)
    usage = scored._model_usage(inputs.model, raw_result) or {}
    cache_write = usage.get("cacheCreationInputTokens")
    if not isinstance(cache_write, int) or isinstance(cache_write, bool):
        cache_write = None

    direct = raw_result.get("total_cost_usd") if raw_result else None
    if isinstance(direct, (int, float)) and not isinstance(direct, bool):
        reported_cost: float | None = float(direct)
        cost_source = "claude-code-result"
        direct_from_result = True
    else:
        fallback = agent_result.get("cost_usd")
        direct_from_result = False
        if isinstance(fallback, (int, float)) and not isinstance(fallback, bool):
            reported_cost = float(fallback)
            cost_source = "harbor-agent-context"
        else:
            reported_cost = None
            cost_source = "unavailable"
    requested_cap = float(inputs.max_budget_usd)

    atif_error = trajectory_error or scored._atif_integrity_error(trajectory)
    oracle_hits = _oracle_access_detected(trajectory)
    # Recorded, deliberately NOT an invalidation. Every aider task sets
    # `allow_internet = true`, and these exercises have published solutions all
    # over the web — so this is the same differential-cheat shape as the bundled
    # payload. It is treated differently because the signal is ambiguous: a
    # network call may equally be a package manager fetching a dependency, and
    # auto-dropping those would bias against whichever arm installs more. The
    # honest handling is to surface the per-arm rate and let a declared decision
    # rule in the pre-registration say when the run stops being interpretable.
    external_access = (
        scored._external_source_access_detected(trajectory) if trajectory is not None else False
    )
    calls = _tool_call_counts(trajectory)
    reported_project = _created_project(agent_dir)
    uptake_disagrees = (
        inputs.condition in _GRAPH_CONDITIONS
        and graph_uptake is not None
        and not graph_uptake.get("reported_project_observed")
    )
    session_files = list((agent_dir / "sessions").rglob("*.jsonl"))
    memory_evidence = (
        scored._session_delivery_evidence(session_files)
        if inputs.condition in _GRAPH_CONDITIONS
        else None
    )

    outcome = "passed" if reward == 1.0 else "failed"
    if trial.get("exception_info") is not None:
        outcome = "infrastructure-error"
    if atif_error is not None or oracle_hits or uptake_disagrees:
        # `invalid` is not a score of zero — it is the apparatus disowning the
        # trial, and the paired analysis drops the whole attempt rather than
        # counting a trial it cannot vouch for.
        outcome = "invalid"

    return BorrowedTrialReceipt(
        challenge_id=inputs.task_name,
        outcome=outcome,
        reward=reward,
        tokens={
            "input": _int_field(agent_result, "n_input_tokens"),
            "cache_read": _int_field(agent_result, "n_cache_tokens"),
            "cache_write": cache_write,
            "output": _int_field(agent_result, "n_output_tokens"),
        },
        cost={
            "claude_reported_amount_usd": reported_cost,
            "claude_reported_source": cost_source,
            "gateway_rate_cross_check_usd": scored._gateway_rate_cross_check(
                inputs.model, raw_result
            ),
            "direct_from_claude_result": direct_from_result,
            "gateway_billing_receipt": False,
            "requested_claude_cli_cap_usd": requested_cap,
            "billed_step_count": 1,
            "claude_report_exceeded_requested_cap": (
                reported_cost > requested_cap if reported_cost is not None else None
            ),
        },
        timings={
            "invocation_wall": scored._seconds(trial),
            "agent_execution": scored._seconds(trial.get("agent_execution")),
            "agent_setup": scored._seconds(trial.get("agent_setup")),
            "verifier": scored._seconds(trial.get("verifier")),
        },
        evidence={
            "trial_result": True,
            "atif_integrity_valid": atif_error is None,
            "atif_integrity_error": atif_error,
            "oracle_payload_access_detected": bool(oracle_hits),
            "oracle_payload_markers": oracle_hits,
            "external_source_access_detected": external_access,
            "raw_claude_stream": raw_path.is_file(),
            "structural_preflight": (agent_dir / "preflight.json").is_file(),
            "injection_channel": scored._injection_channel(agent_dir),
            "verifier_reward_artifact": (trial_dir / "verifier" / "reward.json").is_file(),
            # Total says how much work the exercise took; the Stele share is the
            # overhead the whole experiment is trying to price.
            "tool_calls": calls,
            "created_project": reported_project,
            "graph_uptake": dict(graph_uptake) if graph_uptake is not None else None,
            "graph_uptake_disagrees_with_container": uptake_disagrees,
            "terminal_reason": raw_result.get("terminal_reason") if raw_result else None,
            "result_subtype": raw_result.get("subtype") if raw_result else None,
            "runtime_plugin_count": (
                len(raw_init["plugins"])
                if raw_init and isinstance(raw_init.get("plugins"), list)
                else None
            ),
            "runtime_mcp_server_count": (
                len(raw_init["mcp_servers"])
                if raw_init and isinstance(raw_init.get("mcp_servers"), list)
                else None
            ),
        },
        provenance=dict(dataset_provenance),
        memory_evidence=memory_evidence,
    )


def _int_field(result: Mapping[str, Any], name: str) -> int | None:
    value = result.get(name)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _empty_trial(
    inputs: BorrowedInputs, dataset_provenance: Mapping[str, str]
) -> BorrowedTrialReceipt:
    return BorrowedTrialReceipt(
        challenge_id=inputs.task_name,
        outcome="incomplete",
        reward=None,
        tokens={"input": None, "cache_read": None, "cache_write": None, "output": None},
        cost={
            "claude_reported_amount_usd": None,
            "claude_reported_source": "unavailable",
            "gateway_rate_cross_check_usd": None,
            "direct_from_claude_result": False,
            "gateway_billing_receipt": False,
            "requested_claude_cli_cap_usd": float(inputs.max_budget_usd),
            "billed_step_count": 0,
            "claude_report_exceeded_requested_cap": None,
        },
        timings={
            "invocation_wall": None,
            "agent_execution": None,
            "agent_setup": None,
            "verifier": None,
        },
        evidence={"trial_result": False},
        provenance=dict(dataset_provenance),
    )


def _dataset_provenance(inputs: BorrowedInputs, task: Path) -> dict[str, str]:
    """Pin the borrowed inputs by content, since we do not own them.

    Both halves are recorded because they answer different questions later: the
    workspace is what the agent was given, the tests are how it was judged, and
    an upstream change to either one must be visible as a change rather than as
    a mysterious move in the score.
    """

    return {
        "dataset_path": os.fspath(inputs.dataset.resolve()),
        "task_name": inputs.task_name,
        "workspace_sha256": _tree_digest(task / WORKSPACE_DIRECTORY),
        "verifier_sha256": _tree_digest(task / "tests"),
        "sanitisation_manifest_sha256": support._sha256_file(
            inputs.dataset.resolve() / MANIFEST_NAME
        ),
    }


def run_borrowed_task(inputs: BorrowedInputs) -> BorrowedReceipt:
    task = _validate_task_directory(inputs)
    dataset_provenance = _dataset_provenance(inputs, task)
    has_stele = inputs.condition in _GRAPH_CONDITIONS

    run_directory = support._safe_work_directory(inputs.work_root, inputs.run_id)
    jobs_root = run_directory / "jobs"
    config = run_directory / _CONFIG_NAME

    cleanup_completed = not has_stele
    primary_error: BaseException | None = None
    harbor_exit: int | None = None
    harbor_wall_seconds: float | None = None
    parsed: BorrowedTrialReceipt | None = None
    graph_uptake: dict[str, object] | None = None
    previous_attempt_root = os.environ.get("STELE_BENCH_ATTEMPT_ROOT")
    if has_stele:
        os.environ["STELE_BENCH_ATTEMPT_ROOT"] = os.fspath(inputs.attempt_root.resolve())
    try:
        if has_stele:
            assert inputs.provisioner_executable is not None
            prepared = support._private_command(
                inputs.provisioner_executable.resolve(strict=True),
                "prepare",
                {"run_id": inputs.run_id, "harnesses": ["claude"]},
                timeout_seconds=inputs.timeout_seconds,
            )
            if prepared.get("ok") is not True:
                raise ScoredSmokeError("private_prepare_invalid_response")
        run_directory.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(_job_config(inputs, task), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        harbor_exit, harbor_wall_seconds = _configure_and_run(inputs, config, jobs_root)
        trial_dir, trial = _find_trial(jobs_root, inputs.task_name)
        trajectory_path = trial_dir / "agent" / "trajectory.json"
        trajectory: dict[str, Any] | None = None
        trajectory_error: str | None = None
        if not trajectory_path.is_file():
            trajectory_error = "missing_atif_trajectory"
        else:
            try:
                trajectory = scored._safe_json(trajectory_path)
            except (OSError, ScoredSmokeError):
                trajectory_error = "malformed_atif_trajectory"
        if has_stele:
            # Read the graph BEFORE cleanup, which is what destroys it.
            graph_uptake = _read_graph_uptake(
                inputs, _created_project(trial_dir / "agent")
            )
        parsed = _trial_receipt(
            inputs,
            trial_dir,
            trial,
            trajectory,
            trajectory_error,
            graph_uptake,
            dataset_provenance,
        )
    except BaseException as error:
        primary_error = error
    finally:
        if has_stele and inputs.provisioner_executable is not None:
            try:
                cleaned = support._private_command(
                    inputs.provisioner_executable.resolve(strict=True),
                    "cleanup",
                    {"run_id": inputs.run_id},
                    timeout_seconds=inputs.timeout_seconds,
                )
                cleanup_completed = cleaned == {"ok": True}
                if not cleanup_completed:
                    primary_error = primary_error or ScoredSmokeError(
                        "private_cleanup_invalid_response"
                    )
            except BaseException as error:
                primary_error = primary_error or error
        if previous_attempt_root is None:
            os.environ.pop("STELE_BENCH_ATTEMPT_ROOT", None)
        else:
            os.environ["STELE_BENCH_ATTEMPT_ROOT"] = previous_attempt_root

    result = parsed or _empty_trial(inputs, dataset_provenance)
    if primary_error is not None:
        outcome = "invalid"
    elif harbor_exit not in (0, None) and result.outcome not in ("invalid", "incomplete"):
        outcome = "invalid"
    else:
        outcome = result.outcome
    failure_code, failure_message = (
        scored._failure_details(primary_error) if primary_error is not None else (None, None)
    )
    receipt = BorrowedReceipt(
        schema_version=1,
        classification="exploratory",
        run_id=inputs.run_id,
        condition=inputs.condition,
        model=inputs.model,
        challenge_ids=(inputs.task_name,),
        requested_budget_cap_usd_per_trial=inputs.max_budget_usd,
        requested_budget_cap_usd_across_trials=inputs.max_budget_usd,
        max_turns=inputs.max_turns,
        harbor_job_name=_JOB_NAME,
        harbor_exit_code=harbor_exit,
        timings={"invocation_wall": harbor_wall_seconds},
        outcome=outcome,
        results=(result,),
        cleanup_completed=cleanup_completed,
        error=failure_code,
        failure_code=failure_code,
        failure_message=failure_message,
        harbor_diagnostics=support._recover_harbor_diagnostics(run_directory, []),
        graph_uptake=graph_uptake,
        dataset=dict(dataset_provenance),
    )
    if primary_error is not None:
        raise ScoredSmokeError("borrowed_task_failed", receipt=receipt) from primary_error
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="sanitised dataset directory")
    parser.add_argument("task", help="task directory name inside the dataset")
    parser.add_argument("--condition", choices=_CONDITIONS, default="no-stele")
    parser.add_argument(
        "--provisioner", type=Path, default=Path(__file__).with_name("provision.py")
    )
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--artifact-sha256")
    parser.add_argument("--backend-url")
    parser.add_argument("--backend-anon-key")
    parser.add_argument(
        "--artifact-source-kind",
        choices=("release", "local-dirty-worktree"),
        default="local-dirty-worktree",
    )
    parser.add_argument("--artifact-release-version")
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--harbor", default="harbor")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--max-budget-usd", default="0.50")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument(
        "--receipt-out",
        type=Path,
        help="atomically persist the success or failure envelope as private JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    envelope: dict[str, object]
    try:
        inputs = BorrowedInputs(
            dataset=args.dataset,
            task_name=args.task,
            attempt_root=args.attempt_root,
            work_root=args.work_root,
            run_id=args.run_id,
            condition=args.condition,
            provisioner_executable=args.provisioner,
            artifact_path=args.artifact,
            artifact_sha256=args.artifact_sha256,
            backend_url=args.backend_url,
            backend_anon_key=args.backend_anon_key,
            artifact_source_kind=args.artifact_source_kind,
            artifact_release_version=args.artifact_release_version,
            project_name=args.project_name,
            harbor_executable=args.harbor,
            model=args.model,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
            timeout_seconds=args.timeout_seconds,
        )
        with support._controlled_termination_signals():
            receipt = run_borrowed_task(inputs)
    except (ScoredSmokeError, support.RunnerInterrupted, OSError, ValueError) as error:
        cause = (
            error.__cause__
            if isinstance(error, ScoredSmokeError) and error.code == "borrowed_task_failed"
            else None
        )
        code = getattr(error, "code", None) or error.__class__.__name__
        carried = getattr(error, "receipt", None)
        envelope = {
            "ok": False,
            "error": code,
            "detail": str(cause) if cause is not None else None,
            "receipt": carried.as_dict() if carried is not None else None,
        }
    else:
        envelope = {"ok": True, "receipt": receipt.as_dict()}
    if args.receipt_out is not None:
        support._atomic_write_json(args.receipt_out, envelope)
    print(json.dumps(envelope, sort_keys=True))
    return 0 if envelope["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
