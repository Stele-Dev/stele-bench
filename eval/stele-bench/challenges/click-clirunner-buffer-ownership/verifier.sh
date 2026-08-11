#!/bin/sh
set -eu

logs_root=${MUTATION_LOGS_ROOT:-/logs/verifier}
mkdir -p "$logs_root"

if PYTHONPATH=/workspace/src python3 >"$logs_root/stdout.txt" 2>"$logs_root/stderr.txt" <<'PY'
from click.testing import _NamedTextIOWrapper
from click.testing import StreamMixer

mixer = StreamMixer()
stdout = _NamedTextIOWrapper(
    mixer.stdout, encoding="utf-8", name="<stdout>", mode="w"
)
stderr = _NamedTextIOWrapper(
    mixer.stderr, encoding="utf-8", name="<stderr>", mode="w"
)
stdout.write("out")
stderr.write("err")
stdout.flush()
stderr.flush()
stdout.close()
stderr.close()
assert not mixer.stdout.closed
assert not mixer.stderr.closed
assert not mixer.output.closed
assert mixer.stdout.getvalue() == b"out"
assert mixer.stderr.getvalue() == b"err"
assert mixer.output.getvalue() == b"outerr"
PY
then
  printf '{"reward":1}\n' > "$logs_root/reward.json"
  printf '{"single_buffer_owner":true}\n' > "$logs_root/evidence.json"
else
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"single_buffer_owner":false}\n' > "$logs_root/evidence.json"
fi
