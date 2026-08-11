"""One arm of one borrowed task: what it refuses, and what it records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import run_borrowed_task as borrowed
from prepare_external_dataset import sanitise_dataset

DOCKERFILE = "FROM buildpack-deps:jammy\nWORKDIR /app\nCOPY workspace/ ./\n"
ARTIFACT_DIGEST = "a" * 64


def _dataset(root: Path, *names: str) -> Path:
    source = root / "source"
    source.mkdir()
    for name in names:
        task = source / name
        workspace = task / "environment" / "workspace"
        workspace.mkdir(parents=True)
        (task / "task.toml").write_text(f'[task]\nname = "{name}"\n', encoding="utf-8")
        (task / "environment" / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
        (workspace / "solve.py").write_text("def solve():\n    pass\n", encoding="utf-8")
        payload = workspace / ".oracle"
        payload.mkdir()
        (payload / "solution.enc").write_bytes(b"Salted__\x00encrypted")
        meta = task / "tests" / ".meta"
        meta.mkdir(parents=True)
        (meta / "example.py").write_text("def solve():\n    return 42\n", encoding="utf-8")
        (task / "tests" / "test.sh").write_text(
            "#!/bin/bash\nset -euo pipefail\npytest\n"
            "echo $? > /logs/verifier/reward.txt\n",
            encoding="utf-8",
        )
    clean = root / "clean"
    sanitise_dataset(source, clean)
    return clean


def _inputs(tmp_path: Path, dataset: Path, **overrides: Any) -> borrowed.BorrowedInputs:
    defaults: dict[str, Any] = {
        "dataset": dataset,
        "task_name": "clock",
        "attempt_root": tmp_path / "attempts",
        "work_root": tmp_path / "work",
        "run_id": "suite-001-task-01-control",
        "condition": "no-stele",
    }
    defaults.update(overrides)
    return borrowed.BorrowedInputs(**defaults)


def _stele_inputs(tmp_path: Path, dataset: Path, **overrides: Any) -> borrowed.BorrowedInputs:
    artifact = tmp_path / "stele"
    artifact.write_bytes(b"binary")
    defaults: dict[str, Any] = {
        "condition": "stele-fresh",
        "provisioner_executable": tmp_path / "provision.py",
        "artifact_path": artifact,
        "artifact_sha256": ARTIFACT_DIGEST,
        "backend_url": "http://host.docker.internal:54321",
        "backend_anon_key": "public-key",
    }
    defaults.update(overrides)
    return _inputs(tmp_path, dataset, **defaults)


def _trajectory(*commands: str) -> dict[str, Any]:
    """A trajectory that passes the shared ATIF integrity check.

    Built to the real schema rather than a convenient shape, because the
    integrity check is what makes the cheat scan trustworthy: a trajectory the
    apparatus cannot audit already invalidates the trial, so a loose fixture
    here would test the scanner against input the runner would never accept.
    """

    calls = [
        {
            "tool_call_id": f"call-{index}",
            "function_name": "bash",
            "arguments": {"command": command},
        }
        for index, command in enumerate(commands, start=1)
    ]
    agent_step: dict[str, Any] = {
        "step_id": 2,
        "timestamp": "2026-07-25T10:00:01+00:00",
        "source": "agent",
        "message": "Working on it.",
    }
    if calls:
        agent_step["tool_calls"] = calls
        agent_step["observation"] = {
            "results": [
                {"source_call_id": call["tool_call_id"], "output": "ok"} for call in calls
            ]
        }
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "session-one",
        "agent": {"name": "claude-code"},
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-07-25T10:00:00+00:00",
                "source": "user",
                "message": "Solve the exercise.",
            },
            agent_step,
        ],
    }


def test_a_dataset_that_was_never_sanitised_is_refused(tmp_path: Path) -> None:
    """The whole reason the runner takes a directory instead of a registry id."""

    raw = tmp_path / "raw"
    (raw / "clock").mkdir(parents=True)
    (raw / "clock" / "task.toml").write_text("[task]\n", encoding="utf-8")

    with pytest.raises(borrowed.ScoredSmokeError) as error:
        borrowed._validate_task_directory(_inputs(tmp_path, raw))
    assert error.value.code == "dataset_was_not_sanitised"


def test_a_task_outside_the_dataset_is_refused(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, "clock")
    outside = tmp_path / "elsewhere" / "clock"
    (outside / "environment").mkdir(parents=True)
    (outside / "task.toml").write_text("[task]\n", encoding="utf-8")
    (dataset / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(borrowed.ScoredSmokeError) as error:
        borrowed._validate_task_directory(_inputs(tmp_path, dataset, task_name="escape"))
    assert error.value.code == "borrowed_task_is_not_a_directory"


def test_a_task_name_cannot_traverse(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, "clock")
    with pytest.raises(ValueError):
        _inputs(tmp_path, dataset, task_name="../clock")


def test_the_control_arm_is_given_no_stele_inputs_at_all(tmp_path: Path) -> None:
    """A control that carried the artifact would differ in more than the measure."""

    dataset = _dataset(tmp_path, "clock")
    inputs = _inputs(tmp_path, dataset)
    config = borrowed._job_config(inputs, inputs.task_directory)

    agent = config["agents"][0]
    assert agent["name"] == "claude-code"
    assert "stele" not in json.dumps(config).lower()
    assert config["tasks"] == [{"path": str(dataset / "clock")}]


def test_the_treatment_arm_creates_its_project_rather_than_restoring_one(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path, "clock")
    inputs = _stele_inputs(tmp_path, dataset)
    config = borrowed._job_config(inputs, inputs.task_directory)

    agent = config["agents"][0]
    assert agent["import_path"] == borrowed.HARBOR_AGENT_IMPORT_PATH
    assert agent["kwargs"]["project_provisioning"] == "create"
    # There is no frozen graph to hand over, and claiming one would be a lie the
    # entrypoint would then try to honour.
    assert "project_binding_path" not in agent["kwargs"]
    assert agent["env"]["SUPABASE_URL"] == "http://host.docker.internal:54321"


def test_the_treatment_arm_needs_every_stele_input(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, "clock")
    with pytest.raises(ValueError, match="backend_url"):
        _stele_inputs(tmp_path, dataset, backend_url=None)


def test_a_project_name_that_would_reveal_the_experiment_is_the_caller_s_choice(
    tmp_path: Path,
) -> None:
    """The default is deliberately generic — the agent can read this name."""

    assert "bench" not in borrowed.DEFAULT_PROJECT_NAME.lower()
    assert "eval" not in borrowed.DEFAULT_PROJECT_NAME.lower()


@pytest.mark.parametrize(
    ("command", "marker"),
    [
        ("cat .oracle/solution.enc", ".oracle"),
        ("ls environment/workspace/solution.enc", "solution.enc"),
        ("openssl enc -aes-256-cbc -d -in payload", "openssl_decrypt"),
        ("echo tb-secret-please-change", "tb-secret-please-change"),
    ],
)
def test_reaching_for_the_reference_solution_is_detected(command: str, marker: str) -> None:
    hits = borrowed._oracle_access_detected(_trajectory(command))
    assert marker in hits


def test_an_ordinary_trajectory_trips_nothing() -> None:
    assert borrowed._oracle_access_detected(_trajectory("pytest -q", "cat clock.py")) == []


def test_stele_tool_calls_are_counted_apart_from_the_rest() -> None:
    """The overhead signal: how much of the agent's work went to memory."""

    trajectory = {
        "steps": [
            {
                "tool_calls": [
                    {"function_name": "bash"},
                    {"function_name": "mcp__stele__search_nodes"},
                    {"function_name": "mcp__plugin_stele_stele__add_task"},
                    {"function_name": "edit"},
                ]
            }
        ]
    }
    assert borrowed._tool_call_counts(trajectory) == {"total": 4, "stele": 2}


def _trial(tmp_path: Path, *, reward: float) -> tuple[Path, dict[str, Any]]:
    trial_dir = tmp_path / "trial"
    (trial_dir / "agent").mkdir(parents=True)
    trial = {
        "task_name": "clock",
        "started_at": "2026-07-25T10:00:00+00:00",
        "finished_at": "2026-07-25T10:02:00+00:00",
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_result": {
            "n_input_tokens": 1000,
            "n_output_tokens": 200,
            "n_cache_tokens": 50,
            "cost_usd": 0.2,
        },
    }
    return trial_dir, trial


def _provenance() -> dict[str, str]:
    return {
        "dataset_path": "/tmp/clean",
        "task_name": "clock",
        "workspace_sha256": "c" * 64,
        "verifier_sha256": "d" * 64,
        "sanitisation_manifest_sha256": "e" * 64,
    }


def test_a_solved_task_is_scored_from_the_borrowed_verifier(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)

    receipt = borrowed._trial_receipt(
        _inputs(tmp_path, dataset),
        trial_dir,
        trial,
        _trajectory("pytest -q"),
        None,
        None,
        _provenance(),
    )

    assert receipt.outcome == "passed"
    assert receipt.reward == 1.0
    assert receipt.tokens["input"] == 1000
    assert receipt.timings["invocation_wall"] == 120.0
    assert receipt.evidence["oracle_payload_access_detected"] is False


def test_a_graph_trial_records_unprompted_node_delivery(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)
    session = trial_dir / "agent" / "sessions" / "projects" / "workspace" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "type": "attachment",
                "attachment": {
                    "type": "hook_additional_context",
                    "hookName": "UserPromptSubmit",
                    "content": [
                        "Stele context:\n[KNOW-3] decision\n[TASK-2] open\n"
                        "<!-- stele:rag KNOW-3 TASK-2 -->"
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    receipt = borrowed._trial_receipt(
        _stele_inputs(tmp_path, dataset),
        trial_dir,
        trial,
        _trajectory("pytest -q"),
        None,
        {"reported_project_observed": True},
        _provenance(),
    )

    assert receipt.memory_evidence == {
        "injected": True,
        "channels": ["rag"],
        "occurrences": 1,
        "node_ids": ["KNOW-3", "TASK-2"],
    }


def test_a_partially_rewarded_task_is_not_a_pass(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=0.5)

    receipt = borrowed._trial_receipt(
        _inputs(tmp_path, dataset), trial_dir, trial, _trajectory(), None, None, _provenance()
    )
    assert receipt.outcome == "failed"


def test_a_trial_that_reached_the_answer_is_invalid_not_a_pass(tmp_path: Path) -> None:
    """Excluded rather than scored — a cheat is not a zero, it is not a result."""

    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)

    receipt = borrowed._trial_receipt(
        _inputs(tmp_path, dataset),
        trial_dir,
        trial,
        _trajectory("openssl enc -aes-256-cbc -d -in .oracle/solution.enc"),
        None,
        None,
        _provenance(),
    )

    assert receipt.outcome == "invalid"
    assert receipt.evidence["oracle_payload_access_detected"] is True
    assert ".oracle" in receipt.evidence["oracle_payload_markers"]


def test_an_unreadable_trajectory_cannot_be_audited_so_it_is_invalid(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)

    receipt = borrowed._trial_receipt(
        _inputs(tmp_path, dataset),
        trial_dir,
        trial,
        None,
        "missing_atif_trajectory",
        None,
        _provenance(),
    )
    assert receipt.outcome == "invalid"


def test_the_graph_read_records_what_survived_to_the_database(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)
    (trial_dir / "agent" / borrowed.CREATED_PROJECT_REPORT_NAME).write_text(
        json.dumps({"project_id": "p-1", "slug": "workspace-ab12"}), encoding="utf-8"
    )
    uptake = {
        "projects": [{"project_id": "p-1", "slug": "workspace-ab12"}],
        "project_count": 1,
        "reported_project": {"project_id": "p-1", "slug": "workspace-ab12"},
        "reported_project_observed": True,
        "observed": {
            "project_id": "p-1",
            "slug": "workspace-ab12",
            "agent_nodes_by_type": {"knowledge": 2, "task": 1},
            "agent_tasks": {"total": 1, "claimed": 1, "completed": 1},
            "seeded_tasks_touched": 0,
        },
    }

    receipt = borrowed._trial_receipt(
        _stele_inputs(tmp_path, dataset),
        trial_dir,
        trial,
        _trajectory("pytest -q"),
        None,
        uptake,
        _provenance(),
    )

    assert receipt.outcome == "passed"
    assert receipt.evidence["created_project"]["slug"] == "workspace-ab12"
    assert receipt.evidence["graph_uptake"]["observed"]["agent_nodes_by_type"] == {
        "knowledge": 2,
        "task": 1,
    }
    assert receipt.evidence["graph_uptake_disagrees_with_container"] is False


def test_a_graph_the_host_cannot_confirm_invalidates_the_trial(tmp_path: Path) -> None:
    """The container's claim is checked, not trusted.

    If the project the agent said it made is not one this attempt's principal
    owns, the graph being reported is not the graph that was used — and every
    uptake number would be about the wrong project.
    """

    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)
    uptake = {
        "projects": [],
        "project_count": 0,
        "reported_project": {"project_id": "p-1"},
        "reported_project_observed": False,
        "observed": None,
    }

    receipt = borrowed._trial_receipt(
        _stele_inputs(tmp_path, dataset),
        trial_dir,
        trial,
        _trajectory("pytest -q"),
        None,
        uptake,
        _provenance(),
    )
    assert receipt.outcome == "invalid"


def test_the_control_arm_is_not_held_to_the_graph_check(tmp_path: Path) -> None:
    """It has no project, so an absent one is not a disagreement."""

    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)

    receipt = borrowed._trial_receipt(
        _inputs(tmp_path, dataset), trial_dir, trial, _trajectory(), None, None, _provenance()
    )
    assert receipt.outcome == "passed"
    assert receipt.evidence["graph_uptake_disagrees_with_container"] is False


def test_the_receipt_flattens_into_the_metrics_every_comparison_reads(
    tmp_path: Path,
) -> None:
    """The borrowed receipt has to be readable by the shared parser, unchanged."""

    from dataclasses import asdict

    from stele_bench import metrics

    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)
    result = borrowed._trial_receipt(
        _inputs(tmp_path, dataset), trial_dir, trial, _trajectory(), None, None, _provenance()
    )
    flat = metrics.arm_metrics({"results": [asdict(result)]})
    assert flat is not None
    assert flat["reward"] == 1.0
    assert flat["tokens_input"] == 1000.0
    assert flat["trial_wall_seconds"] == 120.0


def test_finding_the_trial_refuses_an_ambiguous_result(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    for index in (1, 2):
        trial = jobs / f"trial-{index}"
        (trial / "agent").mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps({"task_name": "clock"}), encoding="utf-8")

    with pytest.raises(borrowed.ScoredSmokeError) as error:
        borrowed._find_trial(jobs, "clock")
    assert error.value.code == "duplicate_trial_result"


def test_finding_the_trial_matches_a_namespaced_task(tmp_path: Path) -> None:
    """A borrowed dataset names its tasks however it likes."""

    jobs = tmp_path / "jobs"
    trial = jobs / "trial-1"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"task_name": "aider-polyglot/clock"}), encoding="utf-8"
    )
    # The job aggregate sits alongside and owns no agent directory.
    (jobs / "result.json").write_text(json.dumps({"trials": 1}), encoding="utf-8")

    found, result = borrowed._find_trial(jobs, "clock")
    assert found == trial
    assert result["task_name"] == "aider-polyglot/clock"


def test_a_missing_trial_is_not_silently_scored_as_a_failure(tmp_path: Path) -> None:
    with pytest.raises(borrowed.ScoredSmokeError) as error:
        borrowed._find_trial(tmp_path / "jobs", "clock")
    assert error.value.code == "missing_trial_result"


def test_reaching_the_network_is_recorded_but_does_not_invalidate(tmp_path: Path) -> None:
    """Ambiguous by nature: a fetch may be a package manager, not a lookup.

    Auto-dropping these would bias against whichever arm installs more
    dependencies, so the signal is surfaced for a declared decision rule rather
    than acted on silently.
    """

    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)

    receipt = borrowed._trial_receipt(
        _inputs(tmp_path, dataset),
        trial_dir,
        trial,
        _trajectory("curl -sL https://example.test/solution"),
        None,
        None,
        _provenance(),
    )

    assert receipt.evidence["external_source_access_detected"] is True
    assert receipt.outcome == "passed"


def test_an_offline_trial_records_no_network_access(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, "clock")
    trial_dir, trial = _trial(tmp_path, reward=1.0)

    receipt = borrowed._trial_receipt(
        _inputs(tmp_path, dataset),
        trial_dir,
        trial,
        _trajectory("pytest -q"),
        None,
        None,
        _provenance(),
    )
    assert receipt.evidence["external_source_access_detected"] is False


def test_both_arms_get_the_same_setup_ceiling(tmp_path: Path) -> None:
    """A ceiling either arm can graze is crossed by the slower one more often.

    The treatment arm's setup does strictly more work — upload the artifact,
    install the plugin, create a project, run the preflight — so an ungenerous
    shared ceiling drops treatment trials asymmetrically. Losing more treatment
    trials than control trials is not a paired comparison, so the override has
    to be identical on both arms, not merely present on the slower one.
    """

    dataset = _dataset(tmp_path, "clock")
    control = borrowed._job_config(
        _inputs(tmp_path, dataset), _inputs(tmp_path, dataset).task_directory
    )
    treatment_inputs = _stele_inputs(tmp_path, dataset)
    treatment = borrowed._job_config(treatment_inputs, treatment_inputs.task_directory)

    ceiling = borrowed.AGENT_SETUP_TIMEOUT_SECONDS
    assert control["agents"][0]["override_setup_timeout_sec"] == ceiling
    assert treatment["agents"][0]["override_setup_timeout_sec"] == ceiling
    # Harbor's own default is 360s, which the stock claude-code install alone
    # has been measured over on these images.
    assert ceiling > 360.0
