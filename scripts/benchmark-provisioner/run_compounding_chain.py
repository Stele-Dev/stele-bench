#!/usr/bin/env python3
"""Run one manifest-driven compounding chain with only the graph carried forward."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC_SOURCE = _REPOSITORY_ROOT / "eval" / "stele-bench" / "src"
sys.path.insert(0, os.fspath(_PUBLIC_SOURCE))
sys.path.insert(0, os.fspath(Path(__file__).resolve().parent))

import gateway_reserve  # noqa: E402
import run_install_only_smoke as support  # noqa: E402
import run_scored_smoke as scored  # noqa: E402
from semantic_bridge import SemanticBridgeConfig, start_semantic_bridge  # noqa: E402

from stele_bench.challenges import LoadedChallenge, select_challenges  # noqa: E402
from stele_bench.corpus import (  # noqa: E402
    BlindnessAuditConfig,
    GitHistoryIdentity,
    audit_effective_context,
    materialize_pinned_history,
    materialize_pinned_tree,
)
from stele_bench.harbor_pack import (  # noqa: E402
    HARBOR_TASK_IMAGE,
    FaithfulAgentConfig,
    generate_harbor_task_pack,
)
from stele_bench.memory_evidence import (  # noqa: E402
    detect_injection_in_session,
    detect_stele_delivery_in_session,
)
from stele_bench.preflight import (  # noqa: E402
    CREATED_PROJECT_REPORT_NAME,
    ActiveProjectBinding,
)

GATEWAY_URL = scored.GATEWAY_URL
DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_CHAIN_MANIFEST = (
    _REPOSITORY_ROOT / "eval" / "stele-bench" / "preregistrations" / "click-clirunner-k5-v1.json"
)
SESSION_CHALLENGES = ("profile-selection-intake", "profile-selection-live-path")
LIVE_IMPLEMENTATION_MARKER = "src/components/profile_selection_popup.rs"
ALTERNATIVE_MARKERS = (
    "ProfileSelectionScreen",
    "src/screens/profile_selection.rs",
)
Condition = Literal["no-stele", "stele-fresh"]
_CONDITIONS: tuple[Condition, ...] = ("no-stele", "stele-fresh")
_FULL_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_FULL_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HISTORY_AUDIT_CONFIG = BlindnessAuditConfig(
    forbidden_path_components=tuple(
        component
        for component in BlindnessAuditConfig().forbidden_path_components
        if component != ".git"
    )
)
_STELE_READ_TOOLS = frozenset(
    {
        "build_context",
        "fetch",
        "get_node",
        "grep_nodes",
        "inspect",
        "list",
        "list_components",
        "list_documents",
        "list_my_projects",
        "list_tasks",
        "list_topics",
        "recall",
        "search",
        "search_documents",
        "search_nodes",
        "stats",
        "trace_provenance",
    }
)


class CompoundingRunError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CompoundingInputs:
    repository: Path
    revision: str | None
    attempt_root: Path
    work_root: Path
    chain_id: str
    condition: Condition
    provisioner_executable: Path | None = None
    artifact_path: Path | None = None
    artifact_sha256: str | None = None
    backend_url: str | None = None
    backend_anon_key: str | None = None
    artifact_source_kind: Literal["release", "local-dirty-worktree"] = "local-dirty-worktree"
    artifact_release_version: str | None = None
    project_name: str = "Workspace"
    harbor_executable: str = "harbor"
    model: str = DEFAULT_MODEL
    max_turns: int = 45
    max_budget_usd: str = "10.00"
    timeout_seconds: float = 900.0
    gateway_reserve_config: Path | None = None
    chain_manifest_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.chain_id or any(char.isspace() for char in self.chain_id):
            raise ValueError("chain_id must be non-blank and contain no whitespace")
        if self.condition not in _CONDITIONS:
            raise ValueError(f"condition must be one of: {', '.join(_CONDITIONS)}")
        if self.max_turns < 1 or self.max_turns > 100:
            raise ValueError("max_turns must be between 1 and 100")
        try:
            budget = Decimal(self.max_budget_usd)
        except InvalidOperation as error:
            raise ValueError("max_budget_usd must be a decimal") from error
        if not budget.is_finite() or budget <= 0 or budget > 10:
            raise ValueError("max_budget_usd must be greater than zero and at most ten")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.revision is None and self.chain_manifest_path is None:
            raise ValueError("revision or chain_manifest_path is required")
        if self.condition == "stele-fresh":
            required = {
                "provisioner_executable": self.provisioner_executable,
                "artifact_path": self.artifact_path,
                "artifact_sha256": self.artifact_sha256,
                "backend_url": self.backend_url,
                "backend_anon_key": self.backend_anon_key,
            }
            missing = sorted(name for name, value in required.items() if value in (None, ""))
            if missing:
                raise ValueError(f"stele-fresh requires: {', '.join(missing)}")
        if self.artifact_source_kind == "release" and not self.artifact_release_version:
            raise ValueError("release artifacts require artifact_release_version")
        if (
            self.artifact_source_kind == "local-dirty-worktree"
            and self.artifact_release_version not in (None, "")
        ):
            raise ValueError("local artifacts cannot set artifact_release_version")


@dataclass(frozen=True)
class _SessionRun:
    receipt: dict[str, Any]
    trial_dir: Path
    step_root: Path
    trajectory: dict[str, Any] | None
    source_commit: str
    source_tree_sha256: str
    source_base_tree: str | None = None
    history: GitHistoryIdentity | None = None


@dataclass(frozen=True)
class _SessionSpec:
    index: int
    challenge_id: str
    base_revision: str
    base_tree: str | None = None
    canonical_target_revision: str | None = None
    challenge_manifest_sha256: str | None = None
    verifier_sha256: str | None = None
    history: GitHistoryIdentity | None = None


@dataclass(frozen=True)
class _ChainSpec:
    experiment_id: str
    manifest_path: Path | None
    manifest_sha256: str | None
    sessions: tuple[_SessionSpec, ...]


def _sha256_json(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _required_string(value: object, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise CompoundingRunError(f"chain_manifest_missing_{label}")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise CompoundingRunError(f"chain_manifest_invalid_{label}")
    return value


def _chain_spec(inputs: CompoundingInputs) -> _ChainSpec:
    if inputs.chain_manifest_path is None:
        assert inputs.revision is not None
        sessions = tuple(
            _SessionSpec(index, challenge_id, inputs.revision)
            for index, challenge_id in enumerate(SESSION_CHALLENGES, 1)
        )
        return _ChainSpec("profile-selection-legacy", None, None, sessions)

    path = inputs.chain_manifest_path.resolve(strict=True)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CompoundingRunError("chain_manifest_invalid_json") from error
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise CompoundingRunError("chain_manifest_invalid_schema")
    experiment_id = _required_string(raw.get("experiment_id"), "experiment_id")
    runtime = raw.get("runtime")
    if not isinstance(runtime, Mapping):
        raise CompoundingRunError("chain_manifest_missing_runtime")
    if runtime.get("model") != inputs.model:
        raise CompoundingRunError("chain_manifest_model_mismatch")
    if runtime.get("harness") != "claude-code":
        raise CompoundingRunError("chain_manifest_harness_mismatch")
    if runtime.get("task_image") != HARBOR_TASK_IMAGE:
        raise CompoundingRunError("chain_manifest_task_image_mismatch")
    raw_sessions = raw.get("sessions")
    if not isinstance(raw_sessions, list) or len(raw_sessions) < 1:
        raise CompoundingRunError("chain_manifest_sessions_invalid")
    sessions: list[_SessionSpec] = []
    selected_ids: list[str] = []
    for expected_index, item in enumerate(raw_sessions, 1):
        if not isinstance(item, Mapping) or item.get("index") != expected_index:
            raise CompoundingRunError("chain_manifest_session_order_invalid")
        challenge_id = _required_string(item.get("challenge_id"), "challenge_id")
        selected_ids.append(challenge_id)
        history = GitHistoryIdentity(
            head_tree=_required_string(item.get("base_tree"), "base_tree", _FULL_SHA1),
            commit_count=int(item.get("reachable_commit_count", 0)),
            object_count=int(item.get("reachable_object_count", 0)),
            object_ids_sha256=_required_string(
                item.get("reachable_object_ids_sha256"),
                "reachable_object_ids_sha256",
                _FULL_SHA256,
            ),
        )
        if history.commit_count < 1 or history.object_count < 1:
            raise CompoundingRunError("chain_manifest_history_counts_invalid")
        sessions.append(
            _SessionSpec(
                index=expected_index,
                challenge_id=challenge_id,
                base_revision=_required_string(
                    item.get("base_revision"), "base_revision", _FULL_SHA1
                ),
                base_tree=history.head_tree,
                canonical_target_revision=_required_string(
                    item.get("canonical_target_revision"),
                    "canonical_target_revision",
                    _FULL_SHA1,
                ),
                challenge_manifest_sha256=_required_string(
                    item.get("challenge_manifest_sha256"),
                    "challenge_manifest_sha256",
                    _FULL_SHA256,
                ),
                verifier_sha256=_required_string(
                    item.get("verifier_sha256"), "verifier_sha256", _FULL_SHA256
                ),
                history=history,
            )
        )
    loaded = select_challenges(selected_ids)
    for spec, challenge in zip(sessions, loaded, strict=True):
        if _sha256_file(challenge.directory / "challenge.json") != spec.challenge_manifest_sha256:
            raise CompoundingRunError("chain_manifest_challenge_digest_mismatch")
        if challenge.manifest.verifier.asset.sha256 != spec.verifier_sha256:
            raise CompoundingRunError("chain_manifest_verifier_digest_mismatch")
    return _ChainSpec(
        experiment_id=experiment_id,
        manifest_path=path,
        manifest_sha256=_sha256_file(path),
        sessions=tuple(sessions),
    )


def _configure_and_run(
    inputs: CompoundingInputs, config: Path, jobs_root: Path
) -> tuple[int, float]:
    if not os.environ.get("AI_GATEWAY_KEY_EVAL"):
        raise CompoundingRunError("ai_gateway_key_eval_is_not_exported")

    def execute(semantic_endpoint: str | None = None) -> tuple[int, float]:
        support.configure_agent_job(
            config,
            model=inputs.model,
            max_turns=inputs.max_turns,
            max_budget_usd=inputs.max_budget_usd,
            gateway_url=GATEWAY_URL,
            semantic_endpoint=semantic_endpoint,
        )
        argv = support.scored_harbor_argv(
            config, jobs_root, harbor_executable=inputs.harbor_executable
        )
        started = time.monotonic()
        completed = support._managed_harbor_command(
            argv,
            timeout_seconds=inputs.timeout_seconds,
            diagnostic_path=jobs_root.parent / "harbor.diagnostic.json",
            include_gateway=True,
        )
        return completed.exit_code, completed.wall_seconds or (time.monotonic() - started)

    if inputs.condition == "no-stele":
        return execute()
    assert inputs.backend_url is not None
    assert inputs.backend_anon_key is not None
    bridge = SemanticBridgeConfig(
        gateway_key=os.environ["AI_GATEWAY_KEY_EVAL"],
        supabase_url=inputs.backend_url,
        supabase_anon_key=inputs.backend_anon_key,
        timeout_seconds=min(inputs.timeout_seconds, 15.0),
        expected_token_count=1,
    )
    with start_semantic_bridge(bridge) as running:
        return execute(running.container_endpoint)


def _faithful_config(
    inputs: CompoundingInputs,
    *,
    session_number: int,
    binding_path: Path | None,
) -> FaithfulAgentConfig:
    assert inputs.artifact_path is not None
    assert inputs.artifact_sha256 is not None
    assert inputs.backend_url is not None
    assert inputs.backend_anon_key is not None
    attempt = support._attempt_directory(inputs.attempt_root, inputs.chain_id)
    return FaithfulAgentConfig(
        artifact_path=inputs.artifact_path,
        artifact_sha256=inputs.artifact_sha256,
        artifact_source_kind=inputs.artifact_source_kind,
        artifact_release_version=inputs.artifact_release_version,
        credentials_path=attempt / "credentials.json",
        preferences_path=attempt / "preferences.json",
        project_binding_path=binding_path,
        backend_url=inputs.backend_url,
        backend_anon_key=inputs.backend_anon_key,
        auto_rag_injection="on",
        project_provisioning="create" if session_number == 1 else "restore",
        project_name=inputs.project_name,
    )


def _run_session(
    inputs: CompoundingInputs,
    *,
    session_number: int,
    binding_path: Path | None,
) -> _SessionRun:
    chain = _chain_spec(inputs)
    try:
        stage = chain.sessions[session_number - 1]
    except IndexError as error:
        raise CompoundingRunError("session_number_out_of_range") from error
    challenge_id = stage.challenge_id
    challenges = select_challenges((challenge_id,))
    session_id = f"{inputs.chain_id}-session-{session_number}"
    run_directory = support._safe_work_directory(inputs.work_root, session_id)
    jobs_root = run_directory / "jobs"
    if stage.history is None:
        corpus = materialize_pinned_tree(
            inputs.repository,
            stage.base_revision,
            run_directory / "corpus" / "project",
        )
    else:
        assert stage.canonical_target_revision is not None
        corpus = materialize_pinned_history(
            inputs.repository,
            stage.base_revision,
            run_directory / "corpus" / "project",
            forbidden_object_ids=(stage.canonical_target_revision,),
        )
        if corpus.history != stage.history:
            raise CompoundingRunError("materialized_history_identity_mismatch")
    audit_effective_context(corpus)
    configured = (
        _faithful_config(inputs, session_number=session_number, binding_path=binding_path)
        if inputs.condition == "stele-fresh"
        else None
    )
    pack = generate_harbor_task_pack(
        corpus,
        run_directory / "pack",
        configured,
        challenges=challenges,
    )
    config = pack.configured_config if configured is not None else pack.plain_config
    if config is None:
        raise CompoundingRunError("session_config_was_not_generated")
    for task_directory in pack.task_directories:
        audit_effective_context(
            task_directory,
            _HISTORY_AUDIT_CONFIG if stage.history is not None else BlindnessAuditConfig(),
        )
    interval_context = (
        gateway_reserve.exclusive_gateway_interval(inputs.gateway_reserve_config)
        if inputs.gateway_reserve_config is not None
        else nullcontext(None)
    )
    with interval_context as exclusive_interval:
        gateway_before = (
            gateway_reserve.observe_gateway_key_spend(config_path=inputs.gateway_reserve_config)
            if inputs.gateway_reserve_config is not None
            else None
        )
        harbor_exit, harbor_wall = _configure_and_run(inputs, config, jobs_root)
        gateway_after = (
            gateway_reserve.observe_gateway_key_spend(config_path=inputs.gateway_reserve_config)
            if inputs.gateway_reserve_config is not None
            else None
        )
    gateway_delta = None
    if gateway_before is not None and gateway_after is not None:
        gateway_delta = format(
            max(
                Decimal("0"),
                Decimal(str(gateway_after["current_spend_usd"]))
                - Decimal(str(gateway_before["current_spend_usd"])),
            ),
            "f",
        )
    trials = scored._find_trials(jobs_root, challenges)
    challenge: LoadedChallenge = challenges[0]
    artifacts = trials[challenge_id]
    result = scored._extract_challenge_receipt(
        inputs,  # type: ignore[arg-type]
        challenge,
        artifacts.trial_dir,
        artifacts.result,
        artifacts.trajectory,
        {
            "source_commit": corpus.source_commit,
            "source_tree_sha256": corpus.tree_sha256,
            "fixture_id": None,
            "graph_arm": "fresh" if configured is not None else None,
            "artifact_sha256": (
                inputs.artifact_sha256.lower()
                if configured is not None and inputs.artifact_sha256
                else None
            ),
        },
        artifacts.trajectory_error,
        step_root=artifacts.step_root,
        step_results=artifacts.step_results,
        memory_marker=None,
        allow_local_git_history=stage.history is not None,
    )
    raw_result = scored._parse_raw_result(artifacts.step_root / "agent" / "claude-code.txt")
    raw_init = scored._parse_raw_init(artifacts.step_root / "agent" / "claude-code.txt")
    resolved_models = sorted(
        str(name)
        for name in (raw_result.get("modelUsage", {}) if isinstance(raw_result, Mapping) else {})
        if isinstance(name, str)
    )
    receipt = {
        "schema_version": 1,
        "session_number": session_number,
        "run_id": session_id,
        "condition": inputs.condition,
        "model": inputs.model,
        "resolved_model": (
            raw_init.get("model")
            if isinstance(raw_init, Mapping) and isinstance(raw_init.get("model"), str)
            else resolved_models[0]
            if len(resolved_models) == 1
            else None
        ),
        "models_used": resolved_models,
        "max_turns": inputs.max_turns,
        "requested_budget_cap_usd": inputs.max_budget_usd,
        "harbor_exit_code": harbor_exit,
        "harbor_wall_seconds": harbor_wall,
        "source_commit": corpus.source_commit,
        "source_tree_sha256": corpus.tree_sha256,
        "source_base_tree": corpus.history.head_tree if corpus.history is not None else None,
        "source_history": asdict(corpus.history) if corpus.history is not None else None,
        "gateway_billing": {
            "before": gateway_before,
            "after": gateway_after,
            "delta_usd": gateway_delta,
            "source": "vercel-ai-gateway-quota" if gateway_delta is not None else None,
            "exclusive_to_interval": bool(
                isinstance(exclusive_interval, Mapping)
                and exclusive_interval.get("exclusive") is True
                and gateway_before is not None
                and gateway_after is not None
                and gateway_before.get("key_fingerprint")
                == gateway_after.get("key_fingerprint")
                == exclusive_interval.get("key_fingerprint")
            ),
            "exclusivity": exclusive_interval,
        },
        "challenge": asdict(result),
    }
    for task_directory in pack.task_directories:
        audit_effective_context(
            task_directory,
            _HISTORY_AUDIT_CONFIG if stage.history is not None else BlindnessAuditConfig(),
        )
    return _SessionRun(
        receipt=receipt,
        trial_dir=artifacts.trial_dir,
        step_root=artifacts.step_root,
        trajectory=artifacts.trajectory,
        source_commit=corpus.source_commit,
        source_tree_sha256=corpus.tree_sha256,
        source_base_tree=corpus.history.head_tree if corpus.history is not None else None,
        history=corpus.history,
    )


def _inspect_graph(inputs: CompoundingInputs) -> dict[str, object]:
    assert inputs.provisioner_executable is not None
    response = support._private_command(
        inputs.provisioner_executable.resolve(strict=True),
        "inspect",
        {"run_id": inputs.chain_id},
        timeout_seconds=inputs.timeout_seconds,
    )
    if response.get("ok") is not True or not isinstance(response.get("projects"), list):
        raise CompoundingRunError("private_inspect_invalid_response")
    projects = response["projects"]
    return {
        "projects": projects,
        "project_count": len(projects),
        "sha256": _sha256_json(projects),
    }


def _created_binding(session: _SessionRun) -> ActiveProjectBinding:
    path = session.step_root / "agent" / CREATED_PROJECT_REPORT_NAME
    value = scored._safe_json(path)
    binding = value.get("binding")
    if not isinstance(binding, dict):
        raise CompoundingRunError("created_project_binding_was_not_retained")
    parsed = ActiveProjectBinding.from_json_bytes(json.dumps(binding, sort_keys=True).encode())
    if value.get("project_id") != parsed.project_id or value.get("slug") != parsed.slug:
        raise CompoundingRunError("created_project_binding_disagrees_with_report")
    return parsed


def _agent_nodes(snapshot: Mapping[str, object]) -> list[Mapping[str, object]]:
    projects = snapshot.get("projects")
    if not isinstance(projects, list) or len(projects) != 1:
        return []
    project = projects[0]
    if not isinstance(project, Mapping) or not isinstance(project.get("agent_nodes"), list):
        return []
    return [node for node in project["agent_nodes"] if isinstance(node, Mapping)]


def _graph_intake(snapshot: Mapping[str, object]) -> dict[str, object]:
    nodes = _agent_nodes(snapshot)
    relevant_ids: list[str] = []
    for node in nodes:
        if node.get("status") not in {"active", "open", "in_progress"}:
            continue
        text = json.dumps(node, ensure_ascii=False)
        has_live = LIVE_IMPLEMENTATION_MARKER in text or "ProfileSelectionPopup" in text
        has_alternative = any(marker in text for marker in ALTERNATIVE_MARKERS)
        if has_live and has_alternative and isinstance(node.get("short_id"), str):
            relevant_ids.append(node["short_id"])
    return {
        "written": bool(nodes),
        "agent_node_count": len(nodes),
        "relevant": bool(relevant_ids),
        "relevant_node_ids": sorted(relevant_ids),
    }


def _node_map(snapshot: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    return {
        str(node["short_id"]): node
        for node in _agent_nodes(snapshot)
        if isinstance(node.get("short_id"), str)
    }


def _node_version(node: Mapping[str, object]) -> int | None:
    value = node.get("version")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _node_receipt(node: Mapping[str, object]) -> dict[str, object]:
    return {
        "short_id": node.get("short_id"),
        "type": node.get("type"),
        "status": node.get("status"),
        "version": _node_version(node),
        "sha256": _sha256_json(node),
    }


def _graph_delta(before: Mapping[str, object], after: Mapping[str, object]) -> dict[str, object]:
    prior = _node_map(before)
    current = _node_map(after)
    created = [_node_receipt(current[node_id]) for node_id in sorted(current.keys() - prior.keys())]
    updated = [
        _node_receipt(current[node_id])
        for node_id in sorted(current.keys() & prior.keys())
        if _sha256_json(current[node_id]) != _sha256_json(prior[node_id])
    ]
    return {
        "written": bool(created or updated),
        "created": created,
        "updated": updated,
        "created_count": len(created),
        "updated_count": len(updated),
        "active_count": sum(
            node.get("status") in {"active", "open", "in_progress"} for node in current.values()
        ),
        "archived_count": sum(node.get("status") == "archived" for node in current.values()),
    }


def _session_events(root: Path) -> list[object]:
    events: list[object] = []
    for path in sorted((root / "agent" / "sessions").rglob("*.jsonl")):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > scored._MAX_ARTIFACT_BYTES
        ):
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _is_stele_read(name: str) -> bool:
    normalized = name.casefold().replace("-", "_")
    if "stele" not in normalized:
        return False
    tool = normalized.rsplit("__", 1)[-1]
    return any(tool == prefix or tool.startswith(f"{prefix}_") for prefix in _STELE_READ_TOOLS)


def _stele_reads(
    trajectory: Mapping[str, Any] | None, relevant_ids: Sequence[str] = ()
) -> dict[str, object]:
    if trajectory is None:
        return {
            "count": 0,
            "tools": [],
            "matching_count": 0,
            "matching_tools": [],
            "matching_node_ids": [],
        }
    names: list[str] = []
    matching: list[str] = []
    matching_node_ids: set[str] = set()
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        return {
            "count": 0,
            "tools": [],
            "matching_count": 0,
            "matching_tools": [],
            "matching_node_ids": [],
        }
    for step in steps:
        if not isinstance(step, Mapping) or not isinstance(step.get("tool_calls"), list):
            continue
        observation = step.get("observation")
        results = observation.get("results") if isinstance(observation, Mapping) else None
        by_call = {
            result.get("source_call_id"): result
            for result in results or []
            if isinstance(result, Mapping) and isinstance(result.get("source_call_id"), str)
        }
        for call in step["tool_calls"]:
            if not isinstance(call, Mapping):
                continue
            name = call.get("function_name")
            if not isinstance(name, str) or not _is_stele_read(name):
                continue
            names.append(name)
            result = by_call.get(call.get("tool_call_id"))
            serialized = json.dumps(result, ensure_ascii=False) if result is not None else ""
            matched_ids = {short_id for short_id in relevant_ids if short_id in serialized}
            if matched_ids:
                matching.append(name)
                matching_node_ids.update(matched_ids)
    return {
        "count": len(names),
        "tools": names,
        "matching_count": len(matching),
        "matching_tools": matching,
        "matching_node_ids": sorted(matching_node_ids),
    }


def _prior_version_delivery(
    session: _SessionRun,
    before_snapshot: Mapping[str, object],
    prior_versions: Mapping[str, set[int | None]],
) -> dict[str, object]:
    events = _session_events(session.step_root)
    injection = detect_stele_delivery_in_session(events).as_dict()
    prior_ids = sorted(prior_versions)
    reads = _stele_reads(session.trajectory, prior_ids)
    delivered_ids = {value for value in injection["node_ids"] if isinstance(value, str)} | {
        value for value in reads["matching_node_ids"] if isinstance(value, str)
    }
    current = _node_map(before_snapshot)
    exact: list[dict[str, object]] = []
    for node_id in sorted(delivered_ids):
        node = current.get(node_id)
        if node is None:
            continue
        version = _node_version(node)
        if version in prior_versions.get(node_id, set()):
            exact.append({"short_id": node_id, "version": version})
    return {
        "injection": injection,
        "explicit_reads": reads,
        "delivered_prior_node": bool(exact),
        "exact_prior_versions": exact,
    }


def _relevant_node_injected(events: Sequence[object], relevant_ids: Sequence[str]) -> bool:
    for event in events:
        if not isinstance(event, Mapping):
            continue
        attachment = event.get("attachment")
        if (
            not isinstance(attachment, Mapping)
            or attachment.get("type") != "hook_additional_context"
        ):
            continue
        content = attachment.get("content")
        pieces = content if isinstance(content, list) else [content]
        for piece in pieces:
            if (
                isinstance(piece, str)
                and LIVE_IMPLEMENTATION_MARKER in piece
                and any(short_id in piece for short_id in relevant_ids)
            ):
                return True
    return False


def _first_live_edit(trajectory: Mapping[str, Any] | None) -> dict[str, int | None]:
    if trajectory is None:
        return {"tool_call_index": None, "investigative_calls_before": None}
    calls = scored._tool_calls(trajectory)
    investigative = 0
    for index, call in enumerate(calls, 1):
        name = call.get("function_name")
        arguments = call.get("arguments")
        normalized = name.casefold().replace("-", "_") if isinstance(name, str) else ""
        serialized = json.dumps(arguments, sort_keys=True) if isinstance(arguments, Mapping) else ""
        if LIVE_IMPLEMENTATION_MARKER in serialized and any(
            token in normalized for token in ("edit", "write")
        ):
            return {
                "tool_call_index": index,
                "investigative_calls_before": investigative,
            }
        if normalized in scored._INVESTIGATION_TOOLS:
            investigative += 1
    return {"tool_call_index": None, "investigative_calls_before": investigative}


def _session_identity(trajectory: Mapping[str, Any] | None) -> str | None:
    value = trajectory.get("session_id") if trajectory is not None else None
    return value if isinstance(value, str) and value else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _cumulative(sessions: Sequence[_SessionRun]) -> dict[str, object]:
    totals: dict[str, float] = {}
    token_totals: dict[str, int] = {}
    for session in sessions:
        challenge = session.receipt["challenge"]
        cost = challenge.get("cost") if isinstance(challenge, Mapping) else None
        timings = challenge.get("timings") if isinstance(challenge, Mapping) else None
        tokens = challenge.get("tokens") if isinstance(challenge, Mapping) else None
        for output, field in (
            ("claude_reported_usd", "claude_reported_amount_usd"),
            ("gateway_rate_cross_check_usd", "gateway_rate_cross_check_usd"),
        ):
            amount = _number(cost.get(field)) if isinstance(cost, Mapping) else None
            if amount is not None:
                totals[output] = totals.get(output, 0.0) + amount
        wall = _number(timings.get("invocation_wall")) if isinstance(timings, Mapping) else None
        if wall is not None:
            totals["agent_invocation_wall_seconds"] = (
                totals.get("agent_invocation_wall_seconds", 0.0) + wall
            )
        if isinstance(tokens, Mapping):
            for name in ("input", "output", "cache_read", "cache_write"):
                value = tokens.get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    token_totals[name] = token_totals.get(name, 0) + value
    totals["outer_harbor_wall_seconds"] = sum(
        float(session.receipt["harbor_wall_seconds"]) for session in sessions
    )
    gateway_deltas = [
        session.receipt.get("gateway_billing", {}).get("delta_usd") for session in sessions
    ]
    if all(isinstance(value, str) for value in gateway_deltas):
        totals["gateway_observed_usd"] = float(sum(Decimal(value) for value in gateway_deltas))
    return {**totals, "tokens": token_totals}


def run_compounding_chain(inputs: CompoundingInputs) -> dict[str, object]:
    chain = _chain_spec(inputs)
    has_stele = inputs.condition == "stele-fresh"
    cleanup_completed = not has_stele
    previous_attempt_root = os.environ.get("STELE_BENCH_ATTEMPT_ROOT")
    before_snapshots: list[dict[str, object] | None] = []
    after_snapshots: list[dict[str, object] | None] = []
    sessions: list[_SessionRun] = []
    binding: ActiveProjectBinding | None = None
    primary_error: BaseException | None = None
    if has_stele:
        os.environ["STELE_BENCH_ATTEMPT_ROOT"] = os.fspath(inputs.attempt_root.resolve())
    try:
        binding_path: Path | None = None
        if has_stele:
            assert inputs.provisioner_executable is not None
            prepared = support._private_command(
                inputs.provisioner_executable.resolve(strict=True),
                "prepare",
                {"run_id": inputs.chain_id, "harnesses": ["claude"]},
                timeout_seconds=inputs.timeout_seconds,
            )
            if prepared.get("ok") is not True:
                raise CompoundingRunError("private_prepare_invalid_response")
            initial = _inspect_graph(inputs)
            if initial["project_count"] != 0:
                raise CompoundingRunError("fresh_chain_started_with_a_project")
            before_snapshots.append(initial)

        for session_number in range(1, len(chain.sessions) + 1):
            if has_stele and session_number > 1:
                before = _inspect_graph(inputs)
                prior_after = after_snapshots[-1]
                if prior_after is None or before["sha256"] != prior_after["sha256"]:
                    raise CompoundingRunError("graph_changed_between_sessions")
                before_snapshots.append(before)
            elif not has_stele:
                before_snapshots.append(None)

            session = _run_session(
                inputs,
                session_number=session_number,
                binding_path=binding_path,
            )
            sessions.append(session)
            if has_stele:
                after = _inspect_graph(inputs)
                after_snapshots.append(after)
                if session_number == 1:
                    binding = _created_binding(session)
                    observed = [
                        project
                        for project in after["projects"]
                        if isinstance(project, Mapping)
                        and project.get("project_id") == binding.project_id
                    ]
                    if len(observed) != 1:
                        raise CompoundingRunError("created_project_was_not_observed")
                    binding_path = (
                        support._attempt_directory(inputs.attempt_root, inputs.chain_id)
                        / "project.json"
                    )
                    support._atomic_write_json(binding_path, binding.as_cache_object())
            else:
                after_snapshots.append(None)
    except BaseException as error:
        primary_error = error
    finally:
        if has_stele and inputs.provisioner_executable is not None:
            try:
                cleaned = support._private_command(
                    inputs.provisioner_executable.resolve(strict=True),
                    "cleanup",
                    {"run_id": inputs.chain_id},
                    timeout_seconds=inputs.timeout_seconds,
                )
                cleanup_completed = cleaned == {"ok": True}
                if not cleanup_completed:
                    primary_error = primary_error or CompoundingRunError(
                        "private_cleanup_invalid_response"
                    )
            except BaseException as error:
                primary_error = primary_error or error
        if previous_attempt_root is None:
            os.environ.pop("STELE_BENCH_ATTEMPT_ROOT", None)
        else:
            os.environ["STELE_BENCH_ATTEMPT_ROOT"] = previous_attempt_root

    if len(sessions) != len(chain.sessions):
        code = getattr(primary_error, "code", None) or (
            primary_error.__class__.__name__ if primary_error is not None else "incomplete_chain"
        )
        raise CompoundingRunError(str(code)) from primary_error

    identities = [_session_identity(session.trajectory) for session in sessions]
    distinct_conversations = bool(all(identities) and len(set(identities)) == len(identities))
    source_boundaries = [
        session.source_commit == stage.base_revision
        and (
            stage.history is None
            or (session.history == stage.history and session.source_base_tree == stage.base_tree)
        )
        for stage, session in zip(chain.sessions, sessions, strict=True)
    ]
    source_reset = all(source_boundaries)
    graph_boundaries = (
        [
            before_snapshots[index] is not None
            and after_snapshots[index - 1] is not None
            and before_snapshots[index]["sha256"] == after_snapshots[index - 1]["sha256"]
            for index in range(1, len(sessions))
        ]
        if has_stele
        else []
    )
    graph_unchanged = all(graph_boundaries)

    graph_activity: list[dict[str, object]] | None = None
    memory_delivery: list[dict[str, object]] | None = None
    if has_stele:
        graph_activity = []
        memory_delivery = []
        prior_versions: dict[str, set[int | None]] = {}
        for index, session in enumerate(sessions):
            before = before_snapshots[index] or {}
            after = after_snapshots[index] or {}
            delivery = _prior_version_delivery(session, before, prior_versions)
            delivery["session_number"] = index + 1
            memory_delivery.append(delivery)
            delta = _graph_delta(before, after)
            delta["session_number"] = index + 1
            graph_activity.append(delta)
            for group in (delta["created"], delta["updated"]):
                for node in group:
                    if not isinstance(node, Mapping) or not isinstance(node.get("short_id"), str):
                        continue
                    prior_versions.setdefault(node["short_id"], set()).add(
                        node.get("version") if isinstance(node.get("version"), int) else None
                    )

    external_access = []
    pinned_models = []
    rewards = []
    for session in sessions:
        challenge = session.receipt.get("challenge")
        evidence = challenge.get("evidence") if isinstance(challenge, Mapping) else None
        external_access.append(
            bool(
                isinstance(evidence, Mapping)
                and (
                    evidence.get("external_source_access_detected") is True
                    or evidence.get("forbidden_repository_access_detected") is True
                )
            )
        )
        pinned_models.append(session.receipt.get("resolved_model") == inputs.model)
        rewards.append(challenge.get("reward") if isinstance(challenge, Mapping) else None)

    boundary = {
        "before_sessions": before_snapshots,
        "after_sessions": after_snapshots,
        "binding_sha256": (
            _sha256_json(binding.as_cache_object()) if binding is not None else None
        ),
        "graph_unchanged_between_sessions": graph_unchanged,
        "graph_boundary_checks": graph_boundaries,
        "source_reset_from_registered_bases": source_reset,
        "source_boundary_checks": source_boundaries,
        "distinct_conversations": distinct_conversations,
        "session_ids_sha256": [
            hashlib.sha256(value.encode()).hexdigest() if value else None for value in identities
        ],
        "no_external_source_access": not any(external_access),
        "external_source_access_by_session": external_access,
        "pinned_model_every_session": all(pinned_models),
        "pinned_model_checks": pinned_models,
        "only_graph_crossed_boundary": (
            source_reset
            and distinct_conversations
            and not any(external_access)
            and all(pinned_models)
            and (not has_stele or (graph_unchanged and binding is not None))
        ),
    }
    outcome = "completed"
    if primary_error is not None or not boundary["only_graph_crossed_boundary"]:
        outcome = "invalid"
    cumulative_prefixes = [
        {"k": index, **_cumulative(sessions[:index])} for index in range(1, len(sessions) + 1)
    ]
    receipt: dict[str, object] = {
        "schema_version": 2,
        "chain_id": inputs.chain_id,
        "experiment_id": chain.experiment_id,
        "chain_manifest": (
            {
                "path": os.fspath(chain.manifest_path),
                "sha256": chain.manifest_sha256,
            }
            if chain.manifest_path is not None
            else None
        ),
        "condition": inputs.condition,
        "model": inputs.model,
        "outcome": outcome,
        "cleanup_completed": cleanup_completed,
        "session_count": len(sessions),
        "sessions": [session.receipt for session in sessions],
        "rewards": rewards,
        "graph_activity": graph_activity,
        "memory_delivery": memory_delivery,
        "boundary": boundary,
        "cumulative_prefixes": cumulative_prefixes,
        "cumulative": cumulative_prefixes[-1],
        "error": (
            getattr(primary_error, "code", None)
            or (primary_error.__class__.__name__ if primary_error is not None else None)
        ),
    }
    if len(sessions) == 2 and chain.manifest_path is None:
        session_1, session_2 = sessions
        session_1_response = (
            session_1.receipt["challenge"].get("evidence", {}).get("response_evidence")
        )
        legacy_intake = _graph_intake(after_snapshots[0] or {}) if has_stele else None
        legacy_delivery = None
        if has_stele:
            assert legacy_intake is not None
            relevant_ids = [
                value for value in legacy_intake["relevant_node_ids"] if isinstance(value, str)
            ]
            events = _session_events(session_2.step_root)
            injection = detect_injection_in_session(events, LIVE_IMPLEMENTATION_MARKER).as_dict()
            injection["relevant_node_injected"] = _relevant_node_injected(events, relevant_ids)
            reads = _stele_reads(session_2.trajectory, relevant_ids)
            legacy_intake["deliverable"] = bool(
                injection["relevant_node_injected"] or reads["matching_count"]
            )
            legacy_delivery = {"injection": injection, "explicit_reads": reads}
        receipt.update(
            {
                "session_1": session_1.receipt,
                "session_2": session_2.receipt,
                "session_1_correct": bool(
                    isinstance(session_1_response, Mapping)
                    and session_1_response.get("cited") is True
                ),
                "session_2_reward": rewards[1],
                "graph_intake": legacy_intake,
                "memory_delivery": legacy_delivery,
                "first_live_edit": _first_live_edit(session_2.trajectory),
            }
        )
        boundary.update(
            {
                "before_session_1": before_snapshots[0],
                "after_session_1": after_snapshots[0],
                "before_session_2": before_snapshots[1],
                "after_session_2": after_snapshots[1],
                "source_reset_from_pinned_commit": source_reset,
            }
        )
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("revision", nargs="?")
    parser.add_argument("--chain-manifest", type=Path)
    parser.add_argument("--condition", choices=_CONDITIONS, required=True)
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--chain-id", required=True)
    parser.add_argument(
        "--provisioner", type=Path, default=Path(__file__).with_name("provision.py")
    )
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
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-turns", type=int, default=45)
    parser.add_argument("--max-budget-usd", default="10.00")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--gateway-reserve-config", type=Path)
    parser.add_argument("--receipt-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = CompoundingInputs(
            repository=args.repository,
            revision=args.revision,
            attempt_root=args.attempt_root,
            work_root=args.work_root,
            chain_id=args.chain_id,
            condition=args.condition,
            provisioner_executable=args.provisioner,
            artifact_path=args.artifact,
            artifact_sha256=args.artifact_sha256,
            backend_url=args.backend_url,
            backend_anon_key=args.backend_anon_key,
            artifact_source_kind=args.artifact_source_kind,
            artifact_release_version=args.artifact_release_version,
            model=args.model,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
            timeout_seconds=args.timeout_seconds,
            gateway_reserve_config=args.gateway_reserve_config,
            chain_manifest_path=args.chain_manifest,
        )
        with support._controlled_termination_signals():
            receipt = run_compounding_chain(inputs)
    except (CompoundingRunError, support.RunnerInterrupted, OSError, ValueError) as error:
        envelope: dict[str, object] = {
            "ok": False,
            "error": getattr(error, "code", None) or error.__class__.__name__,
        }
        exit_code = 1
    else:
        envelope = {"ok": receipt.get("outcome") == "completed", "receipt": receipt}
        exit_code = 0 if envelope["ok"] else 1
    if args.receipt_out is not None:
        support._atomic_write_json(args.receipt_out, envelope)
    print(json.dumps(envelope, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
