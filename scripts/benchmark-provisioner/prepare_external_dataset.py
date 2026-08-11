#!/usr/bin/env python3
"""Sanitise a borrowed Harbor dataset before it is used for a scored comparison.

Borrowed task sets are built for leaderboard runs, where a shared cheat inflates
every model about equally and mostly does not change the ranking. A *paired*
comparison has no such tolerance: anything the two arms can reach unequally
becomes the result.

`aider/aider-polyglot` ships the reference solution inside the agent's own
working directory — `environment/workspace/.oracle/solution.enc`, AES-256-CBC
with the password `tb-secret-please-change` hardcoded as the default in the
adapter's public `solve.sh`, and the Dockerfile does `COPY workspace/ ./`. One
`openssl enc -d` yields the reference files plus a manifest saying where each one
goes.

For us that is not a shared cheat, it is a **differential** one. The Stele arm is
instructed to investigate before acting and its discovery hooks watch file reads;
it explores the filesystem more than the control arm by design, so it is more
likely to notice a dotdir sitting in its working directory. If it cheats even
slightly more often, Stele posts a correctness gain that has nothing to do with
memory — a spectacular false positive on the one family whose job is to be the
honest regression guard.

So: copy the dataset, strip the payload, prove it is gone, and record a digest of
what the arms will actually see. Running from the sanitised copy (`harbor run -p`)
rather than the registry is also what makes the run reproducible and auditable —
the tasks stop being a moving target.

A sanitised dataset cannot run Harbor's `oracle` agent, which needs the payload
it just lost. We never run it; that is why this writes a copy instead of editing
a shared cache in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

# The directory the reference solution ships in, relative to a task's workspace.
ORACLE_DIRECTORY = ".oracle"
WORKSPACE_DIRECTORY = "environment/workspace"

# Anything matching these under a task workspace means the answer is reachable
# from inside the container. Names, not contents: the payload is encrypted, so
# scanning bytes for solution text finds nothing.
FORBIDDEN_NAMES = ("solution.enc", "solution.tar", ORACLE_DIRECTORY)
FORBIDDEN_TEXT = ("ORACLE_SECRET", "tb-secret-please-change")

# The image may only take the agent's workspace. A task that also copied `tests/`
# would reintroduce the answer as `tests/.meta/example.*`, which no amount of
# stripping `.oracle` would catch.
ALLOWED_COPY_SOURCES = ("workspace/",)

MANIFEST_NAME = "sanitisation.json"

# --- The verifier's own rule, applied on the path where it forgets ----------
#
# `aider-polyglot`'s `tests/test.sh` ends with its verdict:
#
#     if [ $exit_code -eq 0 ]; then echo 1 > /logs/verifier/reward.txt
#     else                          echo 0 > /logs/verifier/reward.txt; fi
#
# but every compiled-language task (cpp, java, rust — 103 of 225) also has
# earlier `exit 1` paths for "build failed" that never reach it. Harbor then
# finds no reward file and raises `RewardFileNotFoundError`, which our runner
# scores as `infrastructure-error` rather than a failure.
#
# That is not a cosmetic mismatch, it is a differential-validity threat. A
# compile failure is the MOST common failure mode for a weak model on these
# exercises, and an excluded trial leaves the denominator rather than scoring
# zero — so the arm that fails to compile more often looks BETTER, because its
# worst results vanish from the analysis instead of counting against it.
#
# The fix writes the verdict the script's own rule already implies, and only
# when the script wrote none: `[ -f ... ] ||`. It can never override a real
# verdict, and a passing run is untouched.
#
# The `EXIT` trap is deliberately narrow. Bash runs it when the script decides
# to stop — a `set -e` failure or an explicit `exit 1` — and does NOT run it
# when the process is killed. So a build failure becomes an honest 0, while a
# verifier that was OOM-killed or timed out still produces no reward file and
# is still reported as infrastructure. That distinction comes from bash
# semantics rather than from us guessing at somebody else's output.
REWARD_PATH = "/logs/verifier/reward.txt"
REWARD_FAILSAFE = (
    "# stele-bench: apply this script's own rule "
    "(nonzero exit -> reward 0) on the early-exit paths that skip it.\n"
    f"trap '[ -f {REWARD_PATH} ] || echo 0 > {REWARD_PATH}' EXIT\n"
)
REWARD_FAILSAFE_MARKER = "# stele-bench:"


class DatasetPreparationError(RuntimeError):
    """A borrowed dataset could not be made safe for a scored comparison."""


def _task_directories(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and (path / "task.toml").is_file()
    )


def _tree_digest(root: Path) -> str:
    """Content digest of a task's workspace, so a run can pin what it ran on."""

    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _audit_dockerfile(task: Path) -> None:
    dockerfile = task / "environment" / "Dockerfile"
    if not dockerfile.is_file():
        return
    for line in dockerfile.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith(("COPY ", "ADD ")):
            continue
        parts = stripped.split()
        # Everything between the verb and the destination is a source.
        for source in parts[1:-1]:
            if source.startswith("--"):
                continue
            if source not in ALLOWED_COPY_SOURCES:
                raise DatasetPreparationError(
                    f"{task.name}: image copies {source!r}, which may carry the "
                    "reference solution into the container"
                )


def _install_reward_failsafe(task: Path) -> bool:
    """Make the task's own verdict reachable from its early-exit paths.

    Inserted after the leading `#!`/`set` block so it is armed before anything
    can fail, and idempotent — a dataset prepared twice is not patched twice.
    """

    script = task / "tests" / "test.sh"
    if not script.is_file() or script.is_symlink():
        return False
    text = script.read_text(encoding="utf-8", errors="replace")
    if REWARD_FAILSAFE_MARKER in text:
        return False
    lines = text.splitlines(keepends=True)
    insert_at = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index == 0 and stripped.startswith("#!"):
            insert_at = 1
            continue
        if stripped.startswith("#") or not stripped:
            insert_at = index + 1
            continue
        if stripped.startswith("set "):
            # After `set -e`, so the trap is armed for the failures it enables.
            insert_at = index + 1
            continue
        break
    lines.insert(insert_at, REWARD_FAILSAFE)
    script.write_text("".join(lines), encoding="utf-8")
    return True


def _audit_reward_failsafe(task: Path) -> None:
    """Fail closed if a task can still finish without a verdict."""

    script = task / "tests" / "test.sh"
    if not script.is_file():
        raise DatasetPreparationError(f"{task.name}: has no tests/test.sh to grade with")
    text = script.read_text(encoding="utf-8", errors="replace")
    if REWARD_FAILSAFE_MARKER not in text or "trap " not in text:
        raise DatasetPreparationError(
            f"{task.name}: reward fail-safe is missing; a build failure would be "
            "recorded as an infrastructure error instead of a failed task"
        )


def _audit_workspace(task: Path) -> None:
    workspace = task / WORKSPACE_DIRECTORY
    if not workspace.is_dir():
        return
    for path in workspace.rglob("*"):
        if path.name in FORBIDDEN_NAMES:
            raise DatasetPreparationError(
                f"{task.name}: {path.relative_to(task)} survived sanitisation"
            )
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in FORBIDDEN_TEXT:
            if marker in text:
                raise DatasetPreparationError(
                    f"{task.name}: {path.relative_to(task)} still references {marker!r}"
                )


def sanitise_dataset(
    source: Path,
    destination: Path,
    *,
    include: Iterable[str] | None = None,
    overwrite: bool = False,
) -> dict[str, object]:
    """Copy a dataset without its answer payloads, and prove they are gone."""

    source = source.resolve(strict=True)
    tasks = _task_directories(source)
    if not tasks:
        raise DatasetPreparationError(f"no Harbor tasks found under {source}")
    selected = [task for task in tasks if include is None or task.name in set(include)]
    if not selected:
        raise DatasetPreparationError("task selection matched nothing")

    destination = destination.resolve()
    if destination.exists():
        if not overwrite:
            raise DatasetPreparationError(
                f"destination already exists: {destination} (pass --overwrite)"
            )
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    records: list[dict[str, object]] = []
    for task in selected:
        target = destination / task.name
        shutil.copytree(task, target, symlinks=True)
        oracle = target / WORKSPACE_DIRECTORY / ORACLE_DIRECTORY
        removed = oracle.is_dir()
        if removed:
            shutil.rmtree(oracle)
        patched = _install_reward_failsafe(target)
        # Audit AFTER removal, and audit the copy rather than the source: what
        # matters is the tree the arms will actually be built from.
        _audit_dockerfile(target)
        _audit_workspace(target)
        _audit_reward_failsafe(target)
        records.append(
            {
                "task": task.name,
                "oracle_payload_removed": removed,
                "reward_failsafe_added": patched,
                "workspace_sha256": _tree_digest(target / WORKSPACE_DIRECTORY),
            }
        )

    stripped = sum(1 for record in records if record["oracle_payload_removed"])
    failsafed = sum(1 for record in records if record["reward_failsafe_added"])
    manifest = {
        "schema_version": 2,
        "source": str(source),
        "tasks": len(records),
        "oracle_payloads_removed": stripped,
        "reward_failsafes_added": failsafed,
        "forbidden_names": list(FORBIDDEN_NAMES),
        "forbidden_text": list(FORBIDDEN_TEXT),
        "allowed_copy_sources": list(ALLOWED_COPY_SOURCES),
        "records": records,
    }
    (destination / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def verify_dataset(dataset: Path) -> dict[str, object]:
    """Re-prove a sanitised dataset is safe, and pin what a run will execute.

    Sanitisation happens once; a paid run may happen weeks later against a
    directory anyone could have touched. So the audits run again here, over the
    tree as it stands, rather than trusting the manifest that was written when
    it was made.

    Two digests come back, and they map onto the two things that must match for
    two runs to be comparable at all: the *workspaces* are the question the arms
    were asked, and the *tests* are how the answer was graded. A borrowed
    dataset owns both — we do not write either — so a silent upstream change to
    either one has to break the trend line rather than move it.
    """

    dataset = dataset.resolve(strict=True)
    tasks = _task_directories(dataset)
    if not tasks:
        raise DatasetPreparationError(f"no Harbor tasks found under {dataset}")
    manifest_path = dataset / MANIFEST_NAME
    if not manifest_path.is_file():
        raise DatasetPreparationError(
            f"{dataset} carries no {MANIFEST_NAME}; it was not produced by this tool"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded = {
        str(record.get("task")): record
        for record in manifest.get("records", ())
        if isinstance(record, dict)
    }

    workspace_digest = hashlib.sha256()
    tests_digest = hashlib.sha256()
    for task in tasks:
        _audit_dockerfile(task)
        _audit_workspace(task)
        _audit_reward_failsafe(task)
        workspace = _tree_digest(task / WORKSPACE_DIRECTORY)
        declared = recorded.get(task.name, {}).get("workspace_sha256")
        if declared is not None and declared != workspace:
            raise DatasetPreparationError(
                f"{task.name}: workspace no longer matches the sanitisation manifest"
            )
        workspace_digest.update(task.name.encode("utf-8"))
        workspace_digest.update(b"\0")
        workspace_digest.update(bytes.fromhex(workspace))
        tests = task / "tests"
        if not tests.is_dir():
            # A borrowed task with no verifier tree cannot be graded by its own
            # benchmark, which is the whole reason for borrowing it.
            raise DatasetPreparationError(f"{task.name}: has no tests/ directory to grade with")
        tests_digest.update(task.name.encode("utf-8"))
        tests_digest.update(b"\0")
        tests_digest.update(bytes.fromhex(_tree_digest(tests)))

    return {
        "path": str(dataset),
        "tasks": [task.name for task in tasks],
        "task_count": len(tasks),
        "workspaces_sha256": workspace_digest.hexdigest(),
        "tests_sha256": tests_digest.hexdigest(),
        "sanitisation_manifest_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="downloaded dataset directory")
    parser.add_argument("destination", type=Path, help="sanitised copy to write")
    parser.add_argument(
        "--include",
        action="append",
        dest="include",
        help="task name to include (repeat); default is every task",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest = sanitise_dataset(
            args.source,
            args.destination,
            include=args.include,
            overwrite=args.overwrite,
        )
    except (DatasetPreparationError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "destination": str(args.destination.resolve()),
                "tasks": manifest["tasks"],
                "oracle_payloads_removed": manifest["oracle_payloads_removed"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
