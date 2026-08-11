"""Render durable run records into a historical view.

The view exists for one question: *did this move, and is the movement real?*
Two things follow from that and drive every choice here.

**Intervals, not points.** At five attempts a rate of 4/5 and a rate of 5/5 are
the same measurement. A view that draws two dots at 0.8 and 1.0 invites someone
to read a regression into ordinary noise, so every point is drawn with its
Wilson interval and the intervals are the visually dominant element.

**Comparable runs only.** A trend line is keyed on the question's identity —
challenge, fixture, model, conditions, and the digests of the challenge manifest
and verifier. Change the verifier and the score moves without memory having
changed at all, so those runs get their own line and an explicit note saying what
differed. The artifact digest is deliberately *not* part of the key; it is the
thing being tracked, and is labelled on each point instead.

Output is a single self-contained HTML file: no scripts, no fonts, no CDN. It
opens from disk, survives being emailed, and renders the same in five years.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .results import RunRecord, group_by_comparability

# Binary measures get an interval chart; everything else is shown as a figure in
# the table. Ordered — the first is the headline for each line.
CHART_METRICS = ("cited_decision", "reward")

_CHART_WIDTH = 720
_CHART_HEIGHT = 220
_PAD_LEFT = 46
_PAD_RIGHT = 16
_PAD_TOP = 16
_PAD_BOTTOM = 44


def headline_metric(records: Sequence[RunRecord], condition: str) -> str | None:
    """The first chart metric this line actually reports.

    Not every challenge produces every measure — a workspace-graded challenge
    scores `reward` and never sets `cited_decision`. Hardcoding one metric shows
    an em dash for half the corpus and hides the number that exists.
    """

    available = {
        aggregate.metric_name
        for record in records
        for aggregate in record.aggregates
        if aggregate.condition_id == condition
    }
    return next((metric for metric in CHART_METRICS if metric in available), None)


@dataclass(frozen=True)
class MetricPoint:
    run_id: str
    recorded_at: str
    artifact_sha256: str
    rate: float | None
    low: float | None
    high: float | None
    n: int


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _short(digest: str, length: int = 7) -> str:
    return digest[:length] if digest else "—"


def _metric_points(
    records: Sequence[RunRecord], metric: str, condition: str
) -> list[MetricPoint]:
    points: list[MetricPoint] = []
    for record in records:
        for aggregate in record.aggregates:
            if aggregate.metric_name != metric or aggregate.condition_id != condition:
                continue
            points.append(
                MetricPoint(
                    run_id=record.run_id,
                    recorded_at=record.recorded_at,
                    artifact_sha256=record.artifact_sha256,
                    rate=aggregate.value,
                    low=aggregate.interval_low,
                    high=aggregate.interval_high,
                    n=aggregate.n,
                )
            )
            break
    return points


def _x_positions(count: int) -> list[float]:
    usable = _CHART_WIDTH - _PAD_LEFT - _PAD_RIGHT
    if count <= 1:
        return [_PAD_LEFT + usable / 2]
    step = usable / (count - 1)
    return [_PAD_LEFT + index * step for index in range(count)]


def _y(value: float) -> float:
    usable = _CHART_HEIGHT - _PAD_TOP - _PAD_BOTTOM
    return _PAD_TOP + (1.0 - value) * usable


def _series_svg(points: Sequence[MetricPoint], xs: Sequence[float], css_class: str) -> str:
    parts: list[str] = []
    line: list[str] = []
    for point, x in zip(points, xs, strict=True):
        if point.rate is None:
            continue
        if point.low is not None and point.high is not None:
            parts.append(
                f'<line class="band {css_class}" x1="{x:.1f}" y1="{_y(point.low):.1f}" '
                f'x2="{x:.1f}" y2="{_y(point.high):.1f}" />'
            )
            for bound in (point.low, point.high):
                parts.append(
                    f'<line class="cap {css_class}" x1="{x - 4:.1f}" y1="{_y(bound):.1f}" '
                    f'x2="{x + 4:.1f}" y2="{_y(bound):.1f}" />'
                )
        line.append(f"{x:.1f},{_y(point.rate):.1f}")
        parts.append(
            f'<circle class="dot {css_class}" cx="{x:.1f}" cy="{_y(point.rate):.1f}" r="3.5">'
            f"<title>{_escape(point.run_id)} — {point.rate:.2f} of {point.n}"
            f" (build {_short(point.artifact_sha256)})</title></circle>"
        )
    if len(line) > 1:
        parts.insert(0, f'<polyline class="trend {css_class}" points="{" ".join(line)}" />')
    return "".join(parts)


def _chart(metric: str, records: Sequence[RunRecord], key) -> str:
    treatment = _metric_points(records, metric, key.treatment_condition)
    control = _metric_points(records, metric, key.control_condition)
    if not treatment and not control:
        return ""
    xs = _x_positions(max(len(treatment), len(control)))

    gridlines = "".join(
        f'<line class="grid" x1="{_PAD_LEFT}" y1="{_y(value):.1f}" '
        f'x2="{_CHART_WIDTH - _PAD_RIGHT}" y2="{_y(value):.1f}" />'
        f'<text class="axis" x="{_PAD_LEFT - 8}" y="{_y(value) + 4:.1f}" '
        f'text-anchor="end">{value:.1f}</text>'
        for value in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    labels = "".join(
        f'<text class="axis" x="{x:.1f}" y="{_CHART_HEIGHT - _PAD_BOTTOM + 18:.1f}" '
        f'text-anchor="middle">{_escape(point.recorded_at[:10])}</text>'
        f'<text class="axis muted" x="{x:.1f}" y="{_CHART_HEIGHT - _PAD_BOTTOM + 32:.1f}" '
        f'text-anchor="middle">{_escape(_short(point.artifact_sha256))}</text>'
        for point, x in zip(treatment or control, xs, strict=False)
    )

    single = (
        '<p class="note">A single run is a measurement, not a trend — '
        "the interval is the whole story until there is a second point.</p>"
        if len(records) < 2
        else ""
    )

    return (
        f'<figure class="chart"><figcaption>{_escape(metric)}</figcaption>'
        f'<svg viewBox="0 0 {_CHART_WIDTH} {_CHART_HEIGHT}" role="img" '
        f'aria-label="{_escape(metric)} across runs">'
        f"{gridlines}{labels}"
        f"{_series_svg(control, xs, 'control')}"
        f"{_series_svg(treatment, xs, 'treatment')}"
        "</svg>"
        f'<p class="legend"><span class="swatch treatment"></span>'
        f"{_escape(key.treatment_condition)}"
        f'<span class="swatch control"></span>{_escape(key.control_condition)}'
        "<span class=\"muted\">— bars are 95% Wilson intervals</span></p>"
        f"{single}</figure>"
    )


def _format_number(value: float | None, places: int = 2) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.{places}f}"


def _uptake_cell(record: RunRecord) -> str:
    treatment = record.memory_uptake.get("treatment")
    if not treatment:
        return "—"
    attempts = treatment.get("attempts", 0)
    return (
        f"{treatment.get('injected', 0)}/{attempts} injected · "
        f"{treatment.get('marker', 0)}/{attempts} scored"
    )


def _run_table(records: Sequence[RunRecord], key) -> str:
    metric = headline_metric(records, key.treatment_condition) or CHART_METRICS[0]
    rows: list[str] = []
    for record in records:
        admission = record.publication.decision if record.publication else "unreviewed"
        headline = next(
            (
                aggregate
                for aggregate in record.aggregates
                if aggregate.metric_name == metric
                and aggregate.condition_id == key.treatment_condition
            ),
            None,
        )
        verdict = record.verdicts.get(metric) or {}
        p_value = verdict.get("fisher_one_sided_p")
        interval = ""
        if headline is not None and headline.interval_low is not None:
            interval = (
                f' <span class="muted">[{headline.interval_low:.2f}, '
                f"{headline.interval_high:.2f}]</span>"
            )
        rows.append(
            "<tr>"
            f"<td>{_escape(record.recorded_at[:10])}</td>"
            f"<td><code>{_escape(record.run_id)}</code></td>"
            f"<td><code>{_escape(_short(record.artifact_sha256))}</code></td>"
            f"<td>{record.attempts_analysed}/{record.attempts_run}</td>"
            f"<td>{_format_number(headline.value) if headline else '—'}{interval}</td>"
            f"<td>{_format_number(verdict.get('difference'))}</td>"
            f"<td>{'—' if p_value is None else f'{p_value:.3f}'}</td>"
            f"<td>{_uptake_cell(record)}</td>"
            f"<td>{_escape(record.status)}</td>"
            f"<td>{_escape(admission)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>date</th><th>run</th><th>build</th><th>analysed</th>"
        f"<th>{_escape(metric)} (treatment)</th><th>difference</th>"
        "<th>one-sided p</th><th>memory uptake</th><th>status</th><th>admission</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _excluded_runs(records: Sequence[RunRecord]) -> str:
    rows = [
        "<tr>"
        f"<td><code>{_escape(record.run_id)}</code></td>"
        f"<td><code>{_escape(record.publication.reason_code)}</code></td>"
        f"<td>{_escape(record.publication.reason)}</td>"
        "</tr>"
        for record in records
        if record.publication is not None and record.publication.decision == "excluded"
    ]
    if not rows:
        return ""
    return (
        "<h2>Excluded runs</h2>"
        "<p class=\"muted\">Retained for transparency. These outcomes do not "
        "contribute to any reported effect estimate.</p>"
        '<div class="table-scroll"><table><thead><tr>'
        "<th>run</th><th>reason code</th><th>reason</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def _admitted(records: Sequence[RunRecord]) -> list[RunRecord]:
    return [
        record
        for record in records
        if record.publication is None or record.publication.decision == "accepted"
    ]


_NON_INFERIORITY_METRIC = "solve_rate_non_inferiority"
_VERDICT_LABELS = {1.0: "non-inferior", 0.0: "inconclusive", -1.0: "inferior"}


def _non_inferiority(records: Sequence[RunRecord]) -> str:
    """State a no-harm claim in full, or say the run could not make one.

    A negative result is the easiest kind to overstate, and the two ways to
    overstate it are dropping the margin (so a bound looks like a verdict) and
    printing "inconclusive" as though it were "safe". Both the margin and the
    interval are shown next to the word, and `inconclusive` is rendered in its
    own style rather than sharing the pass styling.
    """

    rows: list[str] = []
    for record in records:
        verdict = record.verdicts.get(_NON_INFERIORITY_METRIC)
        if not verdict:
            continue
        label = _VERDICT_LABELS.get(verdict.get("verdict"), "not evaluable")
        low, high = verdict.get("interval_low"), verdict.get("interval_high")
        interval = (
            f"[{low:+.2f}, {high:+.2f}]" if low is not None and high is not None else "—"
        )
        margin = verdict.get("margin")
        rows.append(
            "<tr>"
            f"<td><code>{_escape(record.run_id)}</code></td>"
            f'<td class="verdict-{_escape(label.replace(" ", "-"))}">{_escape(label)}</td>'
            f"<td>{_format_number(verdict.get('difference'))}</td>"
            f"<td>{_escape(interval)}</td>"
            f"<td>{'—' if margin is None else f'−{margin:.2f}'}</td>"
            "</tr>"
        )
    if not rows:
        return ""
    return (
        '<div class="table-scroll"><table><thead><tr>'
        "<th>run</th><th>verdict</th><th>solve-rate difference</th>"
        "<th>95% interval</th><th>margin</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        '<p class="muted">A verdict of <em>inconclusive</em> means the sample could '
        "not rule out a drop larger than the margin — not that there was none.</p>"
    )


def _divergences(lines: Sequence[tuple[str, list[RunRecord]]]) -> str:
    """Explain why runs of the same challenge landed on separate lines.

    Without this the split is silently confusing: you re-run a question, the
    result appears as a brand-new line with one point, and nothing says the
    verifier digest moved. Naming the differing field is the whole value.
    """

    by_challenge: dict[str, list[tuple[str, list[RunRecord]]]] = {}
    for digest, records in lines:
        by_challenge.setdefault(records[0].comparability.challenge_id, []).append(
            (digest, records)
        )

    blocks: list[str] = []
    for challenge, group in sorted(by_challenge.items()):
        if len(group) < 2:
            continue
        fields = (
            "fixture_id",
            "model",
            "control_condition",
            "treatment_condition",
            "challenge_manifest_sha256",
            "verifier_sha256",
            "source_commit",
        )
        differing = [
            field
            for field in fields
            if len({getattr(records[0].comparability, field) for _, records in group}) > 1
        ]
        items = "".join(
            f"<li><code>{_escape(_short(digest, 12))}</code> — "
            f"{len(records)} run(s), "
            + ", ".join(
                f"{_escape(field)}=<code>"
                f"{_escape(_short(str(getattr(records[0].comparability, field)), 12))}</code>"
                for field in differing
            )
            + "</li>"
            for digest, records in group
        )
        blocks.append(
            f"<section class=\"divergence\"><h3>{_escape(challenge)} — "
            f"{len(group)} incomparable groups</h3>"
            "<p>These runs asked the same challenge but differ in inputs that move "
            "the score on their own, so they are not plotted together.</p>"
            f"<ul>{items}</ul></section>"
        )
    return "".join(blocks)


_STYLE = """
:root { color-scheme: light dark; --fg:#16181d; --muted:#5c6470; --bg:#ffffff;
  --line:#d8dce3; --panel:#f6f7f9; --treatment:#2f6df6; --control:#8a8f98; }
@media (prefers-color-scheme: dark) { :root { --fg:#e6e8ec; --muted:#98a0ac;
  --bg:#0f1115; --line:#2a2f38; --panel:#171a20; --treatment:#6f9dff;
  --control:#7d8590; } }
:root[data-theme="dark"] { --fg:#e6e8ec; --muted:#98a0ac; --bg:#0f1115;
  --line:#2a2f38; --panel:#171a20; --treatment:#6f9dff; --control:#7d8590; }
:root[data-theme="light"] { --fg:#16181d; --muted:#5c6470; --bg:#ffffff;
  --line:#d8dce3; --panel:#f6f7f9; --treatment:#2f6df6; --control:#8a8f98; }
body { margin:0; padding:2.5rem 1.25rem 5rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:60rem; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 .25rem; letter-spacing:-.01em; }
h2 { font-size:1.05rem; margin:0 0 .2rem; letter-spacing:-.005em; }
h3 { font-size:.95rem; margin:0 0 .4rem; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em; }
.muted { color:var(--muted); }
.lede { color:var(--muted); margin:0 0 2rem; }
.line { border:1px solid var(--line); border-radius:10px; padding:1.1rem 1.25rem;
  margin:0 0 1.5rem; background:var(--panel); }
.meta { color:var(--muted); font-size:.83rem; margin:0 0 1rem; }
.meta span { margin-right:1rem; white-space:nowrap; }
figure.chart { margin:0 0 1rem; }
figcaption { font-size:.8rem; color:var(--muted); text-transform:uppercase;
  letter-spacing:.06em; margin-bottom:.3rem; }
svg { width:100%; height:auto; display:block; }
.grid { stroke:var(--line); stroke-width:1; }
.axis { fill:var(--muted); font-size:10px; font-family:ui-monospace,monospace; }
.trend { fill:none; stroke-width:1.5; opacity:.85; }
.band { stroke-width:6; opacity:.22; stroke-linecap:round; }
.cap { stroke-width:1.25; opacity:.55; }
.treatment { stroke:var(--treatment); } circle.treatment { fill:var(--treatment); stroke:none; }
.control { stroke:var(--control); } circle.control { fill:var(--control); stroke:none; }
.legend { font-size:.8rem; color:var(--fg); margin:.4rem 0 0; }
.swatch { display:inline-block; width:10px; height:10px; border-radius:2px;
  margin:0 .35rem 0 0; vertical-align:middle; }
.swatch.treatment { background:var(--treatment); } .swatch.control { background:var(--control); }
.legend span.muted { margin-left:.75rem; }
.note { font-size:.83rem; color:var(--muted); margin:.5rem 0 0; }
.table-scroll { overflow-x:auto; }
/* Inconclusive is not a pass. It gets the neutral colour, never the green one. */
.verdict-non-inferior { color:#1a7f45; font-weight:600; }
.verdict-inconclusive { color:var(--muted); font-weight:600; }
.verdict-inferior { color:#b3261e; font-weight:600; }
table { border-collapse:collapse; width:100%; font-size:.85rem; }
th, td { text-align:left; padding:.4rem .6rem; border-bottom:1px solid var(--line);
  white-space:nowrap; }
th { color:var(--muted); font-weight:600; font-size:.78rem; text-transform:uppercase;
  letter-spacing:.04em; }
.divergence { border-left:3px solid var(--line); padding:0 0 0 1rem; margin:0 0 1.25rem; }
.divergence ul { margin:.4rem 0 0; padding-left:1.1rem; }
.divergence li { font-size:.85rem; margin-bottom:.2rem; }
.empty { color:var(--muted); }
"""


def render_report(records: Iterable[RunRecord], *, generated_at: str) -> str:
    """Render every record into one self-contained HTML page."""

    ordered = list(records)
    admitted = _admitted(ordered)
    grouped = group_by_comparability(admitted)
    lines = sorted(
        grouped.items(),
        key=lambda item: (
            item[1][0].comparability.challenge_id,
            item[1][0].recorded_at,
        ),
    )

    if not lines:
        body = '<p class="empty">No result records yet.</p>'
    else:
        sections: list[str] = []
        for digest, group in lines:
            key = group[0].comparability
            meta = (
                f"<p class=\"meta\">"
                f"<span>fixture <code>{_escape(key.fixture_id)}</code></span>"
                f"<span>corpus <code>{_escape(_short(key.source_commit, 8))}</code></span>"
                f"<span>challenge <code>"
                f"{_escape(_short(key.challenge_manifest_sha256))}</code></span>"
                f"<span>verifier <code>{_escape(_short(key.verifier_sha256))}</code></span>"
                f"<span>line <code>{_escape(_short(digest, 12))}</code></span>"
                "</p>"
            )
            charts = "".join(
                _chart(metric, group, key)
                for metric in CHART_METRICS
                if any(
                    aggregate.metric_name == metric
                    for record in group
                    for aggregate in record.aggregates
                )
            )
            no_harm = _non_inferiority(group)
            sections.append(
                f'<section class="line"><h2>{_escape(key.label())}</h2>{meta}'
                f'{charts}<div class="table-scroll">{_run_table(group, key)}</div>'
                f"{no_harm}</section>"
            )
        divergences = _divergences(lines)
        body = "".join(sections) + (
            f"<h2>Not comparable</h2>{divergences}" if divergences else ""
        )
    body += _excluded_runs(ordered)

    run_count = len(ordered)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Benchmark history</title>"
        f"<style>{_STYLE}</style></head><body><main>"
        "<h1>Benchmark history</h1>"
        f'<p class="lede">{run_count} retained run(s); {len(admitted)} admitted across '
        f'{len(lines)} comparable line(s). '
        f"Generated {_escape(generated_at)}. Bars are 95% Wilson intervals — at these "
        "sample sizes the interval, not the point, is the measurement.</p>"
        f"{body}</main></body></html>"
    )


def render_summary_text(records: Sequence[RunRecord]) -> str:
    """A terminal-readable digest of the same grouping, for CI output."""

    admitted = _admitted(records)
    grouped = group_by_comparability(admitted)
    excluded = len(records) - len(admitted)
    lines: list[str] = [
        f"{len(records)} retained · {len(admitted)} admitted · {excluded} excluded · "
        f"{len(grouped)} comparable line(s)"
    ]
    for digest, group in sorted(
        grouped.items(), key=lambda item: item[1][0].comparability.challenge_id
    ):
        key = group[0].comparability
        metric = headline_metric(group, key.treatment_condition) or CHART_METRICS[0]
        lines.append(f"\n{key.label()}  [{_short(digest, 12)}]")
        for record in group:
            headline = next(
                (
                    aggregate
                    for aggregate in record.aggregates
                    if aggregate.metric_name == metric
                    and aggregate.condition_id == key.treatment_condition
                ),
                None,
            )
            interval = (
                f" [{headline.interval_low:.2f}, {headline.interval_high:.2f}]"
                if headline is not None and headline.interval_low is not None
                else ""
            )
            value = _format_number(headline.value) if headline else "—"
            lines.append(
                f"  {record.recorded_at[:10]}  {record.run_id:<20s} "
                f"build {_short(record.artifact_sha256)}  "
                f"{metric}={value}{interval}"
            )
    return "\n".join(lines)


def comparability_meta(records: Sequence[RunRecord]) -> Mapping[str, int]:
    admitted = _admitted(records)
    grouped = group_by_comparability(admitted)
    return {
        "records": len(records),
        "admitted": len(admitted),
        "excluded": len(records) - len(admitted),
        "lines": len(grouped),
    }
