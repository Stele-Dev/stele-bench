#!/bin/sh
set -eu

logs_root=${MUTATION_LOGS_ROOT:-/logs/verifier}
mkdir -p "$logs_root"

if PYTHONPATH=/workspace/src python3 >"$logs_root/stdout.txt" 2>"$logs_root/stderr.txt" <<'PY'
import faulthandler

import click
from click.testing import CliRunner


@click.command()
def cli():
    faulthandler.enable()
    faulthandler.disable()
    click.echo("after-faulthandler")


try:
    result = CliRunner().invoke(cli)
finally:
    faulthandler.disable()
assert result.exit_code == 0, result.exception
assert result.output == "after-faulthandler\n"
PY
then
  printf '{"reward":1}\n' > "$logs_root/reward.json"
  printf '{"fileno_consumer_compatible":true}\n' > "$logs_root/evidence.json"
else
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"fileno_consumer_compatible":false}\n' > "$logs_root/evidence.json"
fi
