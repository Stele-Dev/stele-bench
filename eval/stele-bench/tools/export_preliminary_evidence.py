#!/usr/bin/env python3
"""Export a redacted, hashed v0.1 evidence bundle from retained Harbor runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

BENCH_ROOT = Path(__file__).resolve().parents[1]
PUBLICATION_ROOT = BENCH_ROOT / "results" / "2026-08-preliminary"
DEFAULT_STATE_ROOT = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
) / "stele-bench"

ROOT_FILES = ("certification.json", "repeat.json")
TRIAL_FILES = ("config.json", "result.json", "trial.log")
AGENT_FILES = ("trajectory.json", "injection-channel.json", "preflight.json")
VERIFIER_FILES = ("evidence.json", "mutation.json", "reward.json", "test-stdout.txt")

SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"sb_secret_[A-Za-z0-9_-]{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
BRIDGE_TOKEN = re.compile(r"bridge_token=[^&\s\"']+")


class ExportError(RuntimeError):
    pass


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _redact(data: bytes, *, state_root: Path) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportError("the v0.1 exporter only accepts UTF-8 evidence") from error

    text = BRIDGE_TOKEN.sub("bridge_token=<redacted>", text)
    text = text.replace("ghp_your_token_here", "github-token-placeholder")
    text = text.replace(os.fspath(state_root), "$STELE_BENCH_STATE")
    text = text.replace(os.fspath(Path.home()), "$HOME")
    for pattern in SECRET_PATTERNS:
        matches = [match.group(0) for match in pattern.finditer(text)]
        if matches:
            raise ExportError(f"refusing to export text matching {pattern.pattern!r}")
    return text.encode("utf-8")


def _copy_redacted(source: Path, destination: Path, *, state_root: Path) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_redact(source.read_bytes(), state_root=state_root))


def _attempt_id(payload: dict[str, Any], fallback: str) -> str:
    receipt = payload.get("receipt")
    if isinstance(receipt, dict) and isinstance(receipt.get("run_id"), str):
        return receipt["run_id"]
    return fallback.removesuffix(".json")


def _diagnostic_paths(payload: dict[str, Any]) -> list[Path]:
    receipt = payload.get("receipt")
    if not isinstance(receipt, dict):
        return []
    diagnostics = receipt.get("harbor_diagnostics")
    if not isinstance(diagnostics, list):
        return []
    paths: list[Path] = []
    for item in diagnostics:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            paths.append(Path(item["path"]))
    return paths


def _export_trial(work_root: Path, destination: Path, *, state_root: Path) -> None:
    trajectories = sorted(work_root.rglob("agent/trajectory.json"))
    if not trajectories:
        return

    trial_roots = {
        next(
            parent
            for parent in trajectory.parents
            if (parent / "config.json").is_file()
            and (parent / "result.json").is_file()
        )
        for trajectory in trajectories
    }
    if len(trial_roots) != 1:
        raise ExportError(f"could not identify one Harbor trial under {work_root}")
    trial_root = trial_roots.pop()
    route_root = trial_root.parent
    _copy_redacted(
        work_root / "harbor.diagnostic.json",
        destination / "diagnostic.json",
        state_root=state_root,
    )
    for name in TRIAL_FILES:
        _copy_redacted(trial_root / name, destination / "harbor" / name, state_root=state_root)
    for name in ("config.json", "result.json", "job.log"):
        _copy_redacted(
            route_root / name,
            destination / "harbor" / f"job-{name}",
            state_root=state_root,
        )
    for name in AGENT_FILES:
        for source in sorted(trial_root.rglob(name)):
            _copy_redacted(
                source,
                destination / source.relative_to(trial_root),
                state_root=state_root,
            )
    for name in VERIFIER_FILES:
        _copy_redacted(
            trial_root / "verifier" / name,
            destination / "verifier" / name,
            state_root=state_root,
        )
    _copy_redacted(
        trial_root / "artifacts" / "manifest.json",
        destination / "harbor" / "artifact-manifest.json",
        state_root=state_root,
    )


def _export_run(
    admission: dict[str, Any],
    *,
    state_root: Path,
    destination: Path,
) -> None:
    run_id = admission["run_id"]
    run_root = state_root / "runs" / run_id
    run_destination = destination / admission["decision"] / run_id
    if not run_root.is_dir():
        raise ExportError(f"retained run state is missing: {run_root}")
    run_destination.mkdir(parents=True, exist_ok=True)

    (run_destination / "admission.json").write_text(
        json.dumps(admission, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for name in ROOT_FILES:
        _copy_redacted(run_root / name, run_destination / name, state_root=state_root)

    result = admission.get("result")
    if isinstance(result, str):
        source = (PUBLICATION_ROOT / result).resolve()
        _copy_redacted(source, run_destination / "result.json", state_root=state_root)

    for attempt_path in sorted(run_root.glob("attempt-*.json")):
        payload = _read_json(attempt_path)
        if not isinstance(payload, dict):
            raise ExportError(f"attempt receipt is not an object: {attempt_path}")
        attempt_destination = run_destination / "attempts" / _attempt_id(
            payload, attempt_path.name
        )
        _copy_redacted(
            attempt_path,
            attempt_destination / "receipt.json",
            state_root=state_root,
        )
        diagnostic_paths = _diagnostic_paths(payload)
        if len(diagnostic_paths) > 1:
            raise ExportError(f"multiple Harbor diagnostics in {attempt_path}")
        if diagnostic_paths:
            _export_trial(
                diagnostic_paths[0].parent,
                attempt_destination,
                state_root=state_root,
            )


def _write_manifest(destination: Path, admissions: list[dict[str, Any]]) -> None:
    files = []
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(destination).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    payload = {
        "schema_version": 1,
        "release": "v0.1",
        "source": "retained local Harbor run state",
        "redactions": [
            "host state root replaced with $STELE_BENCH_STATE",
            "host home replaced with $HOME",
            "ephemeral semantic bridge tokens replaced with <redacted>",
            "example GitHub token prefixes replaced with github-token-placeholder",
        ],
        "admission_counts": {
            "accepted": sum(item["decision"] == "accepted" for item in admissions),
            "excluded": sum(item["decision"] == "excluded" for item in admissions),
        },
        "files": files,
    }
    (destination / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def export(*, state_root: Path, replace: bool) -> Path:
    admissions_payload = _read_json(PUBLICATION_ROOT / "admissions.json")
    admissions = admissions_payload.get("runs")
    if not isinstance(admissions, list) or not all(isinstance(item, dict) for item in admissions):
        raise ExportError("admissions.json has no runs list")

    for category in ("accepted", "excluded"):
        path = PUBLICATION_ROOT / category
        if path.exists():
            if not replace:
                raise ExportError(f"evidence already exists: {path}; pass --replace")
            shutil.rmtree(path)

    for admission in admissions:
        _export_run(admission, state_root=state_root, destination=PUBLICATION_ROOT)
    _write_manifest(PUBLICATION_ROOT, admissions)
    return PUBLICATION_ROOT / "manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        manifest = export(state_root=args.state_root.resolve(), replace=args.replace)
    except (ExportError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
