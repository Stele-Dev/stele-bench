#!/usr/bin/env python3
"""Run one budget-capped scored Harbor challenge invocation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, NamedTuple

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC_ROOT = _REPOSITORY_ROOT / "eval" / "stele-bench"
_PUBLIC_SOURCE = _PUBLIC_ROOT / "src"
sys.path.insert(0, os.fspath(_PUBLIC_SOURCE))
sys.path.insert(0, os.fspath(Path(__file__).resolve().parent))

import run_install_only_smoke as support  # noqa: E402
from semantic_bridge import SemanticBridgeConfig, start_semantic_bridge  # noqa: E402

from stele_bench.challenges import LoadedChallenge, select_challenges  # noqa: E402
from stele_bench.contracts import GraphArm, RunIdentity  # noqa: E402
from stele_bench.corpus import audit_effective_context, materialize_pinned_tree  # noqa: E402
from stele_bench.fixtures import load_fixture  # noqa: E402
from stele_bench.harbor_pack import FaithfulAgentConfig, generate_harbor_task_pack  # noqa: E402
from stele_bench.memory_evidence import (  # noqa: E402
    detect_injection_in_session,
    detect_stele_delivery_in_session,
)
from stele_bench.preflight import (  # noqa: E402
    INJECTION_CHANNEL_REPORT_NAME,
    AutoRagInjection,
)
from stele_bench.provisioning import ExternalCommandProvisioner  # noqa: E402

GATEWAY_URL = "https://ai-gateway.vercel.sh"
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_CHALLENGES = ("service-readiness",)
_GATEWAY_RATES_PER_TOKEN = {
    DEFAULT_MODEL: {
        "input": 1 / 1_000_000,
        "output": 5 / 1_000_000,
        "cache_read": 0.1 / 1_000_000,
        "cache_write": 1.25 / 1_000_000,
    }
}
_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
# `faithful-norag` is `faithful` with prompt-time auto-RAG injection stripped
# (TASK-2497). It is the Class B control: the agent still has Stele — tools,
# skills, memory block, the same restored graph — but nothing is pushed at it, so
# anything it recalls it had to go and fetch.
Condition = Literal["no-stele", "faithful", "faithful-norag"]
_CONDITIONS: tuple[Condition, ...] = ("no-stele", "faithful", "faithful-norag")
# The conditions that install Stele and restore a graph. Both of these need the
# artifact, fixture, provisioner and backend inputs; they differ only downstream.
_GRAPH_CONDITIONS: tuple[Condition, ...] = ("faithful", "faithful-norag")

# The subcommand must follow `git` within one shell command: the match may not
# cross a newline or a command separator. These patterns run on each raw
# argument string, not on serialized JSON. Without the limit, a heredoc of
# source code holding the string "git" and a later `.clone()` read as
# `git clone` and invalidated an honest arm.
_GIT_REMOTE = re.compile(
    r"\bgit\b[^\n;&|]{0,240}?\b(?:clone|fetch|pull|ls-remote)\b",
    re.IGNORECASE,
)
_GIT_HISTORY = re.compile(
    r"\bgit\b[^\n;&|]{0,240}?\b(?:log|reflog|rev-list|show|cat-file|blame)\b",
    re.IGNORECASE,
)
_DOTSTATE_REPOSITORY_URL = re.compile(
    r"(?:github\.com|githubusercontent\.com).{0,160}(?:serkanyersen[/\\]dotstate|dotstate)",
    re.IGNORECASE | re.DOTALL,
)
_DOTSTATE_GH_REFERENCE = re.compile(
    r"\bgh\b.{0,240}(?:serkanyersen[/\\]dotstate|repos[/\\]serkanyersen[/\\]dotstate)",
    re.IGNORECASE | re.DOTALL,
)
_ATIF_SCHEMA_VERSION = re.compile(r"^ATIF-v1\.\d+$")
_EXTERNAL_SEARCH_TOOLS = {
    "browser",
    "browse",
    "searchweb",
    "webfetch",
    "websearch",
    "web_fetch",
    "web_search",
}
_SHELL_NETWORK_CLIENT = re.compile(
    r"\bgh\s+(?:search|api)\b|"
    r"\b(?:curl|wget|httpie|lynx)\b"
    r"(?!\s+(?:--version|--help|-V|-h)(?:\s|[\"']|$))",
    re.IGNORECASE,
)
_HTTP_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_PIP_BOOTSTRAP_URL = "https://bootstrap.pypa.io/get-pip.py"
_DELEGATION_TOOLS = {"agent", "task", "spawn_agent", "delegate"}
# Tools an agent uses to READ its way to an answer. The memory claim under test
# is that a recalled prior decision lets the agent surface a constraint early
# instead of reading the codebase to (wrongly) infer it, so counting these in
# the graded turn is the behavioural half of the oracle — the other half being
# whether the answer cited the decision at all. Deliberately NOT a reward: an
# agent that recalls the decision and then still investigates is fine.
_INVESTIGATION_TOOLS = {
    "bash",
    "exec_command",
    "glob",
    "grep",
    "list_dir",
    "ls",
    "read",
    "read_file",
    "search",
    "shell",
    "terminal",
}


class ScoredSmokeError(RuntimeError):
    """The scored invocation could not produce a trustworthy receipt."""

    def __init__(self, code: str, *, receipt: ScoredReceipt | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.receipt = receipt


@dataclass(frozen=True)
class ScoredInputs:
    repository: Path
    revision: str
    attempt_root: Path
    work_root: Path
    run_id: str
    condition: Condition = "faithful"
    challenge_ids: tuple[str, ...] = DEFAULT_CHALLENGES
    fixture_path: Path | None = None
    provisioner_executable: Path | None = None
    artifact_path: Path | None = None
    artifact_sha256: str | None = None
    backend_url: str | None = None
    backend_anon_key: str | None = None
    harbor_executable: str = "harbor"
    model: str = DEFAULT_MODEL
    max_turns: int = 3
    max_budget_usd: str = "0.10"
    timeout_seconds: float = 600.0
    artifact_source_kind: Literal["release", "local-dirty-worktree"] = "local-dirty-worktree"
    artifact_release_version: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id or any(c.isspace() for c in self.run_id):
            raise ValueError("run_id must be non-blank and contain no whitespace")
        if self.condition not in _CONDITIONS:
            raise ValueError(f"condition must be one of: {', '.join(_CONDITIONS)}")
        if not self.challenge_ids:
            raise ValueError("at least one challenge is required")
        if len(self.challenge_ids) != len(set(self.challenge_ids)):
            raise ValueError("challenge ids must not contain duplicates")
        if any(not value or any(char.isspace() for char in value) for value in self.challenge_ids):
            raise ValueError("challenge ids must be non-blank and contain no whitespace")
        if self.max_turns < 1 or self.max_turns > 100:
            raise ValueError("max_turns must be between 1 and 100")
        try:
            budget = Decimal(self.max_budget_usd)
        except InvalidOperation as error:
            raise ValueError("max_budget_usd must be a decimal") from error
        if not budget.is_finite() or budget <= 0 or budget > 5:
            raise ValueError("max_budget_usd must be greater than zero and at most five")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.condition in _GRAPH_CONDITIONS:
            if len(self.challenge_ids) != 1:
                raise ValueError(
                    "graph conditions require exactly one challenge until "
                    "per-challenge attempt credentials are available"
                )
            required = {
                "fixture_path": self.fixture_path,
                "provisioner_executable": self.provisioner_executable,
                "artifact_path": self.artifact_path,
                "artifact_sha256": self.artifact_sha256,
                "backend_url": self.backend_url,
                "backend_anon_key": self.backend_anon_key,
            }
            missing = sorted(name for name, value in required.items() if value in (None, ""))
            if missing:
                raise ValueError(f"{self.condition} condition requires: {', '.join(missing)}")


@dataclass(frozen=True)
class ChallengeReceipt:
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
class ScoredReceipt:
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
    results: tuple[ChallengeReceipt, ...]
    cleanup_completed: bool
    error: str | None
    failure_code: str | None = None
    failure_message: str | None = None
    harbor_diagnostics: tuple[dict[str, str | None], ...] = ()
    # What the agent did to the restored graph. The Stele arm runs a real
    # install, whose instructions tell the agent to create, claim and complete
    # tasks and to record knowledge, so a changed graph is evidence, not a
    # failure. The exact-restore proof is taken before the agent starts.
    graph_after_run: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _identity(run_id: str, challenge_ids: Sequence[str]) -> RunIdentity:
    challenge_key = challenge_ids[0] if len(challenge_ids) == 1 else "challenge-batch"
    return RunIdentity(
        run_id=run_id,
        experiment_id="scored-challenge-run",
        condition_id="faithful",
        challenge_id=challenge_key,
        attempt_id=f"{run_id}-faithful",
        attempt_index=0,
    )


def _configure_scored_job(
    path: Path, inputs: ScoredInputs, *, semantic_endpoint: str | None = None
) -> None:
    try:
        support.configure_agent_job(
            path,
            model=inputs.model,
            max_turns=inputs.max_turns,
            max_budget_usd=inputs.max_budget_usd,
            gateway_url=GATEWAY_URL,
            semantic_endpoint=semantic_endpoint,
        )
    except support.InstallOnlySmokeError as error:
        raise ScoredSmokeError(error.code) from error


def _run_harbor(inputs: ScoredInputs, config: Path, jobs_root: Path) -> tuple[int, float]:
    model_key = support.model_provider.key_env()
    if not os.environ.get(model_key):
        raise ScoredSmokeError(f"{model_key.lower()}_is_not_exported")
    argv = support.scored_harbor_argv(config, jobs_root, harbor_executable=inputs.harbor_executable)
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
            "harbor_install_only_timeout": "harbor_scored_run_timeout",
            "harbor_install_only_output_exceeded_limit": ("harbor_scored_output_exceeded_limit"),
        }.get(error.code, "harbor_scored_run_unavailable")
        raise ScoredSmokeError(code) from error
    return completed.exit_code, completed.wall_seconds or (time.monotonic() - started)


def _run_configured_harbor(
    inputs: ScoredInputs, config: Path, jobs_root: Path
) -> tuple[int, float]:
    """Configure and run Harbor with a semantic bridge only for faithful trials."""
    if inputs.condition == "no-stele":
        _configure_scored_job(config, inputs)
        return _run_harbor(inputs, config, jobs_root)
    if inputs.backend_url is None or inputs.backend_anon_key is None:
        raise ScoredSmokeError("faithful_semantic_bridge_is_not_configured")
    gateway_key = os.environ.get("AI_GATEWAY_KEY_EVAL")
    if not gateway_key:
        raise ScoredSmokeError("ai_gateway_key_eval_is_not_exported")
    if len(inputs.challenge_ids) != 1:
        raise ScoredSmokeError("faithful_multi_challenge_credentials_are_not_isolated")
    bridge_config = SemanticBridgeConfig(
        gateway_key=gateway_key,
        supabase_url=inputs.backend_url,
        supabase_anon_key=inputs.backend_anon_key,
        timeout_seconds=min(inputs.timeout_seconds, 15.0),
        expected_token_count=1,
    )
    with start_semantic_bridge(bridge_config) as bridge:
        _configure_scored_job(
            config,
            inputs,
            semantic_endpoint=bridge.container_endpoint,
        )
        return _run_harbor(inputs, config, jobs_root)


def _safe_json(path: Path, *, max_bytes: int = _MAX_ARTIFACT_BYTES) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ScoredSmokeError("scored_artifact_is_not_a_safe_json_file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScoredSmokeError("scored_artifact_is_not_valid_json") from error
    if not isinstance(value, dict):
        raise ScoredSmokeError("scored_artifact_is_not_a_json_object")
    return value


_SAFE_STEP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class _TrialArtifacts(NamedTuple):
    """Where a trial's graded agent + verifier artifacts live, plus per-step data.

    Index order (trial_dir, result, trajectory, trajectory_error) is preserved so
    positional consumers keep working; ``step_root`` and ``step_results`` are the
    multi-step additions.
    """

    trial_dir: Path
    result: dict[str, Any]
    trajectory: dict[str, Any] | None
    trajectory_error: str | None
    step_root: Path
    step_results: tuple[Mapping[str, Any], ...] | None


def _scored_step_root(
    trial_dir: Path, result: Mapping[str, Any]
) -> tuple[Path, tuple[Mapping[str, Any], ...] | None]:
    """Resolve the artifact root of the graded step and the per-step results.

    A ``resume_trajectory`` task records ``step_results`` as a list and writes
    each step under ``steps/<name>/{agent,verifier}``;
    ``multi_step_reward_strategy = "final"`` grades the LAST step, so its
    trajectory, sessions, and verifier are the graded artifacts. A single-step
    task keeps them directly under the trial directory. When a multi-step layout
    is declared but its graded step directory cannot be resolved, fail closed by
    pointing at the (absent) single-step location so the trajectory reads as
    missing rather than silently skipping the check.
    """

    step_results = result.get("step_results")
    if not (isinstance(step_results, list) and step_results):
        return trial_dir, None
    steps = tuple(step for step in step_results if isinstance(step, Mapping))
    name = steps[-1].get("step_name") if steps else None
    if isinstance(name, str) and _SAFE_STEP_NAME.fullmatch(name):
        candidate = trial_dir / "steps" / name
        if candidate.parent == trial_dir / "steps" and candidate.is_dir():
            return candidate, steps
    return trial_dir, steps


def _find_trials(
    jobs_root: Path, challenges: Sequence[LoadedChallenge]
) -> dict[str, _TrialArtifacts]:
    expected = {challenge.challenge_id for challenge in challenges}
    found: dict[str, _TrialArtifacts] = {}
    extras: list[str] = []
    for result_path in sorted(jobs_root.rglob("result.json")):
        result = _safe_json(result_path)
        task_name = result.get("task_name")
        trial_dir = result_path.parent
        # Harbor job aggregates also contain config.json + result.json. Only a
        # trial owns the agent evidence directory used by the receipt below;
        # a multi-step trial keeps it under steps/<name>/agent instead.
        looks_like_trial = (
            isinstance(task_name, str)
            or (trial_dir / "agent").is_dir()
            or (trial_dir / "steps").is_dir()
        )
        if not looks_like_trial:
            continue
        if not isinstance(task_name, str):
            extras.append("missing-task-name")
            continue
        prefix = "local/"
        challenge_id = task_name[len(prefix) :] if task_name.startswith(prefix) else task_name
        if challenge_id not in expected:
            extras.append(task_name)
            continue
        if challenge_id in found:
            raise ScoredSmokeError("duplicate_trial_result")
        step_root, step_results = _scored_step_root(trial_dir, result)
        trajectory_path = step_root / "agent" / "trajectory.json"
        trajectory: dict[str, Any] | None = None
        trajectory_error: str | None = None
        if not trajectory_path.is_file():
            trajectory_error = "missing_atif_trajectory"
        else:
            try:
                trajectory = _safe_json(trajectory_path)
            except (OSError, ScoredSmokeError):
                trajectory_error = "malformed_atif_trajectory"
        found[challenge_id] = _TrialArtifacts(
            trial_dir, result, trajectory, trajectory_error, step_root, step_results
        )
    if extras:
        raise ScoredSmokeError("unexpected_trial_result")
    missing = [
        challenge.challenge_id for challenge in challenges if challenge.challenge_id not in found
    ]
    if missing or len(found) != len(challenges):
        raise ScoredSmokeError("missing_trial_result")
    return found


def _seconds(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    start, finish = value.get("started_at"), value.get("finished_at")
    if not isinstance(start, str) or not isinstance(finish, str):
        return None
    from datetime import datetime

    try:
        return max(
            0.0, (datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds()
        )
    except ValueError:
        return None


def _parse_raw_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_ARTIFACT_BYTES:
        return None
    final: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            final = event
    return final


def _parse_raw_init(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_ARTIFACT_BYTES:
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            return event
    return None


def _model_usage(model: str, raw_result: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    usage_by_model = raw_result.get("modelUsage") if raw_result is not None else None
    if not isinstance(usage_by_model, Mapping):
        return None
    usage = usage_by_model.get(model)
    if usage is None:
        # Under the direct provider the CLI reports the bare first-party id.
        try:
            usage = usage_by_model.get(support.model_provider.harbor_model_name(model))
        except support.model_provider.ModelProviderError:
            usage = None
    return usage if isinstance(usage, Mapping) else None


def _gateway_rate_cross_check(model: str, raw_result: Mapping[str, Any] | None) -> float | None:
    rates = _GATEWAY_RATES_PER_TOKEN.get(model)
    usage = _model_usage(model, raw_result)
    if rates is None or usage is None:
        return None
    fields = {
        "input": usage.get("inputTokens"),
        "output": usage.get("outputTokens"),
        "cache_read": usage.get("cacheReadInputTokens"),
        "cache_write": usage.get("cacheCreationInputTokens"),
    }
    if not all(isinstance(value, int) and value >= 0 for value in fields.values()):
        return None
    return sum(fields[name] * rates[name] for name in fields)


def _tool_calls(trajectory: Mapping[str, Any], *, start: int = 0) -> list[Mapping[str, Any]]:
    calls: list[Mapping[str, Any]] = []
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        return calls
    for step in steps[start:]:
        if not isinstance(step, Mapping) or not isinstance(step.get("tool_calls"), list):
            continue
        calls.extend(call for call in step["tool_calls"] if isinstance(call, Mapping))
    return calls


def _final_turn_start(trajectory: Mapping[str, Any], final_prompt: str | None) -> int:
    """Index of the first step after the final user turn.

    A multi-step challenge resumes the agent's own session, so the graded
    step's trajectory contains every earlier turn as well. Anything that claims
    to describe "the graded turn" has to slice here first, or it silently
    reports the whole session.
    """

    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not final_prompt:
        return 0
    needle = _normalize_response(final_prompt)
    if not needle:
        return 0
    start = 0
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping) or step.get("source") != "user":
            continue
        message = step.get("message")
        if isinstance(message, str) and needle in _normalize_response(message):
            start = index + 1
    return start


def _atif_integrity_error(trajectory: Mapping[str, Any] | None) -> str | None:
    if trajectory is None:
        return "missing_or_malformed_atif_trajectory"
    schema_version = trajectory.get("schema_version")
    if not isinstance(schema_version, str) or not _ATIF_SCHEMA_VERSION.fullmatch(schema_version):
        return "unsupported_atif_schema"
    if not isinstance(trajectory.get("session_id"), str) or not trajectory["session_id"]:
        return "invalid_atif_session"
    agent = trajectory.get("agent")
    if (
        not isinstance(agent, Mapping)
        or not isinstance(agent.get("name"), str)
        or not agent["name"]
    ):
        return "invalid_atif_agent"
    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps:
        return "invalid_atif_steps"
    step_ids: set[int] = set()
    tool_call_ids: set[str] = set()
    sources: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping):
            return "invalid_atif_step"
        step_id = step.get("step_id")
        if isinstance(step_id, bool) or not isinstance(step_id, int) or step_id in step_ids:
            return "invalid_atif_step_id"
        step_ids.add(step_id)
        if not isinstance(step.get("timestamp"), str) or not step["timestamp"]:
            return "invalid_atif_timestamp"
        if step.get("source") not in {"user", "agent"}:
            return "invalid_atif_source"
        sources.add(step["source"])
        if not isinstance(step.get("message"), str):
            return "invalid_atif_message"
        calls = step.get("tool_calls")
        if calls is None:
            continue
        if not isinstance(calls, list):
            return "invalid_atif_tool_calls"
        observation = step.get("observation")
        results = observation.get("results") if isinstance(observation, Mapping) else None
        if calls and not isinstance(results, list):
            return "missing_atif_tool_results"
        result_ids = {
            result.get("source_call_id")
            for result in results or []
            if isinstance(result, Mapping) and isinstance(result.get("source_call_id"), str)
        }
        for call in calls:
            if not isinstance(call, Mapping):
                return "invalid_atif_tool_call"
            call_id = call.get("tool_call_id")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in tool_call_ids
                or not isinstance(call.get("function_name"), str)
                or not isinstance(call.get("arguments"), Mapping)
                or call_id not in result_ids
            ):
                return "unauditable_atif_tool_call"
            tool_call_ids.add(call_id)
    if sources != {"user", "agent"}:
        return "incomplete_atif_conversation"
    return None


def _count_investigation(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_tool: dict[str, int] = {}
    investigative = 0
    for call in calls:
        name = call.get("function_name")
        if not isinstance(name, str):
            continue
        normalized = name.casefold().replace("-", "_")
        if normalized in _INVESTIGATION_TOOLS:
            investigative += 1
            by_tool[normalized] = by_tool.get(normalized, 0) + 1
    return {"total": len(calls), "investigative": investigative, "by_tool": by_tool}


def _injection_channel(agent_root: Path) -> dict[str, object] | None:
    """Read the arm's verified injection-channel report, if setup wrote one.

    `None` means the file is absent, which is expected for `no-stele` (there is
    no install to inspect) and is itself a finding for any graph condition —
    setup fails closed before a trial runs, so a completed graph-condition trial
    without this report means the receipt and the container disagree.
    """

    path = agent_root / INJECTION_CHANNEL_REPORT_NAME
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _investigation_depth(
    trajectory: Mapping[str, Any] | None, final_prompt: str | None = None
) -> dict[str, Any]:
    """Count how much reading the agent did, at BOTH scopes that matter.

    Reported, never rewarded. Paired with the answer-side "did it cite the
    prior decision" flag, this is what distinguishes *memory short-circuited
    the dig* from *the agent read the whole codebase and still could not know*.

    Two scopes, because on a resumed multi-step challenge they answer different
    questions and only one of them is usually non-zero:

    - the top-level counts cover the **graded turn only** (steps after the final
      user message). For a question answered in conversation this is frequently
      zero in every arm — the agent replies straight from context — so a
      difference here is a strong signal and its absence means very little.
    - ``session`` covers the **whole resumed session**, which is where turn-1
      exploration lives. If memory shortens the dig at all, that is where it
      shows up.

    Counting the whole session and calling it "the graded turn" conflates the
    two and reports identical turn-1 exploration as if it were the answer.
    """

    if trajectory is None:
        return {"total": None, "investigative": None, "by_tool": {}, "session": None}
    start = _final_turn_start(trajectory, final_prompt)
    graded = _count_investigation(_tool_calls(trajectory, start=start))
    graded["scanned_from"] = start
    graded["session"] = _count_investigation(_tool_calls(trajectory))
    return graded


def _normalize_response(text: str) -> str:
    """Ignore presentation-only Markdown, unify dashes, and collapse whitespace."""

    lowered = text.casefold().replace("`", "").replace("–", "-").replace("—", "-")
    return " ".join(lowered.split())


def _response_evidence(
    trajectory: Mapping[str, Any] | None,
    final_prompt: str | None,
    markers: Sequence[str],
) -> dict[str, Any] | None:
    """Scan the agent's answer to the FINAL turn for declared recall markers.

    For a question whose correct answer is spoken rather than written to a file,
    the transcript is the graded artifact and it lives here, not in the
    container. Only agent messages AFTER the final user turn are scanned, so
    reasoning from earlier turns cannot score. Matching is a literal, case-folded
    substring test -- deterministic, with no model in the loop.
    """

    if not markers:
        return None
    if trajectory is None or not isinstance(trajectory.get("steps"), list):
        return {"cited": False, "markers_found": [], "response_chars": 0, "scanned_from": None}

    steps = trajectory["steps"]
    start = _final_turn_start(trajectory, final_prompt)
    answer = " ".join(
        step["message"]
        for step in steps[start:]
        if isinstance(step, Mapping)
        and step.get("source") == "agent"
        and isinstance(step.get("message"), str)
    )
    normalized = _normalize_response(answer)
    found = sorted({marker for marker in markers if _normalize_response(marker) in normalized})
    return {
        "cited": bool(found),
        "markers_found": found,
        "response_chars": len(answer),
        "scanned_from": start,
    }


def _opaque_delegation_detected(trajectory: Mapping[str, Any]) -> bool:
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        return False
    for step in steps:
        if not isinstance(step, Mapping) or not isinstance(step.get("tool_calls"), list):
            continue
        for call in step["tool_calls"]:
            if not isinstance(call, Mapping):
                continue
            function_name = call.get("function_name")
            arguments = call.get("arguments")
            if (
                not isinstance(function_name, str)
                or function_name.casefold() not in _DELEGATION_TOOLS
                or not isinstance(arguments, Mapping)
            ):
                continue
            prompt = arguments.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                return True
            matching_child_indices = [
                index
                for index, candidate in enumerate(steps)
                if isinstance(candidate, Mapping)
                and candidate.get("source") == "user"
                and candidate.get("message") == prompt
                and isinstance(candidate.get("extra"), Mapping)
                and candidate["extra"].get("is_sidechain") is True
            ]
            if not matching_child_indices:
                return True
            child_is_audited = False
            for index in matching_child_indices:
                for candidate in steps[index + 1 :]:
                    if not isinstance(candidate, Mapping):
                        continue
                    extra = candidate.get("extra")
                    is_sidechain = isinstance(extra, Mapping) and extra.get("is_sidechain") is True
                    if is_sidechain and candidate.get("source") == "agent":
                        child_is_audited = True
                        break
                if child_is_audited:
                    break
            if not child_is_audited:
                return True
    return False


def _external_source_access_detected(trajectory: Mapping[str, Any]) -> bool:
    for tool_call in _tool_calls(trajectory):
        function_name = tool_call.get("function_name")
        arguments = tool_call.get("arguments")
        if not isinstance(function_name, str) or not isinstance(arguments, Mapping):
            continue
        normalized_name = function_name.casefold().replace("-", "_")
        if normalized_name in _EXTERNAL_SEARCH_TOOLS or (
            "web" in normalized_name
            and any(word in normalized_name for word in ("search", "fetch", "browse"))
        ):
            return True
        if function_name.casefold() in {"bash", "shell", "terminal", "exec_command"}:
            executable_values = [
                arguments.get(field)
                for field in ("command", "cmd", "script")
                if isinstance(arguments.get(field), str)
            ]
            # Descriptions and labels are inert metadata. Scanning them caused
            # `description: Check if curl or wget is available` to invalidate a
            # command that only ran `curl --version || wget --version`.
            serialized = "\n".join(executable_values) or json.dumps(arguments, sort_keys=True)
            network_clients = list(_SHELL_NETWORK_CLIENT.finditer(serialized))
            urls = {match.group(0).rstrip(".,);]") for match in _HTTP_URL.finditer(serialized)}
            package_bootstrap_only = (
                bool(network_clients)
                and urls == {_PIP_BOOTSTRAP_URL}
                and all(
                    match.group(0).casefold().startswith(("curl", "wget"))
                    for match in network_clients
                )
            )
            if network_clients and not package_bootstrap_only:
                return True
    return False


def _string_values(value: Any) -> Iterator[str]:
    """Every string inside a tool call's arguments, unescaped."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_values(item)


def _forbidden_repository_access(
    trajectory: Mapping[str, Any], *, allow_local_git_history: bool = False
) -> bool:
    """Detect commands that can reveal the benchmark repository or its history.

    Public networking is required for the model gateway, so the scored runner
    validates recorded tool calls as a fail-closed integrity safeguard.
    """
    for tool_call in _tool_calls(trajectory):
        function_name = tool_call.get("function_name")
        arguments = tool_call.get("arguments")
        if not isinstance(function_name, str) or not isinstance(arguments, Mapping):
            continue
        serialized = json.dumps(arguments, sort_keys=True)
        shell_tool = function_name.casefold() in {
            "bash",
            "shell",
            "terminal",
            "exec_command",
        }
        raw_strings = list(_string_values(arguments)) if shell_tool else []
        if any(_GIT_REMOTE.search(text) for text in raw_strings):
            return True
        if not allow_local_git_history and any(
            _GIT_HISTORY.search(text) for text in raw_strings
        ):
            return True
        if _DOTSTATE_REPOSITORY_URL.search(serialized):
            return True
        if shell_tool and _DOTSTATE_GH_REFERENCE.search(serialized):
            return True
    return False


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _int_field_sum(agent_results: Sequence[Mapping[str, Any]], field: str) -> int | None:
    values = [
        result.get(field)
        for result in agent_results
        if isinstance(result.get(field), int) and not isinstance(result.get(field), bool)
    ]
    return sum(values) if values else None


def _session_injection_evidence(session_files: Sequence[Path], marker: str) -> dict[str, object]:
    """Parse retained session transcripts and detect the fixture marker in them."""

    events: list[object] = []
    for session in session_files:
        if not (
            session.is_file()
            and not session.is_symlink()
            and session.stat().st_size <= _MAX_ARTIFACT_BYTES
        ):
            continue
        for line in session.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return detect_injection_in_session(events, marker).as_dict()


def _session_delivery_evidence(session_files: Sequence[Path]) -> dict[str, object]:
    """Detect any node-bearing Stele injection in retained session transcripts."""

    events: list[object] = []
    for session in session_files:
        if not (
            session.is_file()
            and not session.is_symlink()
            and session.stat().st_size <= _MAX_ARTIFACT_BYTES
        ):
            continue
        for line in session.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return detect_stele_delivery_in_session(events).as_dict()


def _extract_challenge_receipt(
    inputs: ScoredInputs,
    challenge: LoadedChallenge,
    trial_dir: Path,
    trial: dict[str, Any],
    trajectory: dict[str, Any] | None,
    common_provenance: Mapping[str, str | None],
    trajectory_load_error: str | None = None,
    *,
    step_root: Path | None = None,
    step_results: Sequence[Mapping[str, Any]] | None = None,
    memory_marker: str | None = None,
    allow_local_git_history: bool = False,
) -> ChallengeReceipt:
    # The graded step (the last, under multi_step_reward_strategy="final") owns
    # the scored trajectory, sessions, and verifier; a single-step trial keeps
    # them directly under the trial directory.
    scored_root = step_root if step_root is not None else trial_dir
    verifier = (
        trial.get("verifier_result") if isinstance(trial.get("verifier_result"), dict) else {}
    )
    rewards = verifier.get("rewards") if isinstance(verifier.get("rewards"), dict) else {}
    reward_value = rewards.get("reward")
    reward = float(reward_value) if isinstance(reward_value, (int, float)) else None

    # Money-relevant fields aggregate across every billed step (a resumed
    # session pays for each turn); single-value diagnostics come from the graded
    # step only.
    if step_results is not None:
        step_agent_dirs = [
            (
                step.get("agent_result") if isinstance(step.get("agent_result"), Mapping) else {},
                trial_dir / "steps" / str(step["step_name"]) / "agent",
            )
            for step in step_results
            if isinstance(step.get("step_name"), str)
            and _SAFE_STEP_NAME.fullmatch(step["step_name"])
        ]
    else:
        single = trial.get("agent_result") if isinstance(trial.get("agent_result"), Mapping) else {}
        step_agent_dirs = [(single, scored_root / "agent")]
    agent_results = [result for result, _ in step_agent_dirs]
    step_raw = [
        (result, _parse_raw_result(agent_dir / "claude-code.txt"))
        for result, agent_dir in step_agent_dirs
    ]

    metrics = trajectory.get("final_metrics") if trajectory is not None else None
    if not isinstance(metrics, dict):
        metrics = {}
    metric_extra = metrics.get("extra") if isinstance(metrics.get("extra"), dict) else {}
    estimated = metric_extra.get("cost_source") == "litellm_estimate"

    cache_write_values: list[int] = []
    cross_check_values: list[float] = []
    for _, raw in step_raw:
        usage = _model_usage(inputs.model, raw) or {}
        cache_created = usage.get("cacheCreationInputTokens")
        if isinstance(cache_created, int) and not isinstance(cache_created, bool):
            cache_write_values.append(cache_created)
        cross = _gateway_rate_cross_check(inputs.model, raw)
        if cross is not None:
            cross_check_values.append(cross)
    cache_write = sum(cache_write_values) if cache_write_values else None
    cross_check = sum(cross_check_values) if cross_check_values else None

    direct_costs = [
        raw["total_cost_usd"]
        for _, raw in step_raw
        if raw is not None
        and isinstance(raw.get("total_cost_usd"), (int, float))
        and not isinstance(raw.get("total_cost_usd"), bool)
    ]
    if step_raw and len(direct_costs) == len(step_raw):
        reported_cost: float | None = float(sum(direct_costs))
        source = "claude-code-result"
        direct_from_result = True
    else:
        agent_costs = [
            result["cost_usd"]
            for result in agent_results
            if isinstance(result.get("cost_usd"), (int, float))
            and not isinstance(result.get("cost_usd"), bool)
        ]
        reported_cost = float(sum(agent_costs)) if agent_costs else None
        direct_from_result = False
        if reported_cost is None:
            source = "unavailable"
        else:
            source = "litellm-estimate" if estimated else "harbor-agent-context"
    requested_cap = float(inputs.max_budget_usd)
    trial_cap = requested_cap * max(1, len(step_agent_dirs))

    # Single-value diagnostics from the graded step's raw stream.
    raw_path = scored_root / "agent" / "claude-code.txt"
    scored_raw_result = _parse_raw_result(raw_path)
    scored_raw_init = _parse_raw_init(raw_path)

    session_files = list((scored_root / "agent" / "sessions").rglob("*.jsonl"))
    session_bytes = 0
    lifecycle_mentions = 0
    for session in session_files:
        if (
            session.is_file()
            and not session.is_symlink()
            and session.stat().st_size <= _MAX_ARTIFACT_BYTES
        ):
            payload = session.read_bytes()
            session_bytes += len(payload)
            lifecycle_mentions += payload.lower().count(b"stele")

    if memory_marker:
        memory_evidence = _session_injection_evidence(session_files, memory_marker)
    elif inputs.condition in _GRAPH_CONDITIONS:
        memory_evidence = _session_delivery_evidence(session_files)
    else:
        memory_evidence = None
    response_evidence = _response_evidence(
        trajectory,
        challenge.manifest.follow_up_prompt or challenge.manifest.prompt,
        challenge.manifest.response_markers,
    )

    atif_error = trajectory_load_error or _atif_integrity_error(trajectory)
    repository_access_detected = (
        _forbidden_repository_access(trajectory, allow_local_git_history=allow_local_git_history)
        if trajectory is not None
        else False
    )
    opaque_delegation_detected = (
        _opaque_delegation_detected(trajectory) if trajectory is not None else False
    )
    external_source_access_detected = (
        _external_source_access_detected(trajectory) if trajectory is not None else False
    )
    outcome = "passed" if reward == 1.0 else "failed"
    if trial.get("exception_info") is not None:
        outcome = "infrastructure-error"
    if (
        atif_error is not None
        or repository_access_detected
        or opaque_delegation_detected
        or external_source_access_detected
    ):
        outcome = "invalid"
    manifest_path = challenge.directory / "challenge.json"
    provenance = dict(common_provenance)
    provenance.update(
        {
            "challenge_manifest_sha256": _sha256(manifest_path),
            "verifier_sha256": challenge.manifest.verifier.asset.sha256,
        }
    )
    return ChallengeReceipt(
        challenge_id=challenge.challenge_id,
        outcome=outcome,
        reward=reward,
        tokens={
            "input": _int_field_sum(agent_results, "n_input_tokens"),
            "cache_read": _int_field_sum(agent_results, "n_cache_tokens"),
            "cache_write": cache_write,
            "output": _int_field_sum(agent_results, "n_output_tokens"),
        },
        cost={
            "claude_reported_amount_usd": reported_cost,
            "claude_reported_source": source,
            "gateway_rate_cross_check_usd": cross_check,
            "direct_from_claude_result": direct_from_result,
            "gateway_billing_receipt": False,
            "requested_claude_cli_cap_usd": requested_cap,
            "billed_step_count": len(step_agent_dirs),
            "claude_report_exceeded_requested_cap": (
                reported_cost > trial_cap if reported_cost is not None else None
            ),
        },
        timings={
            "invocation_wall": _seconds(trial),
            "agent_execution": _seconds(trial.get("agent_execution")),
            "agent_setup": _seconds(trial.get("agent_setup")),
            "verifier": _seconds(trial.get("verifier")),
        },
        evidence={
            "trial_result": True,
            "atif_integrity_valid": atif_error is None,
            "atif_integrity_error": atif_error,
            "forbidden_repository_access_detected": repository_access_detected,
            "opaque_delegation_detected": opaque_delegation_detected,
            "external_source_access_detected": external_source_access_detected,
            "raw_claude_stream": raw_path.is_file(),
            "normalized_atif": atif_error is None,
            "structural_preflight": (scored_root / "agent" / "preflight.json").is_file(),
            # The content, not just presence: this is the proof that the arm's
            # injection channel was what the arm claimed. An analysis that pools
            # arms without checking this can report "no difference" from a pair
            # that never actually differed.
            "injection_channel": _injection_channel(scored_root / "agent"),
            "verifier_reward_artifact": (scored_root / "verifier" / "reward.json").is_file(),
            "verifier_detail_artifact": (scored_root / "verifier" / "evidence.json").is_file(),
            "session_file_count": len(session_files),
            "session_bytes": session_bytes,
            "stele_lifecycle_mentions": lifecycle_mentions,
            "investigation_depth": _investigation_depth(
                trajectory, challenge.manifest.follow_up_prompt or challenge.manifest.prompt
            ),
            "response_evidence": response_evidence,
            "terminal_reason": (
                scored_raw_result.get("terminal_reason") if scored_raw_result else None
            ),
            "result_subtype": scored_raw_result.get("subtype") if scored_raw_result else None,
            "runtime_plugin_count": (
                len(scored_raw_init.get("plugins", []))
                if scored_raw_init and isinstance(scored_raw_init.get("plugins"), list)
                else None
            ),
            "runtime_mcp_server_count": (
                len(scored_raw_init.get("mcp_servers", []))
                if scored_raw_init and isinstance(scored_raw_init.get("mcp_servers"), list)
                else None
            ),
        },
        provenance=provenance,
        memory_evidence=memory_evidence,
    )


def _empty_result(
    challenge: LoadedChallenge, provenance: Mapping[str, str | None]
) -> ChallengeReceipt:
    result_provenance = dict(provenance)
    result_provenance.update(
        {
            "challenge_manifest_sha256": _sha256(challenge.directory / "challenge.json"),
            "verifier_sha256": challenge.manifest.verifier.asset.sha256,
        }
    )
    return ChallengeReceipt(
        challenge_id=challenge.challenge_id,
        outcome="infrastructure-error",
        reward=None,
        tokens={"input": None, "cache_read": None, "cache_write": None, "output": None},
        cost={
            "claude_reported_amount_usd": None,
            "claude_reported_source": "unavailable",
            "gateway_rate_cross_check_usd": None,
            "direct_from_claude_result": False,
            "gateway_billing_receipt": False,
            "requested_claude_cli_cap_usd": None,
            "claude_report_exceeded_requested_cap": None,
        },
        timings={
            "invocation_wall": None,
            "agent_execution": None,
            "agent_setup": None,
            "verifier": None,
        },
        evidence={"trial_result": False},
        provenance=result_provenance,
    )


def _aggregate_outcome(
    results: Sequence[ChallengeReceipt],
    harbor_exit: int | None,
    primary_error: BaseException | None,
) -> str:
    if primary_error is not None or any(
        result.outcome == "infrastructure-error" for result in results
    ):
        return "infrastructure-error"
    if any(result.outcome == "invalid" for result in results):
        return "invalid"
    if any(result.outcome == "failed" for result in results):
        return "failed"
    if harbor_exit not in (None, 0):
        return "infrastructure-error"
    if results and all(result.outcome == "passed" for result in results):
        return "passed"
    return "failed"


def _failure_details(error: BaseException) -> tuple[str, str]:
    if isinstance(error, support.RunnerInterrupted):
        return "runner_interrupted", "runner interrupted by a termination signal"
    if isinstance(error, ScoredSmokeError) and re.fullmatch(r"[a-z0-9_]{1,96}", error.code):
        return error.code, error.code.replace("_", " ")
    if isinstance(error, ValueError):
        return "invalid_arguments", "runner arguments are invalid"
    if isinstance(error, OSError):
        return "operating_system_error", "runner could not access a required local resource"
    return "unexpected_error", "runner failed unexpectedly"


def _graph_after_run(
    restored: Any, snapshot: Any, restore: Any, fixture: Any
) -> dict[str, object]:
    # Read the states directly. `snapshot.observation(restore)` refuses to build
    # once the embedding queue moved, and an agent edit re-queues the node it
    # touched, so it cannot describe a graph the agent wrote to.
    before = restored.state
    after = snapshot.state
    expected = fixture.graphs[GraphArm.FAITHFUL]
    return {
        "unchanged": after.canonical_state.sha256 == expected.sha256,
        "node_count_before": before.declared_node_count,
        "node_count_after": after.declared_node_count,
        "edge_count_before": before.declared_edge_count,
        "edge_count_after": after.declared_edge_count,
        "embed_queue_before": restore.queue_before_count,
        "embed_queue_after": snapshot.queue_after_count,
    }


def run_scored_smoke(inputs: ScoredInputs) -> ScoredReceipt:
    challenges = select_challenges(inputs.challenge_ids)
    # True for both graph conditions: they install Stele and restore the same
    # faithful graph. What separates them is `auto_rag_injection` below — so read
    # this as "has Stele", not "is the treatment arm".
    faithful = inputs.condition in _GRAPH_CONDITIONS
    auto_rag_injection: AutoRagInjection = "off" if inputs.condition == "faithful-norag" else "on"
    fixture = (
        load_fixture(inputs.fixture_path.resolve(strict=True))
        if faithful and inputs.fixture_path
        else None
    )
    artifact: Path | None = None
    if faithful and inputs.artifact_path and inputs.artifact_sha256:
        artifact = inputs.artifact_path.resolve(strict=True)
        if support._sha256_file(artifact) != inputs.artifact_sha256.lower():
            raise ScoredSmokeError("integration_artifact_digest_does_not_match")

    run_directory = support._safe_work_directory(inputs.work_root, inputs.run_id)
    jobs_root = run_directory / "jobs"
    attempt_directory = support._attempt_directory(inputs.attempt_root, inputs.run_id)
    provisioner: ExternalCommandProvisioner | None = None
    if faithful and inputs.provisioner_executable and inputs.fixture_path:
        provisioner = ExternalCommandProvisioner(
            (os.fspath(inputs.provisioner_executable.resolve(strict=True)),),
            inputs.fixture_path.resolve(strict=True).parent,
            timeout_seconds=inputs.timeout_seconds,
        )

    project = None
    cleanup_completed = not faithful
    primary_error: BaseException | None = None
    harbor_exit: int | None = None
    harbor_wall_seconds: float | None = None
    graph_after_run: dict[str, object] | None = None
    harbor_job_name: str | None = None
    source_commit = inputs.revision
    source_tree_sha = ""
    parsed: tuple[ChallengeReceipt, ...] | None = None
    previous_attempt_root = os.environ.get("STELE_BENCH_ATTEMPT_ROOT")
    if faithful:
        os.environ["STELE_BENCH_ATTEMPT_ROOT"] = os.fspath(inputs.attempt_root.resolve())
    try:
        corpus = materialize_pinned_tree(
            inputs.repository, inputs.revision, run_directory / "corpus" / "project"
        )
        source_commit, source_tree_sha = corpus.source_commit, corpus.tree_sha256
        audit_effective_context(corpus)
        faithful_config: FaithfulAgentConfig | None = None
        if faithful:
            assert inputs.provisioner_executable is not None
            assert provisioner is not None
            assert fixture is not None
            assert artifact is not None
            assert inputs.artifact_sha256 is not None
            assert inputs.backend_url is not None
            assert inputs.backend_anon_key is not None
            prepared = support._private_command(
                inputs.provisioner_executable.resolve(strict=True),
                "prepare",
                {"run_id": inputs.run_id, "harnesses": ["claude"]},
                timeout_seconds=inputs.timeout_seconds,
            )
            if prepared.get("ok") is not True:
                raise ScoredSmokeError("private_prepare_invalid_response")
            with support._scoped_child_environment():
                restore = asyncio.run(
                    provisioner.restore_with_receipt(
                        fixture,
                        GraphArm.FAITHFUL,
                        _identity(inputs.run_id, inputs.challenge_ids),
                    )
                )
            project = restore.project
            # Prove the restore before the agent can write: afterwards the graph
            # legitimately differs from the fixture.
            with support._scoped_child_environment():
                restored = asyncio.run(provisioner.snapshot_with_receipt(project))
            if not support._snapshot_matches_fixture(restored.observation(restore), fixture):
                raise ScoredSmokeError("restored_snapshot_mismatch")
            support._read_binding(attempt_directory / "project.json")
            faithful_config = FaithfulAgentConfig(
                artifact_path=artifact,
                artifact_sha256=inputs.artifact_sha256,
                artifact_source_kind=inputs.artifact_source_kind,
                artifact_release_version=inputs.artifact_release_version,
                credentials_path=attempt_directory / "credentials.json",
                preferences_path=attempt_directory / "preferences.json",
                project_binding_path=attempt_directory / "project.json",
                backend_url=inputs.backend_url,
                backend_anon_key=inputs.backend_anon_key,
                auto_rag_injection=auto_rag_injection,
            )

        pack = generate_harbor_task_pack(
            corpus,
            run_directory / "pack",
            faithful_config,
            challenges=challenges,
        )
        if faithful:
            if pack.configured_config is None or pack.configured_job_name is None:
                raise ScoredSmokeError("configured_route_was_not_generated")
            config = pack.configured_config
            harbor_job_name = pack.configured_job_name
        else:
            config = pack.plain_config
            harbor_job_name = pack.plain_job_name
        for task_directory in pack.task_directories:
            audit_effective_context(task_directory)
        harbor_exit, harbor_wall_seconds = _run_configured_harbor(inputs, config, jobs_root)
        trials = _find_trials(jobs_root, challenges)
        common_provenance = {
            "source_commit": source_commit,
            "source_tree_sha256": source_tree_sha,
            "fixture_id": fixture.fixture_id if fixture else None,
            "graph_arm": "faithful" if faithful else None,
            "artifact_sha256": (
                inputs.artifact_sha256.lower() if faithful and inputs.artifact_sha256 else None
            ),
        }
        memory_marker = fixture.memory_marker if fixture is not None else None
        parsed_results: list[ChallengeReceipt] = []
        for challenge in challenges:
            artifacts = trials[challenge.challenge_id]
            parsed_results.append(
                _extract_challenge_receipt(
                    inputs,
                    challenge,
                    artifacts.trial_dir,
                    artifacts.result,
                    artifacts.trajectory,
                    common_provenance,
                    artifacts.trajectory_error,
                    step_root=artifacts.step_root,
                    step_results=artifacts.step_results,
                    memory_marker=memory_marker,
                )
            )
        parsed = tuple(parsed_results)
        if faithful:
            assert provisioner is not None
            assert fixture is not None
            with support._scoped_child_environment():
                snapshot = asyncio.run(provisioner.snapshot_with_receipt(project))
            graph_after_run = _graph_after_run(restored, snapshot, restore, fixture)
        for task_directory in pack.task_directories:
            audit_effective_context(task_directory)
    except BaseException as error:
        primary_error = error
    finally:
        if faithful:
            if project is not None and provisioner is not None:
                try:
                    with support._scoped_child_environment():
                        asyncio.run(provisioner.destroy(project))
                except BaseException as error:
                    primary_error = primary_error or error
            if inputs.provisioner_executable is not None:
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

    common_provenance = {
        "source_commit": source_commit,
        "source_tree_sha256": source_tree_sha or None,
        "fixture_id": fixture.fixture_id if fixture else None,
        "graph_arm": "faithful" if faithful else None,
        "artifact_sha256": (
            inputs.artifact_sha256.lower() if faithful and inputs.artifact_sha256 else None
        ),
    }
    results = parsed or tuple(
        _empty_result(challenge, common_provenance) for challenge in challenges
    )
    outcome = _aggregate_outcome(results, harbor_exit, primary_error)
    failure_code, failure_message = (
        _failure_details(primary_error) if primary_error is not None else (None, None)
    )
    receipt = ScoredReceipt(
        schema_version=4,
        classification="exploratory",
        run_id=inputs.run_id,
        condition=inputs.condition,
        model=inputs.model,
        challenge_ids=inputs.challenge_ids,
        requested_budget_cap_usd_per_trial=inputs.max_budget_usd,
        requested_budget_cap_usd_across_trials=str(
            Decimal(inputs.max_budget_usd) * len(challenges)
        ),
        max_turns=inputs.max_turns,
        harbor_job_name=harbor_job_name,
        harbor_exit_code=harbor_exit,
        timings={"invocation_wall": harbor_wall_seconds},
        outcome=outcome,
        results=results,
        cleanup_completed=cleanup_completed,
        error=failure_code,
        failure_code=failure_code,
        failure_message=failure_message,
        harbor_diagnostics=support._recover_harbor_diagnostics(run_directory, []),
        graph_after_run=graph_after_run,
    )
    if primary_error is not None:
        raise ScoredSmokeError("scored_smoke_failed", receipt=receipt) from primary_error
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("revision")
    parser.add_argument("fixture", type=Path, nargs="?")
    parser.add_argument("--condition", choices=_CONDITIONS, default="faithful")
    parser.add_argument("--challenge", action="append", dest="challenges")
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
    parser.add_argument("--harbor", default="harbor")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--max-budget-usd", default="0.10")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument(
        "--receipt-out",
        type=Path,
        help="atomically persist the success or failure envelope as private JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    envelope: dict[str, object]
    try:
        inputs = ScoredInputs(
            repository=args.repository,
            revision=args.revision,
            fixture_path=args.fixture,
            provisioner_executable=args.provisioner,
            attempt_root=args.attempt_root,
            work_root=args.work_root,
            run_id=args.run_id,
            condition=args.condition,
            challenge_ids=tuple(args.challenges or DEFAULT_CHALLENGES),
            artifact_path=args.artifact,
            artifact_sha256=args.artifact_sha256,
            backend_url=args.backend_url,
            backend_anon_key=args.backend_anon_key,
            artifact_source_kind=args.artifact_source_kind,
            artifact_release_version=args.artifact_release_version,
            harbor_executable=args.harbor,
            model=args.model,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
            timeout_seconds=args.timeout_seconds,
        )
        with support._controlled_termination_signals():
            receipt = run_scored_smoke(inputs)
    except (ScoredSmokeError, support.RunnerInterrupted, OSError, ValueError) as error:
        cause = (
            error.__cause__
            if isinstance(error, ScoredSmokeError) and error.code == "scored_smoke_failed"
            else None
        )
        code, message = _failure_details(cause or error)
        retained = error.receipt if isinstance(error, ScoredSmokeError) else None
        envelope = {
            "ok": False,
            "failure": {"code": code, "message": message},
            "receipt": retained.as_dict() if retained is not None else None,
        }
        if args.receipt_out is not None:
            support._atomic_write_json(args.receipt_out, envelope)
        print(json.dumps(envelope, sort_keys=True))
        return 1
    envelope = {"ok": True, "receipt": receipt.as_dict()}
    if args.receipt_out is not None:
        support._atomic_write_json(args.receipt_out, envelope)
    print(json.dumps(envelope, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
