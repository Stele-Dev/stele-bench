#!/usr/bin/env python3
"""Build the deterministic standalone Fieldnote benchmark repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STAGES_ROOT = REPOSITORY_ROOT / "eval" / "stele-bench" / "scenarios" / "fieldnote-v1" / "stages"
MARKER = ".fieldnote-benchmark-source"
FIXED_ENV = {
    "GIT_AUTHOR_NAME": "Fieldnote Fixture",
    "GIT_AUTHOR_EMAIL": "fieldnote@example.invalid",
    "GIT_COMMITTER_NAME": "Fieldnote Fixture",
    "GIT_COMMITTER_EMAIL": "fieldnote@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


class BuildError(RuntimeError):
    pass


def _run(repository: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repository), *args],
        input=input_bytes,
        check=False,
        capture_output=True,
        env={**os.environ, **FIXED_ENV},
    )
    if completed.returncode:
        raise BuildError(completed.stderr.decode("utf-8", errors="replace").strip())
    return completed.stdout


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sync_stage(stage: int, repository: Path) -> None:
    source = STAGES_ROOT / str(stage)
    if not source.is_dir():
        raise BuildError(f"missing scenario stage: {source}")
    for child in repository.iterdir():
        if child.name in {".git", MARKER}:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in source.iterdir():
        target = repository / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    (repository / "fieldnote.py").chmod(0o755)


def _reachable_identity(repository: Path, commit: str) -> dict[str, object]:
    object_ids = sorted(
        line.split(" ", 1)[0]
        for line in _run(repository, "rev-list", "--objects", commit).decode().splitlines()
        if line
    )
    return {
        "head_tree": _run(repository, "show", "-s", "--format=%T", commit).decode().strip(),
        "commit_count": int(_run(repository, "rev-list", "--count", commit).decode()),
        "object_count": len(object_ids),
        "object_ids_sha256": _sha256(("\n".join(object_ids) + "\n").encode()),
    }


def build(destination: Path) -> dict[str, object]:
    destination = destination.resolve(strict=False)
    if destination.exists():
        raise BuildError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="fieldnote-v1-", dir=destination.parent))
    try:
        _run(temporary, "init", "-q", "--initial-branch=main")
        (temporary / MARKER).write_text("fieldnote-v1\n", encoding="utf-8")
        commits: list[str] = []
        for stage in range(5):
            _sync_stage(stage, temporary)
            _run(temporary, "add", "README.md", "fieldnote.py")
            _run(temporary, "commit", "-q", "-m", f"fieldnote milestone {stage}")
            commits.append(_run(temporary, "rev-parse", "HEAD").decode().strip())
        records = []
        for index in range(4):
            base = commits[index]
            target = commits[index + 1]
            identity = _reachable_identity(temporary, base)
            patch = _run(temporary, "diff", "--binary", base, target)
            records.append(
                {
                    "index": index + 1,
                    "base_revision": base,
                    "canonical_target_revision": target,
                    "canonical_target_tree": _run(
                        temporary, "show", "-s", "--format=%T", target
                    ).decode().strip(),
                    "canonical_patch_sha256": _sha256(patch),
                    "reachable_commit_count": identity["commit_count"],
                    "reachable_object_count": identity["object_count"],
                    "reachable_object_ids_sha256": identity["object_ids_sha256"],
                    "base_tree": identity["head_tree"],
                }
            )
        _run(temporary, "remote")
        temporary.rename(destination)
        return {
            "schema_version": 1,
            "experiment_id": "fieldnote-continuity-v8",
            "repository": os.fspath(destination),
            "commits": commits,
            "sessions": records,
        }
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    receipt = build(args.destination)
    payload = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.receipt is not None:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
