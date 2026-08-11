"""The auto-RAG injection channel: the strip, and the proof it took.

These tests run the *generated shell* against a real directory tree rather than
asserting on strings. A mock adapter can be made to agree with any command; the
question that decides whether a `faithful-norag` arm is real is what `sh` does to
files on disk, so that is what is exercised here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from stele_bench.preflight import (
    StructuralPreflightError,
    evaluate_injection_channel,
    injection_channel_command,
    parse_injection_channel,
    strip_injection_channel_command,
)

# The shape that matters: two hooks on the same event, one of which is auto-RAG.
# A tree with auto-RAG alone would let a "delete the whole block" strip pass.
PLUGIN_JSON = {
    "name": "stele",
    "hooks": {
        "SessionStart": [
            {"hooks": [{"type": "command", "command": "stele hook session-start --harness=claude"}]}
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {"type": "command", "command": "stele hook auto-rag --harness=claude"},
                    {"type": "command", "command": "stele hook discovery --harness=claude"},
                ]
            }
        ],
    },
}

# A single logical install leaves the hook declarations in more than one place:
# the synthesized marketplace and the harness's own plugin cache (KNOW-2415).
COPIES = (
    ".stele/claude-marketplace/stele/.claude-plugin/plugin.json",
    ".claude/plugins/cache/stele-local/stele/1.2.3/.claude-plugin/plugin.json",
)


def _home(root: Path, copies: tuple[str, ...] = COPIES) -> Path:
    home = root / "home"
    for relative in copies:
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(PLUGIN_JSON, indent=2) + "\n", encoding="utf-8")
    return home


def _sh(script: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh"],
        input=script,
        text=True,
        capture_output=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        timeout=60,
    )


def _counts(home: Path) -> dict[str, int]:
    result = _sh(injection_channel_command(), home)
    assert result.returncode == 0, result.stderr
    return parse_injection_channel(result.stdout)


def _hook_commands(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [hook["command"] for hook in data["hooks"]["UserPromptSubmit"][0]["hooks"]]


def test_strip_rewrites_every_copy_and_leaves_the_plugin_intact(tmp_path: Path) -> None:
    home = _home(tmp_path)
    # The verifier command itself is retained in Harbor's logs. It must not be
    # mistaken for live hook wiring merely because it contains the needle.
    log = home / "trial.log"
    log.write_text("grep for stele hook auto-rag\n", encoding="utf-8")
    assert _counts(home) == {"roots": 1, "auto_rag": 2, "sibling": 2}

    result = _sh(strip_injection_channel_command(), home)
    assert result.returncode == 0, result.stderr

    assert _counts(home) == {"roots": 1, "auto_rag": 0, "sibling": 2}
    for relative in COPIES:
        path = home / relative
        # Still parses: the rewrite substitutes one bare word for another inside
        # a JSON string, so it never needs a JSON parser in the container.
        commands = _hook_commands(path)
        assert commands[0].startswith("true")
        assert commands[1] == "stele hook discovery --harness=claude"
    assert list(home.rglob("*.stele-bench-norag")) == []
    assert "stele hook auto-rag" in log.read_text(encoding="utf-8")


def test_strip_fails_closed_when_there_is_nothing_to_strip(tmp_path: Path) -> None:
    home = _home(tmp_path)
    assert _sh(strip_injection_channel_command(), home).returncode == 0

    # Running it again means the assumption "this arm had injection to remove"
    # was already false. Silently succeeding here would let a broken install
    # masquerade as a valid control arm.
    repeated = _sh(strip_injection_channel_command(), home)
    assert repeated.returncode != 0
    assert "already absent" in repeated.stderr


def test_strip_fails_closed_with_no_install_at_all(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    result = _sh(strip_injection_channel_command(), empty)
    assert result.returncode != 0
    assert "no Stele install root" in result.stderr


def test_strip_refuses_to_follow_a_symlinked_plugin_file(tmp_path: Path) -> None:
    # The symlink has to sit at a path the strip actually searches. That search
    # is `find -L $roots -type f -path '*/.claude-plugin/plugin.json'`, so the
    # link stands in for the SECOND canonical copy (COPIES[1]) while COPIES[0]
    # is a real file — a plausible install where the harness cache is symlinked
    # at the marketplace rather than duplicated.
    #
    # An earlier version of this test put the link at `.claude/plugins/plugin.json`,
    # which no install produces. That worked only while discovery was a blanket
    # `grep -RlIF` over the whole install root; once discovery narrowed to the
    # manifest path, `find` stopped seeing the link, the guard below never ran,
    # the real copy stripped cleanly, and the command exited 0. The test failed
    # on an assertion about the symlink guard while the guard itself was fine.
    #
    # There is no platform skip here any more, either. The old one probed
    # whether grep followed symlinks under -R (GNU does, BSD doesn't), which
    # stopped being the mechanism when discovery moved to `find -L` — and `-L`
    # follows links on both. Skipping on macOS would now just hide the test.
    home = _home(tmp_path, copies=COPIES[:1])
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(PLUGIN_JSON), encoding="utf-8")
    link = home / COPIES[1]
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    result = _sh(strip_injection_channel_command(), home)
    assert result.returncode != 0
    assert "symlink" in result.stderr
    # The file the link pointed at is untouched.
    assert "stele hook auto-rag --harness=claude" in outside.read_text(encoding="utf-8")


def test_counts_report_an_absent_install_rather_than_an_absent_search(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    # The home directory always exists, so `roots` says the probe had somewhere
    # to look; it is the SIBLING count that distinguishes "stripped" from
    # "nothing was ever installed".
    assert _counts(empty) == {"roots": 1, "auto_rag": 0, "sibling": 0}


@pytest.mark.parametrize(
    "expected, counts",
    [
        ("on", {"roots": 2, "auto_rag": 2, "sibling": 2}),
        ("off", {"roots": 2, "auto_rag": 0, "sibling": 2}),
    ],
)
def test_evaluate_accepts_an_arm_that_matches_its_claim(
    expected: str, counts: dict[str, int]
) -> None:
    report = evaluate_injection_channel(counts, expected)  # type: ignore[arg-type]

    assert report["expected"] == expected
    assert report["verified"] is True


def test_evaluate_rejects_a_treatment_arm_that_lost_its_injection() -> None:
    # The direction that would otherwise pass unnoticed: this arm scores as
    # evidence that auto-RAG doesn't help, when in fact it never ran.
    with pytest.raises(StructuralPreflightError, match="requires it"):
        evaluate_injection_channel({"roots": 2, "auto_rag": 0, "sibling": 2}, "on")


def test_evaluate_rejects_a_control_arm_that_kept_its_injection() -> None:
    with pytest.raises(StructuralPreflightError, match="survived the strip"):
        evaluate_injection_channel({"roots": 2, "auto_rag": 1, "sibling": 2}, "off")


def test_evaluate_rejects_a_vanished_install() -> None:
    with pytest.raises(StructuralPreflightError, match="does not wire any lifecycle hooks"):
        evaluate_injection_channel({"roots": 2, "auto_rag": 0, "sibling": 0}, "off")


def test_evaluate_rejects_a_missing_install_root() -> None:
    with pytest.raises(StructuralPreflightError, match="no Stele install root"):
        evaluate_injection_channel({"roots": 0, "auto_rag": 0, "sibling": 0}, "off")


@pytest.mark.parametrize("raw", ["", "roots=2 auto_rag=0", "roots=2 auto_rag=0 sibling=x"])
def test_parse_rejects_output_it_does_not_recognise(raw: str) -> None:
    with pytest.raises(StructuralPreflightError):
        parse_injection_channel(raw)
