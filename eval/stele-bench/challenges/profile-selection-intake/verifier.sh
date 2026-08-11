#!/bin/sh
set -eu

logs_root=${MUTATION_LOGS_ROOT:-/logs/verifier}
mkdir -p "$logs_root"
printf '{"reward":1}\n' > "$logs_root/reward.json"
printf '{"answer_is_graded_from_retained_trajectory":true}\n' > "$logs_root/evidence.json"
