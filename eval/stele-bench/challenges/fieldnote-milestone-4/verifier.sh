#!/bin/sh
set -eu

logs_root=${MUTATION_LOGS_ROOT:-/logs/verifier}
workspace_root=${MUTATION_WORKSPACE_ROOT:-/workspace}
mkdir -p "$logs_root"

if FIELDNOTE_WORKSPACE="$workspace_root" python3 >"$logs_root/stdout.txt" 2>"$logs_root/stderr.txt" <<'PY'
import json
import os
import subprocess
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as raw:
    root = Path(raw)
    version = subprocess.run(
        ["python3", str(Path(os.environ["FIELDNOTE_WORKSPACE"]) / "fieldnote.py"), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert version.returncode == 0
    assert version.stdout.strip() == "fieldnote 1.0.0"
    invalid = root / "invalid.jsonl"
    invalid.write_text("not-json\n", encoding="utf-8")
    output = root / "report.json"
    output.write_text("keep-me\n", encoding="utf-8")
    rejected = subprocess.run(
        ["python3", str(Path(os.environ["FIELDNOTE_WORKSPACE"]) / "fieldnote.py"), str(invalid), "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 2
    assert "traceback" not in rejected.stderr.lower()
    assert output.read_text(encoding="utf-8") == "keep-me\n"
    source = root / "events.jsonl"
    source.write_text(json.dumps({"level": "INFO", "token": "hide"}) + "\n", encoding="utf-8")
    completed = subprocess.run(
        ["python3", str(Path(os.environ["FIELDNOTE_WORKSPACE"]) / "fieldnote.py"), str(source), "--output", str(output)],
        check=False,
    )
    assert completed.returncode == 0
    assert "hide" not in output.read_text(encoding="utf-8")
    assert not list(root.glob(".report.json.*"))
    readme = (Path(os.environ["FIELDNOTE_WORKSPACE"]) / "README.md").read_text(encoding="utf-8").lower()
    for marker in ("standard library", "network", "watch mode", "50 kib", "--include-events"):
        assert marker in readme
PY
then
  printf '{"reward":1}\n' > "$logs_root/reward.json"
  printf '{"version":true,"stable_errors":true,"atomic_output":true,"release_contract":true}\n' > "$logs_root/evidence.json"
else
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"version":false,"stable_errors":false,"atomic_output":false,"release_contract":false}\n' > "$logs_root/evidence.json"
fi
