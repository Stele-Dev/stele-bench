from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from stele_bench.results import RunRecord

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "results" / "2026-08-preliminary"


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(
        self,
        _tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.hrefs.extend(value for name, value in attrs if name == "href" and value)


def test_preliminary_admissions_are_complete_and_match_durable_records() -> None:
    payload = json.loads((RELEASE / "admissions.json").read_text(encoding="utf-8"))
    runs = payload["runs"]
    assert len(runs) == 10
    assert len({run["run_id"] for run in runs}) == len(runs)
    assert {run["run_id"] for run in runs if run["decision"] == "accepted"} == {
        "input-keypress-architecture-v3",
        "package-cache-is-empty-v2",
        "settings-label-roundtrip-v4",
    }

    for admission in runs:
        preregistration = (RELEASE / admission["preregistration"]).resolve()
        assert preregistration.is_file()
        if admission["result"] is None:
            continue
        result_path = (RELEASE / admission["result"]).resolve()
        record = RunRecord.model_validate_json(result_path.read_text(encoding="utf-8"))
        assert record.run_id == admission["run_id"]
        assert record.publication is not None
        assert record.publication.decision == admission["decision"]
        assert record.publication.reason_code == admission["reason_code"]
        assert record.publication.reason == admission["reason"]


def test_preliminary_evidence_manifest_covers_every_exported_file() -> None:
    manifest = json.loads((RELEASE / "manifest.json").read_text(encoding="utf-8"))
    listed = {entry["path"]: entry for entry in manifest["files"]}
    actual = {
        path.relative_to(RELEASE).as_posix(): path
        for path in RELEASE.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert listed.keys() == actual.keys()
    for relative, path in actual.items():
        data = path.read_bytes()
        assert listed[relative]["bytes"] == len(data)
        assert listed[relative]["sha256"] == hashlib.sha256(data).hexdigest()


def test_preliminary_evidence_contains_no_host_paths_or_secret_shapes() -> None:
    forbidden = (
        re.compile(r"/Users/serkan"),
        re.compile(r"bridge_token=(?!<redacted>)"),
        re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
        re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
        re.compile(r"sb_secret_[A-Za-z0-9_-]{12,}"),
    )
    for path in RELEASE.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert not any(pattern.search(text) for pattern in forbidden), path


def test_interpreted_report_covers_admitted_results_and_resolves_local_links() -> None:
    report = (ROOT / "index.html").read_text(encoding="utf-8")
    for required in (
        "−40.7%",
        "−71.3%",
        "−54.0%",
        "+24.04s",
        "+83,520",
        "+33.08s",
        "COMPOUNDING NOT MEASURED",
        "within these admitted runs",
        "tui-forge-integration",
    ):
        assert required in report

    parser = _LinkParser()
    parser.feed(report)
    for href in parser.hrefs:
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        assert (ROOT / parsed.path).is_file(), href
