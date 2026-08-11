#!/bin/sh
set -eu

logs_root=${MUTATION_LOGS_ROOT:-/logs/verifier}
mkdir -p "$logs_root"

if PYTHONPATH=/workspace/src python3 >"$logs_root/stdout.txt" 2>"$logs_root/stderr.txt" <<'PY'
import gc

from click.testing import StreamMixer

events = []


class Probe:
    def __init__(self, name):
        self.name = name

    def close(self):
        events.append(self.name)


mixer = object.__new__(StreamMixer)
mixer.output = Probe("output")
mixer.stdout = Probe("stdout")
mixer.stderr = Probe("stderr")
del mixer
gc.collect()
assert events == ["stderr", "stdout", "output"], events
PY
then
  printf '{"reward":1}\n' > "$logs_root/reward.json"
  printf '{"dependency_close_order":true}\n' > "$logs_root/evidence.json"
else
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"dependency_close_order":false}\n' > "$logs_root/evidence.json"
fi
