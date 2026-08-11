from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from stele_bench.challenges import DEFAULT_CHALLENGE_ROOT


def _score_keypress_answer(tmp_path: Path, answer: str) -> tuple[float, dict[str, int]]:
    answer_path = tmp_path / "answer.md"
    logs = tmp_path / "logs"
    answer_path.write_text(answer, encoding="utf-8")

    source = (
        DEFAULT_CHALLENGE_ROOT / "input-keypress-architecture" / "verifier.sh"
    ).read_text(encoding="utf-8")
    runnable = source.replace(
        "/workspace/.submission/answer.md", answer_path.as_posix()
    ).replace("/logs/verifier", logs.as_posix())
    verifier = tmp_path / "verifier.sh"
    verifier.write_text(runnable, encoding="utf-8")
    subprocess.run(["sh", verifier], check=True)

    reward = json.loads((logs / "reward.json").read_text(encoding="utf-8"))
    evidence = json.loads((logs / "evidence.json").read_text(encoding="utf-8"))
    return float(reward["reward"]), evidence


@pytest.mark.parametrize(
    "answer",
    (
        """
The app-wide input mode gate in src/app.rs uses input_mode_active and
sync_input_mode to suppress navigation actions in get_action.

Each screen manually implements is_input_focused, so a forgotten popup breaks
the guard. Plain character insertion must happen
before keymap lookup. Otherwise Vim h/j/k/l/q keys are interpreted as move or
quit actions instead of text.

This duplicated per-screen character insertion and event handling is the
architectural reason the bug recurs.
""",
        """
src/app.rs contains the global focus guard in sync_input_mode and get_action,
which filters keymap shortcuts while typing. Popups report focus with bespoke,
per-screen is_input_focused implementations, so omissions create gaps.

The screen-level character insertion pattern is duplicated across several Rust
files. KeyCode::Char is inserted
ahead of action lookup, because Vim q, h, j, k, and l would otherwise trigger
quit or navigation. Reimplementing that routing lets the bug be reintroduced.
""",
        """
The app-level keymap filter in src/app.rs uses sync_input_mode and get_action.
Every screen manually implements is_input_focused, so omissions break the
guard. Each per-screen event handler must insert plain Char keys before keymap
lookup because Vim navigation actions otherwise eat the characters.

This character insertion is duplicated across the handlers. With no compiler
enforcement, the same architectural bug recurs whenever a screen misses a step.
""",
    ),
)
def test_keypress_verifier_accepts_grounded_multiline_paraphrases(
    tmp_path: Path, answer: str
) -> None:
    reward, evidence = _score_keypress_answer(tmp_path, answer)

    assert reward == 1
    assert set(evidence.values()) == {1}


def test_keypress_verifier_rejects_unsupported_answer(tmp_path: Path) -> None:
    reward, evidence = _score_keypress_answer(
        tmp_path, "The issue probably comes from keyboard handling."
    )

    assert reward == 0
    assert set(evidence.values()) == {0}
