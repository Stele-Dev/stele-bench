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
    source = root / "events.jsonl"
    source.write_text(
        '\n'.join([
            json.dumps({"level": "INFO", "message": "started"}),
            json.dumps({"level": "error", "message": "failed"}),
            json.dumps({"level": "Info", "message": "done"}),
        ]) + '\n',
        encoding="utf-8",
    )
    command = ["python3", str(Path(os.environ["FIELDNOTE_WORKSPACE"]) / "fieldnote.py"), str(source)]
    first = subprocess.run(command, check=False, capture_output=True, text=True)
    second = subprocess.run(command, check=False, capture_output=True, text=True)
    assert first.returncode == second.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == {
        "event_count": 3,
        "levels": {"error": 1, "info": 2},
    }
    assert len(first.stdout.encode()) <= 50 * 1024
    output = root / "report.json"
    written = subprocess.run(command + ["--output", str(output)], check=False)
    assert written.returncode == 0
    assert output.read_text(encoding="utf-8") == first.stdout
    invalid = root / "invalid.jsonl"
    invalid.write_text("not-json\n", encoding="utf-8")
    rejected = subprocess.run(
        ["python3", str(Path(os.environ["FIELDNOTE_WORKSPACE"]) / "fieldnote.py"), str(invalid)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 2
    assert rejected.stderr.strip()
PY
then
  printf '{"reward":1}\n' > "$logs_root/reward.json"
  printf '{"core_summary":true,"deterministic":true,"invalid_exit_2":true}\n' > "$logs_root/evidence.json"
else
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"core_summary":false,"deterministic":false,"invalid_exit_2":false}\n' > "$logs_root/evidence.json"
fi
