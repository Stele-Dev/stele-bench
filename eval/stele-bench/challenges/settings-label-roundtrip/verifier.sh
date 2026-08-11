#!/bin/sh
set -u

workspace_root=${MUTATION_WORKSPACE_ROOT:-/workspace}
logs_root=${MUTATION_LOGS_ROOT:-/logs/verifier}
mkdir -p "$logs_root"
test_file=$workspace_root/tests/settings_label_roundtrip_contract.rs
contract=0
library=0

cleanup() {
  rm -f "$test_file"
}
trap cleanup EXIT HUP INT TERM

if [ -e "$test_file" ] || [ -L "$test_file" ]; then
  printf '{"reward":0}\n' > "$logs_root/reward.json"
  printf '{"contract_test":0,"library_tests":0,"reason":"temporary test path already exists"}\n' \
    > "$logs_root/evidence.json"
  exit 0
fi

mkdir -p "$workspace_root/tests"
cat > "$test_file" <<'RS'
use dotstate::config::Config;
use dotstate::screens::settings::{SettingItem, SettingsScreen};

#[test]
fn renamed_update_setting_label_still_round_trips() {
    let setting_name = SettingItem::CheckForUpdates.name();
    assert_eq!(setting_name, "Automatic Update Checks");

    let screen = SettingsScreen::new();
    let mut config = Config::default();

    assert!(config.updates.check_enabled);
    assert!(screen.apply_setting_to_config(&mut config, setting_name, 1));
    assert!(!config.updates.check_enabled);
    assert!(screen.apply_setting_to_config(&mut config, setting_name, 0));
    assert!(config.updates.check_enabled);
}
RS

cd "$workspace_root"
if cargo test --test settings_label_roundtrip_contract; then
  contract=1
fi
if cargo test --lib; then
  library=1
fi

reward=0
if [ "$contract" -eq 1 ] && [ "$library" -eq 1 ]; then
  reward=1
fi
printf '{"reward":%s}\n' "$reward" > "$logs_root/reward.json"
printf '{"settings_label_roundtrip":%s,"library_tests":%s}\n' "$contract" "$library" \
  > "$logs_root/evidence.json"
