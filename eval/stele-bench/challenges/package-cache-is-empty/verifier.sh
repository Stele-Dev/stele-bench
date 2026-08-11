#!/bin/sh
set -u

mkdir -p /logs/verifier
test_file=/workspace/tests/package_cache_is_empty_contract.rs
contract=0
library=0

cleanup() {
  rm -f "$test_file"
}
trap cleanup EXIT HUP INT TERM

if [ -e "$test_file" ] || [ -L "$test_file" ]; then
  printf '{"reward":0}\n' > /logs/verifier/reward.json
  printf '{"contract_test":0,"library_tests":0,"reason":"temporary test path already exists"}\n' \
    > /logs/verifier/evidence.json
  exit 0
fi

mkdir -p /workspace/tests
cat > "$test_file" <<'RS'
use chrono::Utc;
use dotstate::utils::package_cache::{PackageCacheData, PackageCacheEntry};

#[test]
fn empty_means_no_package_status_entries_regardless_of_schema_version() {
    let default_data = PackageCacheData::default();
    assert!(default_data.is_empty());

    let version_only = PackageCacheData {
        version: u32::MAX,
        entries: Default::default(),
    };
    assert!(version_only.is_empty());

    let mut populated = PackageCacheData {
        version: 0,
        entries: Default::default(),
    };
    populated.entries.insert(
        "profile::package".to_owned(),
        PackageCacheEntry {
            installed: false,
            last_checked: Utc::now(),
            check_command: None,
            output: None,
        },
    );
    assert!(!populated.is_empty());

    populated.entries.clear();
    assert!(populated.is_empty());
}
RS

cd /workspace
if cargo test --test package_cache_is_empty_contract; then
  contract=1
fi
if cargo test --lib; then
  library=1
fi

reward=0
if [ "$contract" -eq 1 ] && [ "$library" -eq 1 ]; then
  reward=1
fi
printf '{"reward":%s}\n' "$reward" > /logs/verifier/reward.json
printf '{"contract_test":%s,"library_tests":%s}\n' "$contract" "$library" \
  > /logs/verifier/evidence.json
