#!/bin/sh
set -u

workspace_root=${MUTATION_WORKSPACE_ROOT:-/workspace}
logs_root=${MUTATION_LOGS_ROOT:-/logs/verifier}
mkdir -p "$logs_root"
test_file=$workspace_root/tests/profile_selection_live_path_contract.rs
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
use crossterm::event::{KeyCode, KeyModifiers};
use dotstate::components::ProfileSelectionPopup;
use dotstate::Config;

#[test]
fn ctrl_u_clears_the_live_onboarding_profile_name_input() {
    let repo = tempfile::tempdir().expect("temporary repo");
    let config = Config::default();
    let mut popup = ProfileSelectionPopup::new();
    popup.show(repo.path()).expect("show profile picker");

    for character in "draft-profile".chars() {
        assert!(popup
            .handle_key(KeyCode::Char(character), KeyModifiers::NONE, &config)
            .is_none());
    }

    assert!(popup
        .handle_key(KeyCode::Char('u'), KeyModifiers::CONTROL, &config)
        .is_none());

    assert!(popup
        .handle_key(KeyCode::Enter, KeyModifiers::NONE, &config)
        .is_none());
}
RS

cd "$workspace_root"
if cargo test --test profile_selection_live_path_contract; then
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
printf '{"live_profile_picker_contract":%s,"library_tests":%s}\n' "$contract" "$library" \
  > "$logs_root/evidence.json"
