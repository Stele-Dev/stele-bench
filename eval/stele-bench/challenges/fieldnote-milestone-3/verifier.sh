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

events = [
    {
        "id": index,
        "level": "INFO",
        "message": f"event-{index}-" + ("x" * 220),
        "token": f"never-show-{index}",
    }
    for index in range(320)
]
with tempfile.TemporaryDirectory() as raw:
    source = Path(raw) / "events.jsonl"
    source.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
    base = ["python3", str(Path(os.environ["FIELDNOTE_WORKSPACE"]) / "fieldnote.py"), str(source)]
    default = subprocess.run(base, check=False, capture_output=True, text=True)
    assert default.returncode == 0, default.stderr
    assert len(default.stdout.encode()) <= 50 * 1024
    assert json.loads(default.stdout)["event_count"] == len(events)
    html = subprocess.run(
        base + ["--format", "html", "--include-events"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert html.returncode == 0, html.stderr
    assert html.stdout.lstrip().lower().startswith("<!doctype html")
    assert len(html.stdout.encode()) > 50 * 1024
    assert "event-319-" in html.stdout
    assert "never-show-0" not in html.stdout
    assert "[REDACTED]" in html.stdout
PY
then
  printf '{"reward":1}\n' > "$logs_root/reward.json"
  printf '{"html_report":true,"full_sanitized_events":true,"size_exception":true}\n' > "$logs_root/evidence.json"
else
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"html_report":false,"full_sanitized_events":false,"size_exception":false}\n' > "$logs_root/evidence.json"
fi
