#!/usr/bin/env python3
"""Run Fieldnote's base/target graders in the pinned image without an agent."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = REPOSITORY_ROOT / "eval" / "stele-bench"
sys.path.insert(0, str(BENCH_ROOT / "src"))

from stele_bench.challenges import select_challenges  # noqa: E402
from stele_bench.corpus import materialize_pinned_history  # noqa: E402
from stele_bench.harbor_pack import generate_harbor_task_pack  # noqa: E402

DEFAULT_MANIFEST = BENCH_ROOT / "preregistrations" / "fieldnote-continuity-v8.json"


class QualificationError(RuntimeError):
    pass


def _validate_harbor_task_schema(task: Path) -> None:
    harbor = shutil.which("harbor")
    if harbor is None:
        raise QualificationError("Harbor is not installed")
    interpreter = Path(harbor).resolve().parent / "python"
    command = (
        str(interpreter),
        "-c",
        (
            "from pathlib import Path; "
            "from harbor.models.task.config import TaskConfig; "
            "TaskConfig.model_validate_toml(Path(__import__('sys').argv[1]).read_text())"
        ),
        str(task / "task.toml"),
    )
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        raise QualificationError(
            f"Harbor rejected task.toml: {completed.stderr[-2000:]}"
        )


def _validate_harbor_trial_construction(task: Path, jobs: Path) -> None:
    """Run Harbor's real environment path with its model-free no-op agent."""

    harbor = shutil.which("harbor")
    if harbor is None:
        raise QualificationError("Harbor is not installed")
    command = (
        harbor,
        "run",
        "--path",
        str(task),
        "--agent",
        "nop",
        "--disable-verification",
        "--job-name",
        "fieldnote-zero-model-qualification",
        "--jobs-dir",
        str(jobs),
        "--n-concurrent",
        "1",
        "--yes",
        "--quiet",
    )
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=180)
    if completed.returncode != 0:
        raise QualificationError(
            "Harbor could not construct and run the task with its nop agent: "
            f"{completed.stderr[-4000:]}"
        )
    job_results = []
    for path in jobs.rglob("result.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("n_total_trials") == 1:
            job_results.append(payload)
    if len(job_results) != 1:
        raise QualificationError(
            f"Harbor nop qualification produced {len(job_results)} job results"
        )
    stats = job_results[0].get("stats")
    if not isinstance(stats, dict) or stats.get("n_completed_trials") != 1:
        raise QualificationError("Harbor nop qualification did not complete one trial")


def _run_task(task: Path, logs: Path, image: str) -> dict[str, Any]:
    logs.mkdir()
    command = (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cpus",
        "1",
        "--memory",
        "1024m",
        "--mount",
        f"type=bind,src={task / 'environment' / 'project'},dst=/workspace,readonly",
        "--mount",
        f"type=bind,src={task / 'tests'},dst=/tests,readonly",
        "--mount",
        f"type=bind,src={logs},dst=/logs/verifier",
        image,
        "bash",
        "/tests/test.sh",
    )
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
    if completed.returncode != 0:
        raise QualificationError(
            f"verifier container failed ({completed.returncode}): {completed.stderr[-2000:]}"
        )
    reward = json.loads((logs / "reward.json").read_text(encoding="utf-8"))
    mutation = json.loads((logs / "mutation.json").read_text(encoding="utf-8"))
    return {
        "reward": reward.get("reward"),
        "mutation_status": mutation.get("status"),
    }


def qualify(source: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image = manifest["runtime"]["task_image"]
    sessions = manifest["sessions"]
    observations: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="fieldnote-qualification-") as raw:
        root = Path(raw)
        for session in sessions:
            index = int(session["index"])
            challenge_id = str(session["challenge_id"])
            challenge = select_challenges((challenge_id,))
            next_target = (
                (str(sessions[index]["canonical_target_revision"]),)
                if index < len(sessions)
                else ()
            )
            for state, revision, expected_reward, forbidden in (
                (
                    "base",
                    str(session["base_revision"]),
                    0,
                    (str(session["canonical_target_revision"]),),
                ),
                (
                    "target",
                    str(session["canonical_target_revision"]),
                    1,
                    next_target,
                ),
            ):
                stage_root = root / f"session-{index}-{state}"
                corpus = materialize_pinned_history(
                    source,
                    revision,
                    stage_root / "corpus",
                    forbidden_object_ids=forbidden,
                )
                history = corpus.history
                if history is None:
                    raise QualificationError("reachable history was not materialized")
                expected_tree = str(
                    session["base_tree"]
                    if state == "base"
                    else session["canonical_target_tree"]
                )
                if history.head_tree != expected_tree:
                    raise QualificationError(
                        f"session {index} {state} tree differs from the registration"
                    )
                if state == "base" and (
                    history.commit_count != int(session["reachable_commit_count"])
                    or history.object_count != int(session["reachable_object_count"])
                    or history.object_ids_sha256 != session["reachable_object_ids_sha256"]
                ):
                    raise QualificationError(
                        f"session {index} base history differs from the registration"
                    )
                pack = generate_harbor_task_pack(
                    corpus,
                    stage_root / "pack",
                    challenges=challenge,
                )
                _validate_harbor_task_schema(pack.task_directory)
                if state == "base":
                    _validate_harbor_trial_construction(
                        pack.task_directory,
                        stage_root / "harbor-nop",
                    )
                observed = _run_task(pack.task_directory, stage_root / "logs", image)
                if observed != {"reward": expected_reward, "mutation_status": "passed"}:
                    raise QualificationError(
                        f"session {index} {state} expected reward {expected_reward}, "
                        f"observed {observed}"
                    )
                observations.append(
                    {
                        "session": index,
                        "challenge_id": challenge_id,
                        "state": state,
                        **observed,
                    }
                )
    return {
        "ok": True,
        "zero_model": True,
        "experiment_id": manifest["experiment_id"],
        "task_image": image,
        "observations": observations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    print(json.dumps(qualify(args.source.resolve(), args.manifest.resolve()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
