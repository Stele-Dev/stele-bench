from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

from stele_bench.challenges import LoadedChallenge
from stele_bench.contracts import (
    ArtifactDescriptor,
    ChallengeManifestV1,
    ScriptVerifierV1,
)

MODULE_PATH = Path(__file__).with_name("run_scored_smoke.py")
SPEC = importlib.util.spec_from_file_location("run_scored_smoke", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
scored = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scored
SPEC.loader.exec_module(scored)


def _inputs(tmp_path: Path, *, condition: str = "faithful", **overrides: object) -> object:
    values: dict[str, object] = {
        "repository": tmp_path,
        "revision": "a" * 40,
        "fixture_path": tmp_path / "fixture.json",
        "provisioner_executable": tmp_path / "provision.py",
        "attempt_root": tmp_path / "attempt",
        "work_root": tmp_path / "work",
        "run_id": "canary-one",
        "condition": condition,
        "artifact_path": tmp_path / "stele",
        "artifact_sha256": "0" * 64,
        "backend_url": "http://host.docker.internal:54321",
        "backend_anon_key": "public-key",
    }
    values.update(overrides)
    return scored.ScoredInputs(**values)


def _challenge(tmp_path: Path, challenge_id: str) -> LoadedChallenge:
    directory = tmp_path / challenge_id
    directory.mkdir()
    verifier = directory / "verifier.sh"
    verifier.write_text("#!/bin/sh\n", encoding="utf-8")
    digest = scored._sha256(verifier)
    manifest = ChallengeManifestV1(
        challenge_id=challenge_id,
        title=challenge_id,
        description="neutral task",
        prompt="Do the requested work.\n",
        allowed_output_paths=("/workspace/output",),
        verifier=ScriptVerifierV1(
            kind="script",
            runtime="posix-shell",
            asset=ArtifactDescriptor(
                path="verifier.sh",
                sha256=digest,
                media_type="text/x-shellscript",
            ),
        ),
    )
    (directory / "challenge.json").write_text(
        json.dumps(manifest.model_dump(mode="json")), encoding="utf-8"
    )
    return LoadedChallenge(manifest=manifest, directory=directory, verifier_path=verifier)


def _valid_trajectory(*tool_calls: dict[str, object]) -> dict[str, object]:
    steps: list[dict[str, object]] = [
        {
            "step_id": 1,
            "timestamp": "2026-07-23T10:00:00+00:00",
            "source": "user",
            "message": "Investigate the local source tree.",
        }
    ]
    if tool_calls:
        calls: list[dict[str, object]] = []
        results: list[dict[str, object]] = []
        for index, tool_call in enumerate(tool_calls, start=1):
            call = dict(tool_call)
            call_id = str(call.setdefault("tool_call_id", f"call-{index}"))
            calls.append(call)
            results.append({"source_call_id": call_id, "content": "ok"})
        steps.append(
            {
                "step_id": 2,
                "timestamp": "2026-07-23T10:00:01+00:00",
                "source": "agent",
                "message": "",
                "tool_calls": calls,
                "observation": {"results": results},
            }
        )
    else:
        steps.append(
            {
                "step_id": 2,
                "timestamp": "2026-07-23T10:00:01+00:00",
                "source": "agent",
                "message": "Investigation complete.",
            }
        )
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "session-one",
        "agent": {"name": "claude-code"},
        "steps": steps,
    }


def test_scored_config_templates_secret_and_pins_budget(tmp_path: Path) -> None:
    config = tmp_path / "route-two.json"
    config.write_text(json.dumps({"agents": [{"kwargs": {}, "env": {}}]}))

    scored._configure_scored_job(config, _inputs(tmp_path))

    data = json.loads(config.read_text())
    agent = data["agents"][0]
    assert agent["model_name"] == "anthropic/claude-haiku-4.5"
    assert agent["kwargs"] == {
        "max_turns": 3,
        "max_budget_usd": "0.10",
        "permission_mode": "bypassPermissions",
    }
    assert agent["env"]["ANTHROPIC_AUTH_TOKEN"] == "${AI_GATEWAY_KEY_EVAL}"
    assert agent["env"]["ANTHROPIC_API_KEY"] == ""
    assert "public-key" not in config.read_text()


def test_faithful_harbor_run_starts_bridge_and_injects_container_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "route-two.json"
    config.write_text(json.dumps({"agents": [{"kwargs": {}, "env": {}}]}))
    inputs = _inputs(tmp_path)
    observed: dict[str, object] = {}

    class FakeBridge:
        container_endpoint = "http://host.docker.internal:45678/api/semantic-seeds"

        def __enter__(self) -> object:
            observed["entered"] = True
            return self

        def __exit__(self, *args: object) -> None:
            observed["exited"] = True

    def fake_start(bridge_config: object) -> FakeBridge:
        observed["config"] = bridge_config
        return FakeBridge()

    def fake_harbor(*args: object) -> tuple[int, float]:
        observed["harbor"] = True
        assert observed.get("entered") is True
        data = json.loads(config.read_text())
        assert data["agents"][0]["env"]["STELE_SEMANTIC_ENDPOINT"] == (
            "http://host.docker.internal:45678/api/semantic-seeds"
        )
        return 0, 1.25

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    monkeypatch.setattr(scored, "start_semantic_bridge", fake_start)
    monkeypatch.setattr(scored, "_run_harbor", fake_harbor)

    assert scored._run_configured_harbor(inputs, config, tmp_path / "jobs") == (0, 1.25)
    bridge_config = observed["config"]
    assert bridge_config.gateway_key == "gateway-secret"
    assert bridge_config.supabase_url == "http://host.docker.internal:54321"
    assert bridge_config.supabase_anon_key == "public-key"
    assert bridge_config.expected_token_count == 1
    assert observed["entered"] is True
    assert observed["exited"] is True
    assert observed["harbor"] is True


def test_no_stele_harbor_run_does_not_start_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "route-one.json"
    config.write_text(json.dumps({"agents": [{"kwargs": {}, "env": {}}]}))
    inputs = _inputs(tmp_path, condition="no-stele")
    monkeypatch.setattr(
        scored,
        "start_semantic_bridge",
        lambda *args, **kwargs: pytest.fail("no-stele must not start semantic bridge"),
    )
    monkeypatch.setattr(scored, "_run_harbor", lambda *args: (0, 0.5))

    assert scored._run_configured_harbor(inputs, config, tmp_path / "jobs") == (0, 0.5)
    data = json.loads(config.read_text())
    assert "STELE_SEMANTIC_ENDPOINT" not in data["agents"][0]["env"]


def test_scored_harbor_uses_minimal_gateway_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "route.json"
    config.write_text("{}")
    inputs = _inputs(tmp_path, condition="no-stele")
    observed: dict[str, object] = {}

    def fake_command(argv: tuple[str, ...], **kwargs: object) -> object:
        observed["argv"] = argv
        observed.update(kwargs)
        return scored.support.CommandObservation(exit_code=0, wall_seconds=0.25)

    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    monkeypatch.setattr(scored.support, "_managed_harbor_command", fake_command)

    assert scored._run_harbor(inputs, config, tmp_path / "jobs") == (0, 0.25)
    assert observed["include_gateway"] is True
    assert observed["diagnostic_path"] == tmp_path / "harbor.diagnostic.json"


def test_faithful_multi_challenge_is_rejected_before_setup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one challenge"):
        _inputs(tmp_path, challenge_ids=("one", "two"))


def test_no_stele_inputs_require_no_graph_or_artifact_values(tmp_path: Path) -> None:
    inputs = scored.ScoredInputs(
        repository=tmp_path,
        revision="a" * 40,
        attempt_root=tmp_path / "attempt",
        work_root=tmp_path / "work",
        run_id="control-one",
        condition="no-stele",
        challenge_ids=("input-keypress-architecture", "package-cache-is-empty"),
    )

    assert inputs.fixture_path is None
    assert inputs.artifact_path is None
    assert inputs.challenge_ids == (
        "input-keypress-architecture",
        "package-cache-is-empty",
    )


def test_faithful_inputs_require_every_graph_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="faithful condition requires"):
        scored.ScoredInputs(
            repository=tmp_path,
            revision="a" * 40,
            attempt_root=tmp_path / "attempt",
            work_root=tmp_path / "work",
            run_id="faithful-one",
        )


def test_challenge_selection_defaults_and_preserves_repeat_order(tmp_path: Path) -> None:
    parser = scored._parser()
    common = [
        str(tmp_path),
        "a" * 40,
        "--attempt-root",
        str(tmp_path / "attempt"),
        "--work-root",
        str(tmp_path / "work"),
        "--run-id",
        "run-one",
        "--condition",
        "no-stele",
    ]

    default = parser.parse_args(common)
    ordered = parser.parse_args(
        [
            *common,
            "--challenge",
            "package-cache-is-empty",
            "--challenge",
            "input-keypress-architecture",
        ]
    )

    assert tuple(default.challenges or scored.DEFAULT_CHALLENGES) == ("service-readiness",)
    assert tuple(ordered.challenges) == (
        "package-cache-is-empty",
        "input-keypress-architecture",
    )


def test_duplicate_challenge_selection_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not contain duplicates"):
        _inputs(
            tmp_path,
            challenge_ids=("service-readiness", "service-readiness"),
        )


def _write_trial(root: Path, challenge_id: str, suffix: str) -> None:
    trial = root / f"{challenge_id}-route" / f"{challenge_id}__{suffix}"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"task_name": f"local/{challenge_id}"}), encoding="utf-8"
    )
    (trial / "agent" / "trajectory.json").write_text("{}", encoding="utf-8")


def _write_multistep_trial(
    root: Path, challenge_id: str, suffix: str, *, marker: str | None
) -> Path:
    """Write a resume_trajectory trial: two steps under steps/<name>/{agent,verifier}."""
    trial = root / f"{challenge_id}-route" / f"{challenge_id}__{suffix}"
    trial.mkdir(parents=True)
    trial.joinpath("result.json").write_text(
        json.dumps(
            {
                "task_name": f"local/{challenge_id}",
                "verifier_result": {"rewards": {"reward": 0.0}},
                "agent_result": None,
                "step_results": [
                    {
                        "step_name": "establish",
                        "agent_result": {
                            "n_input_tokens": 100,
                            "n_cache_tokens": 40,
                            "n_output_tokens": 5,
                            "cost_usd": 0.10,
                        },
                        "verifier_result": {"rewards": {"reward": 0.0}},
                    },
                    {
                        "step_name": "follow-up",
                        "agent_result": {
                            "n_input_tokens": 200,
                            "n_cache_tokens": 80,
                            "n_output_tokens": 9,
                            "cost_usd": 0.20,
                        },
                        "verifier_result": {"rewards": {"reward": 0.0}},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    for step in ("establish", "follow-up"):
        agent = trial / "steps" / step / "agent"
        agent.mkdir(parents=True)
        (agent / "trajectory.json").write_text(json.dumps(_valid_trajectory()), encoding="utf-8")
        (trial / "steps" / step / "verifier").mkdir(parents=True)
    sessions = trial / "steps" / "follow-up" / "agent" / "sessions" / "projects" / "-workspace"
    sessions.mkdir(parents=True)
    lines: list[str] = []
    if marker is not None:
        lines.append(
            json.dumps(
                {
                    "type": "attachment",
                    "attachment": {
                        "type": "hook_additional_context",
                        "hookName": "PostToolUse:Edit",
                        "content": [
                            f"Stele — a lesson: {marker}\n<!-- stele:nudge recall-file -->"
                        ],
                    },
                }
            )
        )
    lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant"}}))
    (sessions / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return trial


def test_scored_step_root_selects_graded_step_and_reports_step_results(tmp_path: Path) -> None:
    trial = _write_multistep_trial(tmp_path, "sbr", "aaa", marker=None)
    result = json.loads((trial / "result.json").read_text())

    step_root, step_results = scored._scored_step_root(trial, result)

    assert step_root == trial / "steps" / "follow-up"
    assert step_results is not None and len(step_results) == 2


def test_scored_step_root_falls_back_for_single_step_and_unsafe_name(tmp_path: Path) -> None:
    single = scored._scored_step_root(tmp_path, {"task_name": "local/x"})
    assert single == (tmp_path, None)

    unsafe = scored._scored_step_root(tmp_path, {"step_results": [{"step_name": "../escape"}]})
    # A traversal step name must not resolve outside steps/: fail closed to the
    # (absent) single-step location so the trajectory reads as missing.
    assert unsafe[0] == tmp_path
    assert unsafe[1] is not None


def test_find_trials_reads_multi_step_graded_trajectory(tmp_path: Path) -> None:
    challenge = _challenge(tmp_path, "sbr")
    jobs = tmp_path / "jobs"
    _write_multistep_trial(jobs, "sbr", "aaa", marker=None)

    found = scored._find_trials(jobs, (challenge,))
    artifacts = found["sbr"]

    assert artifacts.step_root.name == "follow-up"
    assert artifacts.trajectory_error is None
    assert artifacts.trajectory is not None
    assert artifacts.step_results is not None and len(artifacts.step_results) == 2


def test_multi_step_receipt_aggregates_cost_and_populates_memory_evidence(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    challenge = _challenge(tmp_path, "sbr")
    jobs = tmp_path / "jobs"
    marker = "Close out a new dotfile candidate with a CHANGELOG entry"
    _write_multistep_trial(jobs, "sbr", "aaa", marker=marker)
    artifacts = scored._find_trials(jobs, (challenge,))["sbr"]

    receipt = scored._extract_challenge_receipt(
        inputs,
        challenge,
        artifacts.trial_dir,
        artifacts.result,
        artifacts.trajectory,
        {},
        artifacts.trajectory_error,
        step_root=artifacts.step_root,
        step_results=artifacts.step_results,
        memory_marker=marker,
    )

    # Graded step read → not invalid; both billed steps summed into the receipt.
    assert receipt.outcome == "failed"
    assert receipt.tokens["input"] == 300
    assert receipt.tokens["output"] == 14
    assert receipt.cost["claude_reported_amount_usd"] == pytest.approx(0.30)
    assert receipt.cost["billed_step_count"] == 2
    assert receipt.evidence["session_file_count"] == 1
    assert receipt.memory_evidence == {
        "marker_sha256": scored.hashlib.sha256(marker.encode()).hexdigest(),
        "injected": True,
        "channels": ["nudge:recall-file"],
        "occurrences": 1,
    }


def test_multi_step_receipt_without_marker_leaves_memory_evidence_null(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, condition="no-stele")
    challenge = _challenge(tmp_path, "sbr")
    jobs = tmp_path / "jobs"
    _write_multistep_trial(jobs, "sbr", "aaa", marker="unused")
    artifacts = scored._find_trials(jobs, (challenge,))["sbr"]

    receipt = scored._extract_challenge_receipt(
        inputs,
        challenge,
        artifacts.trial_dir,
        artifacts.result,
        artifacts.trajectory,
        {},
        artifacts.trajectory_error,
        step_root=artifacts.step_root,
        step_results=artifacts.step_results,
        memory_marker=None,
    )

    assert receipt.memory_evidence is None


def test_graph_receipt_without_marker_records_node_delivery(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, condition="faithful")
    challenge = _challenge(tmp_path, "sbr")
    jobs = tmp_path / "jobs"
    _write_multistep_trial(jobs, "sbr", "aaa", marker="KNOW-146")
    artifacts = scored._find_trials(jobs, (challenge,))["sbr"]

    receipt = scored._extract_challenge_receipt(
        inputs,
        challenge,
        artifacts.trial_dir,
        artifacts.result,
        artifacts.trajectory,
        {},
        artifacts.trajectory_error,
        step_root=artifacts.step_root,
        step_results=artifacts.step_results,
        memory_marker=None,
    )

    assert receipt.memory_evidence == {
        "injected": True,
        "channels": ["nudge:recall-file"],
        "occurrences": 1,
        "node_ids": ["KNOW-146"],
    }


def test_find_trials_maps_exactly_by_task_name_in_challenge_order(tmp_path: Path) -> None:
    first = _challenge(tmp_path, "first-task")
    second = _challenge(tmp_path, "second-task")
    jobs = tmp_path / "jobs"
    _write_trial(jobs, "second-task", "bbb")
    _write_trial(jobs, "first-task", "aaa")
    aggregate = jobs / "first-task-route" / "result.json"
    aggregate.write_text("{}", encoding="utf-8")

    found = scored._find_trials(jobs, (first, second))

    assert tuple(found) == ("first-task", "second-task")
    assert found["first-task"][1]["task_name"] == "local/first-task"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "missing_trial_result"),
        ("duplicate", "duplicate_trial_result"),
        ("extra", "unexpected_trial_result"),
        ("unnamed-extra", "unexpected_trial_result"),
    ],
)
def test_find_trials_rejects_non_bijective_results(
    tmp_path: Path, mutation: str, message: str
) -> None:
    first = _challenge(tmp_path, "first-task")
    second = _challenge(tmp_path, "second-task")
    jobs = tmp_path / "jobs"
    _write_trial(jobs, "first-task", "aaa")
    if mutation != "missing":
        _write_trial(jobs, "second-task", "bbb")
    if mutation == "duplicate":
        _write_trial(jobs, "first-task", "ccc")
    if mutation == "extra":
        _write_trial(jobs, "other-task", "ddd")
    if mutation == "unnamed-extra":
        unnamed = jobs / "unknown-route" / "unknown__ddd"
        (unnamed / "agent").mkdir(parents=True)
        (unnamed / "result.json").write_text("{}", encoding="utf-8")

    with pytest.raises(scored.ScoredSmokeError, match=message):
        scored._find_trials(jobs, (first, second))


def test_find_trials_preserves_malformed_atif_as_trial_integrity_failure(
    tmp_path: Path,
) -> None:
    challenge = _challenge(tmp_path, "first-task")
    jobs = tmp_path / "jobs"
    _write_trial(jobs, "first-task", "aaa")
    trajectory = next(jobs.rglob("trajectory.json"))
    trajectory.write_text("{not-json", encoding="utf-8")

    found = scored._find_trials(jobs, (challenge,))

    assert found["first-task"][2] is None
    assert found["first-task"][3] == "malformed_atif_trajectory"


def _two_turn_trajectory(final_prompt: str, *, before: str, after: str) -> dict[str, object]:
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "s",
        "agent": {"name": "claude-code"},
        "steps": [
            {"step_id": 1, "timestamp": "t", "source": "user", "message": "first turn"},
            {"step_id": 2, "timestamp": "t", "source": "agent", "message": before},
            {"step_id": 3, "timestamp": "t", "source": "user", "message": final_prompt},
            {"step_id": 4, "timestamp": "t", "source": "agent", "message": after},
        ],
    }


MARKERS = ("watchdog-2", "8-12%")
FINAL = "can we do this in the background?\n"


def test_response_evidence_scans_only_the_answer_to_the_final_turn() -> None:
    # The marker appearing BEFORE the final turn must not score: it would credit
    # reasoning the agent had already done, not its answer to the question.
    stale = scored._response_evidence(
        _two_turn_trajectory(FINAL, before="recall watchdog-2 here", after="sure, spawn a worker"),
        FINAL,
        MARKERS,
    )
    assert stale["cited"] is False
    assert stale["markers_found"] == []

    cited = scored._response_evidence(
        _two_turn_trajectory(
            FINAL, before="thinking", after="No - the watchdog-2 spike settled it."
        ),
        FINAL,
        MARKERS,
    )
    assert cited["cited"] is True
    assert cited["markers_found"] == ["watchdog-2"]


def test_response_evidence_normalizes_dashes_and_whitespace() -> None:
    graded = scored._response_evidence(
        _two_turn_trajectory(FINAL, before="", after="it idled at   8–12%  of a core"),
        FINAL,
        MARKERS,
    )
    assert graded["cited"] is True
    assert graded["markers_found"] == ["8-12%"]


def test_response_evidence_ignores_markdown_code_span_formatting() -> None:
    marker = "Live implementation: src/components/profile_selection_popup.rs"
    graded = scored._response_evidence(
        _two_turn_trajectory(
            FINAL,
            before="",
            after="Live implementation: `src/components/profile_selection_popup.rs`",
        ),
        FINAL,
        (marker,),
    )
    assert graded["cited"] is True
    assert graded["markers_found"] == [marker]


def test_response_evidence_is_null_without_declared_markers() -> None:
    trajectory = _two_turn_trajectory(FINAL, before="", after="x")
    assert scored._response_evidence(trajectory, FINAL, ()) is None


def test_generic_background_advice_does_not_cite() -> None:
    graded = scored._response_evidence(
        _two_turn_trajectory(
            FINAL,
            before="",
            after="Yes - spawn a background thread or a watchdog process that polls.",
        ),
        FINAL,
        MARKERS,
    )
    assert graded["cited"] is False


def test_investigation_depth_counts_only_reading_tools() -> None:
    trajectory = _valid_trajectory(
        {"function_name": "Read", "arguments": {"file_path": "/workspace/src/a.rs"}},
        {"function_name": "Grep", "arguments": {"pattern": "watch"}},
        {"function_name": "Bash", "arguments": {"command": "ls src"}},
        # Not investigative: writing the answer and a graph lookup.
        {"function_name": "Write", "arguments": {"file_path": "/workspace/answer.md"}},
        {"function_name": "mcp__plugin_stele_stele__fetch", "arguments": {"id": "KNOW-1"}},
    )

    depth = scored._investigation_depth(trajectory)

    assert depth["investigative"] == 3
    assert depth["total"] == 5
    assert depth["by_tool"] == {"read": 1, "grep": 1, "bash": 1}


def test_investigation_depth_is_null_without_a_trajectory() -> None:
    assert scored._investigation_depth(None) == {
        "total": None,
        "investigative": None,
        "by_tool": {},
        "session": None,
    }


def _resumed_trajectory(final_prompt: str) -> dict[str, object]:
    """A resumed two-turn session: turn 1 explores, the graded turn does not.

    This is the real shape a multi-step challenge produces — `resume_trajectory`
    means the graded step's trajectory carries every earlier turn with it.
    """

    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "s",
        "agent": {"name": "claude-code"},
        "steps": [
            {"step_id": 1, "timestamp": "t", "source": "user", "message": "first turn"},
            {
                "step_id": 2,
                "timestamp": "t",
                "source": "agent",
                "message": "exploring",
                "tool_calls": [
                    {"function_name": "Read", "arguments": {"file_path": "/workspace/a.rs"}},
                    {"function_name": "Bash", "arguments": {"command": "ls src"}},
                    {"function_name": "Grep", "arguments": {"pattern": "drift"}},
                ],
            },
            {"step_id": 3, "timestamp": "t", "source": "user", "message": final_prompt},
            {"step_id": 4, "timestamp": "t", "source": "agent", "message": "answering"},
        ],
    }


def test_investigation_depth_does_not_count_a_resumed_turn_as_the_graded_turn() -> None:
    """The graded turn here is toolless; only the session scope saw the digging.

    Counting the whole resumed trajectory and calling it "the graded turn"
    reports turn-1 exploration — identical work in every arm — as if it were
    the measurement.
    """

    depth = scored._investigation_depth(_resumed_trajectory(FINAL), FINAL)

    assert depth["investigative"] == 0
    assert depth["total"] == 0
    assert depth["scanned_from"] == 3
    assert depth["session"]["investigative"] == 3
    assert depth["session"]["by_tool"] == {"read": 1, "bash": 1, "grep": 1}


def test_investigation_depth_counts_work_done_in_the_graded_turn() -> None:
    trajectory = _resumed_trajectory(FINAL)
    trajectory["steps"][3]["tool_calls"] = [  # type: ignore[index]
        {"function_name": "Read", "arguments": {"file_path": "/workspace/b.rs"}}
    ]

    depth = scored._investigation_depth(trajectory, FINAL)

    assert depth["investigative"] == 1
    assert depth["session"]["investigative"] == 4


def test_investigation_depth_without_a_prompt_covers_the_whole_trajectory() -> None:
    """No final prompt to slice on means no turn boundary is known."""

    depth = scored._investigation_depth(_resumed_trajectory(FINAL))

    assert depth["scanned_from"] == 0
    assert depth["investigative"] == 3
    assert depth["session"]["investigative"] == 3


def test_raw_result_preserves_budget_exhaustion(tmp_path: Path) -> None:
    stream = tmp_path / "claude-code.txt"
    stream.write_text(
        "noise\n"
        + json.dumps(
            {
                "type": "result",
                "subtype": "error_max_budget_usd",
                "terminal_reason": "budget_exhausted",
                "total_cost_usd": 0.2049,
            }
        )
        + "\n"
    )

    result = scored._parse_raw_result(stream)

    assert result is not None
    assert result["total_cost_usd"] == pytest.approx(0.2049)
    assert result["terminal_reason"] == "budget_exhausted"


def test_gateway_cross_check_uses_published_haiku_rates() -> None:
    result = scored._gateway_rate_cross_check(
        "anthropic/claude-haiku-4.5",
        {
            "modelUsage": {
                "anthropic/claude-haiku-4.5": {
                    "inputTokens": 1677,
                    "outputTokens": 139,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 30887,
                }
            }
        },
    )

    assert result == pytest.approx(0.04098075)


def test_budget_limit_rejects_accidental_large_trial(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at most five"):
        _inputs(tmp_path, max_budget_usd="5.01")


def test_turn_limit_allows_bounded_delegated_investigations(tmp_path: Path) -> None:
    assert _inputs(tmp_path, max_turns=60).max_turns == 60
    with pytest.raises(ValueError, match="between 1 and 100"):
        _inputs(tmp_path, max_turns=101)


def test_sibling_failure_does_not_poison_successful_trial_receipt(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    first = _challenge(tmp_path, "first-task")
    second = _challenge(tmp_path, "second-task")
    common_provenance = {
        "source_commit": inputs.revision,
        "source_tree_sha256": "tree-sha",
        "fixture_id": "fixture-one",
        "graph_arm": "faithful",
        "artifact_sha256": inputs.artifact_sha256,
    }
    successful_trial_dir = tmp_path / "successful"
    failed_trial_dir = tmp_path / "failed"
    (successful_trial_dir / "agent").mkdir(parents=True)
    (failed_trial_dir / "agent").mkdir(parents=True)

    successful = scored._extract_challenge_receipt(
        inputs,
        first,
        successful_trial_dir,
        {
            "started_at": "2026-07-23T10:00:00+00:00",
            "finished_at": "2026-07-23T10:00:07.500000+00:00",
            "verifier_result": {"rewards": {"reward": 1.0}},
        },
        _valid_trajectory(),
        common_provenance,
    )
    failed = scored._extract_challenge_receipt(
        inputs,
        second,
        failed_trial_dir,
        {
            "started_at": "2026-07-23T10:00:08+00:00",
            "finished_at": "2026-07-23T10:00:12+00:00",
            "verifier_result": {"rewards": {"reward": 0.0}},
        },
        _valid_trajectory(),
        common_provenance,
    )

    assert successful.outcome == "passed"
    assert successful.timings["invocation_wall"] == pytest.approx(7.5)
    assert failed.outcome == "failed"
    assert failed.timings["invocation_wall"] == pytest.approx(4.0)


def test_trial_exception_is_infrastructure_error_without_poisoning_sibling(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    challenge = _challenge(tmp_path, "first-task")
    trial_dir = tmp_path / "trial"
    (trial_dir / "agent").mkdir(parents=True)

    result = scored._extract_challenge_receipt(
        inputs,
        challenge,
        trial_dir,
        {
            "exception_info": {"type": "RuntimeError"},
            "verifier_result": {"rewards": {"reward": 1.0}},
        },
        _valid_trajectory(),
        {},
    )

    assert result.outcome == "infrastructure-error"
    assert result.timings["invocation_wall"] is None


@pytest.mark.parametrize(
    ("function_name", "arguments"),
    [
        ("Bash", {"command": "git -C /workspace log --oneline -5"}),
        ("Bash", {"command": "git ls-remote https://example.invalid/repository.git"}),
        (
            "Bash",
            {"command": "curl -L https://github.com/serkanyersen/dotstate/blob/main/src/app.rs"},
        ),
        (
            "WebFetch",
            {"url": "https://raw.githubusercontent.com/serkanyersen/dotstate/main/src/app.rs"},
        ),
        ("Bash", {"command": "gh repo clone serkanyersen/dotstate /tmp/source"}),
    ],
)
def test_forbidden_repository_access_detection(
    function_name: str, arguments: dict[str, str]
) -> None:
    trajectory = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [
                    {"function_name": function_name, "arguments": arguments},
                ],
            }
        ]
    }

    assert scored._forbidden_repository_access(trajectory) is True


def test_benign_local_code_investigation_is_not_repository_access() -> None:
    trajectory = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [
                    {
                        "function_name": "Bash",
                        "arguments": {
                            "command": "rg -n 'handle_event|keymap' src && git status --short"
                        },
                    }
                ],
            }
        ]
    }

    assert scored._forbidden_repository_access(trajectory) is False


def test_truncated_history_mode_allows_local_history_but_never_remote_git() -> None:
    local = _valid_trajectory(
        {
            "function_name": "Bash",
            "arguments": {"command": "git log -5 && git blame src/click/testing.py"},
        }
    )
    remote = _valid_trajectory(
        {
            "function_name": "Bash",
            "arguments": {"command": "git fetch origin main"},
        }
    )

    assert scored._forbidden_repository_access(local, allow_local_git_history=True) is False
    assert scored._forbidden_repository_access(remote, allow_local_git_history=True) is True


def test_truncated_history_receipt_accepts_local_git_log(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    challenge = _challenge(tmp_path, "first-task")
    trial_dir = tmp_path / "trial"
    (trial_dir / "agent").mkdir(parents=True)
    trajectory = _valid_trajectory(
        {
            "function_name": "Bash",
            "arguments": {"command": "git log --oneline -5"},
        }
    )

    result = scored._extract_challenge_receipt(
        inputs,
        challenge,
        trial_dir,
        {"verifier_result": {"rewards": {"reward": 1.0}}},
        trajectory,
        {},
        allow_local_git_history=True,
    )

    assert result.outcome == "passed"
    assert result.evidence["forbidden_repository_access_detected"] is False


def test_forbidden_repository_access_invalidates_only_its_trial(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    challenge = _challenge(tmp_path, "first-task")
    trial_dir = tmp_path / "trial"
    (trial_dir / "agent").mkdir(parents=True)
    trajectory = _valid_trajectory(
        {
            "function_name": "Bash",
            "arguments": {"command": "git log --all --oneline"},
        }
    )

    result = scored._extract_challenge_receipt(
        inputs,
        challenge,
        trial_dir,
        {"verifier_result": {"rewards": {"reward": 1.0}}},
        trajectory,
        {},
    )

    assert result.outcome == "invalid"
    assert result.evidence["forbidden_repository_access_detected"] is True


@pytest.mark.parametrize(
    "trajectory",
    [
        None,
        {},
        {
            "schema_version": "ATIF-v1.7",
            "session_id": "session-one",
            "agent": {"name": "claude-code"},
            "steps": [],
        },
    ],
)
def test_missing_or_invalid_atif_marks_trial_invalid(
    tmp_path: Path, trajectory: dict[str, object] | None
) -> None:
    inputs = _inputs(tmp_path)
    challenge = _challenge(tmp_path, "first-task")
    trial_dir = tmp_path / "trial"
    (trial_dir / "agent").mkdir(parents=True)

    result = scored._extract_challenge_receipt(
        inputs,
        challenge,
        trial_dir,
        {"verifier_result": {"rewards": {"reward": 1.0}}},
        trajectory,
        {},
    )

    assert result.outcome == "invalid"
    assert result.evidence["atif_integrity_valid"] is False


def test_unobserved_tool_call_is_not_valid_atif() -> None:
    trajectory = _valid_trajectory(
        {"function_name": "Read", "arguments": {"file_path": "/workspace/src/app.rs"}}
    )
    trajectory["steps"][1]["observation"] = {"results": []}

    assert scored._atif_integrity_error(trajectory) == "unauditable_atif_tool_call"


def test_opaque_delegation_is_detected_but_flattened_sidechain_is_auditable() -> None:
    prompt = "Inspect local input handling."
    opaque = _valid_trajectory(
        {
            "function_name": "Agent",
            "arguments": {"prompt": prompt, "subagent_type": "Explore"},
        }
    )
    flattened = _valid_trajectory(
        {
            "function_name": "Agent",
            "arguments": {"prompt": prompt, "subagent_type": "Explore"},
        }
    )
    flattened["steps"].extend(
        [
            {
                "step_id": 3,
                "timestamp": "2026-07-23T10:00:02+00:00",
                "source": "user",
                "message": prompt,
                "extra": {"is_sidechain": True},
            },
            {
                "step_id": 4,
                "timestamp": "2026-07-23T10:00:03+00:00",
                "source": "agent",
                "message": "Found the local call sites.",
                "extra": {"is_sidechain": True},
            },
        ]
    )

    assert scored._opaque_delegation_detected(opaque) is True
    assert scored._opaque_delegation_detected(flattened) is False
    assert scored._atif_integrity_error(flattened) is None


@pytest.mark.parametrize(
    ("function_name", "arguments"),
    [
        ("WebSearch", {"query": "Dotstate input handling GitHub"}),
        ("browser", {"query": "why does Dotstate key handling recur"}),
        ("Bash", {"command": "curl 'https://www.google.com/search?q=dotstate+app.rs'"}),
        ("Bash", {"command": "gh search code 'keypress dotstate'"}),
    ],
)
def test_external_search_or_network_tools_are_detected(
    function_name: str, arguments: dict[str, str]
) -> None:
    trajectory = _valid_trajectory({"function_name": function_name, "arguments": arguments})

    assert scored._external_source_access_detected(trajectory) is True


@pytest.mark.parametrize(
    "command",
    [
        "curl --version 2>&1 | head -1 || wget --version 2>&1 | head -1",
        "httpie --help",
        "lynx -h",
    ],
)
def test_network_client_capability_probes_are_not_external_source_access(
    command: str,
) -> None:
    trajectory = _valid_trajectory(
        {"function_name": "Bash", "arguments": {"command": command}}
    )

    assert scored._external_source_access_detected(trajectory) is False


def test_network_client_names_in_bash_description_do_not_override_the_command() -> None:
    trajectory = _valid_trajectory(
        {
            "function_name": "Bash",
            "arguments": {
                "command": (
                    "curl --version 2>/dev/null | head -1 || "
                    "wget --version 2>/dev/null | head -1 || echo 'no downloader'"
                ),
                "description": "Check if curl or wget is available",
            },
        }
    )

    assert scored._external_source_access_detected(trajectory) is False


@pytest.mark.parametrize(
    "command",
    [
        "curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py",
        "wget -qO /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py",
    ],
)
def test_pip_bootstrap_is_not_external_information_access(command: str) -> None:
    trajectory = _valid_trajectory(
        {"function_name": "Bash", "arguments": {"command": command}}
    )

    assert scored._external_source_access_detected(trajectory) is False


def test_pip_bootstrap_does_not_hide_another_external_url() -> None:
    trajectory = _valid_trajectory(
        {
            "function_name": "Bash",
            "arguments": {
                "command": (
                    "curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py "
                    "&& curl https://example.com/context"
                )
            },
        }
    )

    assert scored._external_source_access_detected(trajectory) is True


@pytest.mark.parametrize(
    "trajectory",
    [
        _valid_trajectory(
            {
                "function_name": "Agent",
                "arguments": {"prompt": "Investigate this elsewhere."},
            }
        ),
        _valid_trajectory(
            {
                "function_name": "WebSearch",
                "arguments": {"query": "Dotstate keypress architecture"},
            }
        ),
    ],
)
def test_opaque_delegation_or_external_search_invalidates_trial(
    tmp_path: Path, trajectory: dict[str, object]
) -> None:
    inputs = _inputs(tmp_path)
    challenge = _challenge(tmp_path, "first-task")
    trial_dir = tmp_path / "trial"
    (trial_dir / "agent").mkdir(parents=True)

    result = scored._extract_challenge_receipt(
        inputs,
        challenge,
        trial_dir,
        {"verifier_result": {"rewards": {"reward": 1.0}}},
        trajectory,
        {},
    )

    assert result.outcome == "invalid"


def test_nonzero_harbor_exit_is_aggregate_infrastructure_only_when_unexplained() -> None:
    passed = scored.ChallengeReceipt(
        challenge_id="first-task",
        outcome="passed",
        reward=1.0,
        tokens={},
        cost={},
        timings={},
        evidence={},
        provenance={},
    )
    failed = scored.ChallengeReceipt(
        challenge_id="second-task",
        outcome="failed",
        reward=0.0,
        tokens={},
        cost={},
        timings={},
        evidence={},
        provenance={},
    )

    assert scored._aggregate_outcome((passed,), 7, None) == "infrastructure-error"
    assert scored._aggregate_outcome((passed, failed), 7, None) == "failed"
    assert passed.outcome == "passed"


def test_aggregate_receipt_keeps_order_and_scales_possible_spend(tmp_path: Path) -> None:
    inputs = _inputs(
        tmp_path,
        condition="no-stele",
        challenge_ids=("first-task", "second-task"),
        max_budget_usd="0.25",
    )
    results = tuple(
        scored.ChallengeReceipt(
            challenge_id=challenge_id,
            outcome="passed",
            reward=1.0,
            tokens={},
            cost={},
            timings={},
            evidence={},
            provenance={},
        )
        for challenge_id in inputs.challenge_ids
    )

    receipt = scored.ScoredReceipt(
        schema_version=4,
        classification="exploratory",
        run_id=inputs.run_id,
        condition=inputs.condition,
        model=inputs.model,
        challenge_ids=inputs.challenge_ids,
        requested_budget_cap_usd_per_trial=inputs.max_budget_usd,
        requested_budget_cap_usd_across_trials="0.50",
        max_turns=inputs.max_turns,
        harbor_job_name="route-two",
        harbor_exit_code=0,
        timings={"invocation_wall": 12.5},
        outcome="passed",
        results=results,
        cleanup_completed=True,
        error=None,
    )

    payload = receipt.as_dict()
    assert [result["challenge_id"] for result in payload["results"]] == [
        "first-task",
        "second-task",
    ]
    assert payload["requested_budget_cap_usd_across_trials"] == "0.50"
    assert payload["timings"]["invocation_wall"] == pytest.approx(12.5)
    assert payload["classification"] == "exploratory"


def _minimal_scored_receipt() -> object:
    return scored.ScoredReceipt(
        schema_version=4,
        classification="exploratory",
        run_id="receipt-run",
        condition="no-stele",
        model=scored.DEFAULT_MODEL,
        challenge_ids=("service-readiness",),
        requested_budget_cap_usd_per_trial="0.10",
        requested_budget_cap_usd_across_trials="0.10",
        max_turns=3,
        harbor_job_name=None,
        harbor_exit_code=None,
        timings={"invocation_wall": None},
        outcome="infrastructure-error",
        results=(),
        cleanup_completed=True,
        error=None,
    )


def test_scored_main_persists_success_receipt_mode_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "scored.json"
    monkeypatch.setattr(scored, "run_scored_smoke", lambda *_: _minimal_scored_receipt())
    exit_code = scored.main(
        [
            str(tmp_path),
            "a" * 40,
            "--condition",
            "no-stele",
            "--attempt-root",
            str(tmp_path / "attempt"),
            "--work-root",
            str(tmp_path / "work"),
            "--run-id",
            "receipt-run",
            "--receipt-out",
            str(destination),
        ]
    )

    assert exit_code == 0
    assert json.loads(destination.read_text())["ok"] is True
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_scored_main_persists_retained_failure_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "scored.json"
    retained = _minimal_scored_receipt()

    def fail(*_: object) -> object:
        error = scored.ScoredSmokeError("scored_smoke_failed", receipt=retained)
        error.__cause__ = scored.ScoredSmokeError("post_run_snapshot_mismatch")
        raise error

    monkeypatch.setattr(scored, "run_scored_smoke", fail)
    exit_code = scored.main(
        [
            str(tmp_path),
            "a" * 40,
            "--condition",
            "no-stele",
            "--attempt-root",
            str(tmp_path / "attempt"),
            "--work-root",
            str(tmp_path / "work"),
            "--run-id",
            "receipt-run",
            "--receipt-out",
            str(destination),
        ]
    )

    payload = json.loads(destination.read_text())
    assert exit_code == 1
    assert payload["failure"]["code"] == "post_run_snapshot_mismatch"
    assert payload["receipt"]["classification"] == "exploratory"
