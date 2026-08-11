#!/bin/sh
set -eu

logs_root=${MUTATION_LOGS_ROOT:-/logs/verifier}
mkdir -p "$logs_root"

if PYTHONPATH=/workspace/src python3 >"$logs_root/stdout.txt" 2>"$logs_root/stderr.txt" <<'PY'
import click
from click.testing import CliRunner


@click.command()
def cli():
    click.echo("out-1")
    click.echo("err-1", err=True)
    click.echo("out-2")
    click.echo("err-2", err=True)


result = CliRunner().invoke(cli)
assert result.exit_code == 0, result.exception
assert result.stdout == "out-1\nout-2\n"
assert result.stderr == "err-1\nerr-2\n"
assert result.output == "out-1\nerr-1\nout-2\nerr-2\n"
try:
    CliRunner(mix_stderr=True)
except TypeError:
    pass
else:
    raise AssertionError("mix_stderr remains accepted")
PY
then
  printf '{"reward":1}\n' > "$logs_root/reward.json"
  printf '{"stream_contract":true}\n' > "$logs_root/evidence.json"
else
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"stream_contract":false}\n' > "$logs_root/evidence.json"
fi
