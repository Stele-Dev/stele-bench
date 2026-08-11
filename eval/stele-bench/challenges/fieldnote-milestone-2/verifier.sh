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
        "level": "INFO" if index % 2 == 0 else "ERROR",
        "message": f"event-{index}",
        "token": f"token-{index}",
        "nested": {"password": f"password-{index}", "safe": index},
        "items": [{"secret": f"secret-{index}"}],
    }
    for index in range(7)
]
with tempfile.TemporaryDirectory() as raw:
    source = Path(raw) / "events.jsonl"
    source.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
    result = subprocess.run(
        ["python3", str(Path(os.environ["FIELDNOTE_WORKSPACE"]) / "fieldnote.py"), str(source)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "FIELDNOTE_SENTINEL": "must-not-appear"},
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["event_count"] == 7
    assert len(report["samples"]) == 5
    encoded = json.dumps(report)
    for index in range(7):
        assert f"token-{index}" not in encoded
        assert f"password-{index}" not in encoded
        assert f"secret-{index}" not in encoded
    assert "must-not-appear" not in encoded
    for sample in report["samples"]:
        assert sample["token"] == "[REDACTED]"
        assert sample["nested"]["password"] == "[REDACTED]"
        assert sample["items"][0]["secret"] == "[REDACTED]"
PY
then
  printf '{"reward":1}\n' > "$logs_root/reward.json"
  printf '{"five_samples":true,"recursive_redaction":true,"environment_not_read":true}\n' > "$logs_root/evidence.json"
else
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"five_samples":false,"recursive_redaction":false,"environment_not_read":false}\n' > "$logs_root/evidence.json"
fi
