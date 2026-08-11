#!/bin/sh
set -eu

mkdir -p /logs/verifier
reward=0
if [ -f /workspace/.ready ] && [ ! -L /workspace/.ready ] && \
   [ "$(cat /workspace/.ready)" = "ready" ] && \
   [ "$(wc -c < /workspace/.ready)" -eq 6 ]; then
  reward=1
fi
printf '{"reward":%s}\n' "$reward" > /logs/verifier/reward.json
