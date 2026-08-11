"""Sanitising a borrowed dataset before it is used for a scored comparison."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_prepare_external_dataset", Path(__file__).with_name("prepare_external_dataset.py")
)
assert _SPEC is not None and _SPEC.loader is not None
preparer = importlib.util.module_from_spec(_SPEC)
sys.modules["_prepare_external_dataset"] = preparer
_SPEC.loader.exec_module(preparer)

DOCKERFILE = "FROM buildpack-deps:jammy\nWORKDIR /app\nCOPY workspace/ ./\n"


def _task(root: Path, name: str, *, oracle: bool = True, dockerfile: str = DOCKERFILE) -> Path:
    task = root / name
    workspace = task / "environment" / "workspace"
    workspace.mkdir(parents=True)
    (task / "task.toml").write_text(f'[task]\nname = "{name}"\n', encoding="utf-8")
    (task / "instruction.md").write_text("Solve it.\n", encoding="utf-8")
    (workspace / "solution.py").write_text("def solve():\n    pass\n", encoding="utf-8")
    (task / "environment" / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    # The verifier's tree, which never enters the image — it holds the reference
    # implementation and must survive sanitisation untouched.
    meta = task / "tests" / ".meta"
    meta.mkdir(parents=True)
    (meta / "example.py").write_text("def solve():\n    return 42\n", encoding="utf-8")
    # Every real Harbor task has one, and sanitisation now audits it.
    (task / "tests" / "test.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\npytest\n"
        "echo $? > /logs/verifier/reward.txt\n",
        encoding="utf-8",
    )
    if oracle:
        payload = workspace / ".oracle"
        payload.mkdir()
        (payload / "solution.enc").write_bytes(b"Salted__\x00encrypted")
    return task


def test_the_answer_payload_is_removed_from_every_task(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _task(source, "task-one")
    _task(source, "task-two")

    manifest = preparer.sanitise_dataset(source, tmp_path / "clean")

    assert manifest["tasks"] == 2
    assert manifest["oracle_payloads_removed"] == 2
    assert list((tmp_path / "clean").rglob("solution.enc")) == []
    assert list((tmp_path / "clean").rglob(".oracle")) == []


def test_the_agent_stub_files_survive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _task(source, "task-one")

    preparer.sanitise_dataset(source, tmp_path / "clean")

    workspace = tmp_path / "clean" / "task-one" / "environment" / "workspace"
    assert (workspace / "solution.py").is_file()
    # And the verifier's own tree is untouched — it is not copied into the image,
    # so stripping it would break grading for no safety gain.
    assert (tmp_path / "clean" / "task-one" / "tests" / ".meta" / "example.py").is_file()


def test_the_source_dataset_is_not_modified(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    task = _task(source, "task-one")

    preparer.sanitise_dataset(source, tmp_path / "clean")

    # Sanitising in place over a shared cache would silently break Harbor's
    # oracle agent for everyone else using that download.
    assert (task / "environment" / "workspace" / ".oracle" / "solution.enc").is_file()


def test_a_task_that_copies_the_verifier_tree_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _task(
        source,
        "task-one",
        dockerfile="FROM scratch\nWORKDIR /app\nCOPY workspace/ ./\nCOPY tests/ /tests\n",
    )

    # Removing `.oracle` would not help here: the reference implementation would
    # ride in as tests/.meta/example.*, and the sanitiser must say so rather
    # than report a clean dataset.
    with pytest.raises(preparer.DatasetPreparationError, match="carry the reference solution"):
        preparer.sanitise_dataset(source, tmp_path / "clean")


def test_a_surviving_secret_reference_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    task = _task(source, "task-one")
    (task / "environment" / "workspace" / "README.md").write_text(
        "run with ORACLE_SECRET set\n", encoding="utf-8"
    )

    with pytest.raises(preparer.DatasetPreparationError, match="ORACLE_SECRET"):
        preparer.sanitise_dataset(source, tmp_path / "clean")


def test_the_manifest_pins_what_the_arms_will_see(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _task(source, "task-one")

    preparer.sanitise_dataset(source, tmp_path / "clean")

    manifest = json.loads(
        (tmp_path / "clean" / preparer.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    record = manifest["records"][0]
    assert record["task"] == "task-one"
    assert record["oracle_payload_removed"] is True
    # A content digest of the workspace, so a result can name the exact tree it
    # was produced against rather than a registry tag that may move.
    assert len(record["workspace_sha256"]) == 64


def test_selecting_a_subset_keeps_only_those_tasks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _task(source, "task-one")
    _task(source, "task-two")

    manifest = preparer.sanitise_dataset(
        source, tmp_path / "clean", include=["task-two"]
    )

    assert manifest["tasks"] == 1
    assert not (tmp_path / "clean" / "task-one").exists()


def test_an_existing_destination_is_not_silently_replaced(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _task(source, "task-one")
    (tmp_path / "clean").mkdir()

    with pytest.raises(preparer.DatasetPreparationError, match="already exists"):
        preparer.sanitise_dataset(source, tmp_path / "clean")


def test_a_directory_with_no_tasks_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(preparer.DatasetPreparationError, match="no Harbor tasks"):
        preparer.sanitise_dataset(source, tmp_path / "clean")


def test_verification_pins_what_the_arms_will_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _task(source, "task-one")
    _task(source, "task-two")
    clean = tmp_path / "clean"
    preparer.sanitise_dataset(source, clean)

    verified = preparer.verify_dataset(clean)

    assert verified["tasks"] == ["task-one", "task-two"]
    assert verified["task_count"] == 2
    # The two halves are separate on purpose: what was asked, and how it was
    # graded. A change to either has to be visible as a different digest.
    assert verified["workspaces_sha256"] != verified["tests_sha256"]
    assert len(verified["workspaces_sha256"]) == 64
    assert preparer.verify_dataset(clean) == verified


def test_verification_refuses_a_dataset_that_was_never_sanitised(tmp_path: Path) -> None:
    """A registry download still carries the answer; it must not be runnable."""

    source = tmp_path / "source"
    source.mkdir()
    _task(source, "task-one")

    with pytest.raises(preparer.DatasetPreparationError, match="sanitisation"):
        preparer.verify_dataset(source)


def test_verification_catches_a_payload_restored_after_sanitisation(tmp_path: Path) -> None:
    """Sanitising once is not a permanent property of a directory on a laptop."""

    source = tmp_path / "source"
    source.mkdir()
    _task(source, "task-one")
    clean = tmp_path / "clean"
    preparer.sanitise_dataset(source, clean)

    restored = clean / "task-one" / preparer.WORKSPACE_DIRECTORY / preparer.ORACLE_DIRECTORY
    restored.mkdir()
    (restored / "solution.enc").write_bytes(b"Salted__\x00encrypted")

    with pytest.raises(preparer.DatasetPreparationError, match="survived sanitisation"):
        preparer.verify_dataset(clean)


def test_verification_catches_an_edited_workspace(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _task(source, "task-one")
    clean = tmp_path / "clean"
    preparer.sanitise_dataset(source, clean)

    workspace = clean / "task-one" / preparer.WORKSPACE_DIRECTORY
    (workspace / "solution.py").write_text("def solve():\n    return 42\n", encoding="utf-8")

    with pytest.raises(preparer.DatasetPreparationError, match="no longer matches"):
        preparer.verify_dataset(clean)


def test_a_task_with_nothing_to_grade_it_is_refused(tmp_path: Path) -> None:
    """Caught at preparation now, which is earlier than the run that needs it."""

    source = tmp_path / "source"
    source.mkdir()
    task = _task(source, "task-one")
    shutil.rmtree(task / "tests")

    with pytest.raises(preparer.DatasetPreparationError, match="no tests/test.sh"):
        preparer.sanitise_dataset(source, tmp_path / "clean")


def _compiled_task(root: Path, name: str) -> Path:
    """A task shaped like aider's compiled-language ones: it can exit early."""

    task = _task(root, name)
    (task / "tests" / "test.sh").write_text(
        "#!/bin/bash\n"
        "set -Eeuo pipefail\n"
        "cd /app\n"
        'if [ ! -d "$TEST_DIR" ]; then\n'
        "    echo 0 > /logs/verifier/reward.txt\n"
        "    exit 0\n"
        "fi\n"
        "if cmake .. && make; then\n"
        "    ./tests\n"
        "else\n"
        '    echo "CMake build failed"\n'
        "    exit 1\n"
        "fi\n"
        "exit_code=$?\n"
        "if [ $exit_code -eq 0 ]; then\n"
        "  echo 1 > /logs/verifier/reward.txt\n"
        "else\n"
        "  echo 0 > /logs/verifier/reward.txt\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    return task


def test_a_build_failure_is_graded_as_a_failure_not_an_infrastructure_error(
    tmp_path: Path,
) -> None:
    """The bug this exists for, and why it is not cosmetic.

    Every compiled-language aider task exits early on "build failed" without
    reaching its own reward write. Harbor then reports no reward at all, which
    our runner scores as `infrastructure-error` — and an excluded trial leaves
    the denominator rather than scoring zero. A compile failure is the most
    common failure mode for a weak model, so the arm that fails to compile more
    often would look BETTER: its worst results vanish instead of counting.
    """

    source = tmp_path / "source"
    source.mkdir()
    _compiled_task(source, "task-one")
    clean = tmp_path / "clean"

    manifest = preparer.sanitise_dataset(source, clean)

    assert manifest["reward_failsafes_added"] == 1
    script = (clean / "task-one" / "tests" / "test.sh").read_text(encoding="utf-8")
    # Armed after `set -e`, so it covers the failures that setting enables.
    assert script.index("trap ") > script.index("set -Eeuo pipefail")
    assert script.index("trap ") < script.index("cmake")
    # It can only ever ADD a verdict, never replace one the script wrote.
    assert "[ -f /logs/verifier/reward.txt ] ||" in script


def test_the_failsafe_never_overrides_the_verifier_s_own_verdict(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _compiled_task(source, "task-one")
    clean = tmp_path / "clean"
    preparer.sanitise_dataset(source, clean)

    script = (clean / "task-one" / "tests" / "test.sh").read_text(encoding="utf-8")
    # Both of the script's own writes survive untouched.
    assert "echo 1 > /logs/verifier/reward.txt" in script
    assert "echo 0 > /logs/verifier/reward.txt" in script


def test_preparing_a_dataset_twice_does_not_patch_it_twice(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _compiled_task(source, "task-one")
    clean = tmp_path / "clean"
    preparer.sanitise_dataset(source, clean)
    once = (clean / "task-one" / "tests" / "test.sh").read_text(encoding="utf-8")

    preparer.sanitise_dataset(clean, tmp_path / "clean2")
    twice = (tmp_path / "clean2" / "task-one" / "tests" / "test.sh").read_text(
        encoding="utf-8"
    )

    assert once == twice
    assert twice.count("trap ") == 1


def test_verification_refuses_a_dataset_whose_failsafe_was_stripped(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _compiled_task(source, "task-one")
    clean = tmp_path / "clean"
    preparer.sanitise_dataset(source, clean)

    script = clean / "task-one" / "tests" / "test.sh"
    script.write_text(
        "\n".join(
            line
            for line in script.read_text(encoding="utf-8").splitlines()
            if "trap " not in line and preparer.REWARD_FAILSAFE_MARKER not in line
        ),
        encoding="utf-8",
    )

    with pytest.raises(preparer.DatasetPreparationError, match="reward fail-safe"):
        preparer.verify_dataset(clean)
