#!/bin/sh
set -u

mkdir -p /logs/verifier

candidates_file=/workspace/src/dotfile_candidates.rs
changelog_file=/workspace/CHANGELOG.md

has_entry=0
if [ -f "$candidates_file" ] && grep -Eq 'path: *"\.windsurf/rules"' "$candidates_file"; then
  has_entry=1
fi

has_changelog_bullet=0
if [ -f "$changelog_file" ]; then
  # Isolate the "## [Unreleased]" section: from that heading up to the next
  # "## [" version heading (or end of file).
  unreleased=$(awk '/^## \[Unreleased\]/{flag=1; next} /^## \[/{flag=0} flag' "$changelog_file")
  if printf '%s\n' "$unreleased" | grep -Eiq '^- \*\*[A-Za-z0-9 /_-]+\*\*: .*windsurf'; then
    has_changelog_bullet=1
  fi
fi

reward=0
if [ "$has_entry" -eq 1 ] && [ "$has_changelog_bullet" -eq 1 ]; then
  reward=1
fi

printf '{"reward":%s}\n' "$reward" > /logs/verifier/reward.json
printf '{"dotfile_candidate_entry":%s,"changelog_unreleased_bullet":%s}\n' \
  "$has_entry" "$has_changelog_bullet" > /logs/verifier/evidence.json
