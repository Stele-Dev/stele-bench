#!/bin/sh
set -eu

logs_root=${MUTATION_LOGS_ROOT:-/logs/verifier}
mkdir -p "$logs_root"

if PYTHONPATH=/workspace/src python3 >"$logs_root/stdout.txt" 2>"$logs_root/stderr.txt" <<'PY'
import io
import os
import sys

import click
from click.testing import CliRunner


@click.command()
def safe_default():
    unavailable = False
    try:
        sys.stdout.fileno()
    except io.UnsupportedOperation:
        unavailable = True
    assert unavailable
    click.echo("safe-default")


default_result = CliRunner().invoke(safe_default)
assert default_result.exit_code == 0, default_result.exception
assert default_result.stdout == "safe-default\n"


@click.command()
def fd_output():
    click.echo("python-out")
    os.write(1, b"fd-out\n")
    os.write(2, b"fd-err\n")


fd_result = CliRunner(capture="fd").invoke(fd_output)
assert fd_result.exit_code == 0, fd_result.exception
assert "python-out" in fd_result.stdout
assert "fd-out" in fd_result.stdout
assert "fd-err" in fd_result.stderr
PY
then
  printf '{"reward":1}\n' > "$logs_root/reward.json"
  printf '{"safe_default":true,"fd_capture":true}\n' > "$logs_root/evidence.json"
else
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"safe_default":false,"fd_capture":false}\n' > "$logs_root/evidence.json"
fi
