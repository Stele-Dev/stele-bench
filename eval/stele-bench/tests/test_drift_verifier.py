"""The dotfile-drift-detection floor check, executed for real.

The memory signal for this challenge is spoken, so it is graded host-side from
the transcript (see the runner's response-marker tests). What this verifier
checks is the floor the container CAN see: the first turn said not to change any
code yet, so a workspace with no drift/watcher implementation means the agent
advised rather than going off and building it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from stele_bench.challenges import select_challenges

CHALLENGE = select_challenges(("dotfile-drift-detection",))[0]
VERIFIER = CHALLENGE.verifier_path


def _run(tmp_path: Path, *, source_files: tuple[str, ...] = ()) -> dict[str, object]:
    workspace = tmp_path / "workspace"
    logs = tmp_path / "logs"
    (workspace / "src").mkdir(parents=True)
    logs.mkdir()
    for name in source_files:
        (workspace / "src" / name).write_text("// code\n", encoding="utf-8")
    result = subprocess.run(
        [str(VERIFIER)],
        env={
            **os.environ,
            "MUTATION_WORKSPACE_ROOT": str(workspace),
            "MUTATION_LOGS_ROOT": str(logs),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stderr == ""
    return {
        "reward": json.loads((logs / "reward.json").read_text())["reward"],
        **json.loads((logs / "evidence.json").read_text()),
    }


def test_advice_only_workspace_satisfies_the_floor(tmp_path: Path) -> None:
    assert _run(tmp_path, source_files=("app.rs", "sync_service.rs")) == {
        "reward": 1,
        "advice_only": 1,
    }


def test_building_the_thing_instead_of_advising_fails_the_floor(tmp_path: Path) -> None:
    for name in ("drift_detector.rs", "file_watcher.rs", "daemon.rs"):
        graded = _run(tmp_path / name, source_files=(name,))
        assert graded == {"reward": 0, "advice_only": 0}, name


def test_challenge_is_response_graded_with_six_word_final_turn() -> None:
    manifest = CHALLENGE.manifest
    assert manifest.is_response_graded
    assert manifest.is_multi_step
    # The final turn stays bare: an earlier revision appended "write your answer
    # to <path>", which made "your answer" ambiguous (answer to WHICH question?)
    # and split the two arms onto different questions. Every added instruction is
    # a chance to leak the concept or to confuse the ask.
    assert manifest.follow_up_prompt is not None
    assert manifest.follow_up_prompt.strip() == "can we do this in the background?"
    for banned in ("background", "periodic", "daemon", "resident"):
        assert banned not in manifest.prompt.casefold(), banned
