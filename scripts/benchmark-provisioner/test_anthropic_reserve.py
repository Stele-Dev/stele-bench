from __future__ import annotations

import hashlib
import json
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic_reserve
import pytest
from anthropic_reserve import (
    UnpricedUsageError,
    check_anthropic_drain,
    check_anthropic_reserve,
    load_reserve_config,
)
from gateway_reserve import GatewayAuthError, GatewayDrainError, GatewayReserveError

KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz-WXYZ"
ADMIN = "sk-ant-admin01-admin-key-value"
KEY_SHA256 = hashlib.sha256(KEY.encode("utf-8")).hexdigest()
KEY_ID = "apikey_01Bench"
WORKSPACE = "wrkspc_01Bench"
ORG = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 10, 10, 12, 30, tzinfo=UTC)
# Captured at import, before the conftest guard replaces it for each test.
_REAL_GET_JSON = anthropic_reserve._get_json


def _config(tmp_path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "key_sha256": KEY_SHA256,
        "key_id": KEY_ID,
        "key_name": "stele-bench-eval",
        "organization_id": ORG,
        "workspace_id": WORKSPACE,
        "monthly_spend_limit_usd": "400.00",
        "calibrated_at": "2026-10-09",
    }
    payload.update(overrides)
    for key in [name for name, value in payload.items() if value is None]:
        payload.pop(key)
    path = tmp_path / "anthropic-reserve.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


class FakeAnthropic:
    """Route each call by path and record which key it carried."""

    def __init__(self, routes: dict[str, list[tuple[int, Any]] | tuple[int, Any]]) -> None:
        self.routes = {
            path: list(value) if isinstance(value, list) else [value]
            for path, value in routes.items()
        }
        self.calls: list[tuple[str, dict[str, list[str]], str]] = []

    def __call__(self, url: str, api_key: str) -> tuple[int, Any]:
        parts = urllib.parse.urlsplit(url)
        self.calls.append((parts.path, urllib.parse.parse_qs(parts.query), api_key))
        queue = self.routes.get(parts.path)
        if not queue:
            raise AssertionError(f"unexpected Anthropic call: {url}")
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response


def _key_record(**overrides: Any) -> tuple[int, Any]:
    payload = {
        "id": KEY_ID,
        "name": "stele-bench-eval",
        "status": "active",
        "partial_key_hint": "sk-ant-api03-abc...WXYZ",
        "scope": {"type": "workspace", "workspace_id": WORKSPACE},
        "workspace_id": WORKSPACE,
        "type": "api_key",
    }
    payload.update(overrides)
    return 200, payload


def _cost(*cents: tuple[str | None, str]) -> tuple[int, Any]:
    return 200, {
        "data": [
            {
                "starting_at": "2026-10-01T00:00:00Z",
                "ending_at": "2026-10-02T00:00:00Z",
                "results": [
                    {"amount": amount, "currency": "USD", "workspace_id": workspace}
                    for workspace, amount in cents
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }


def _fence(**overrides: Any) -> FakeAnthropic:
    routes: dict[str, Any] = {
        "/v1/models": (200, {"data": [{"id": "claude-haiku-4-5"}]}),
        "/v1/organizations/me": (200, {"id": ORG, "name": "Bench Org", "type": "organization"}),
        f"/v1/organizations/api_keys/{KEY_ID}": _key_record(),
        "/v1/organizations/cost_report": _cost((WORKSPACE, "4250.6"), (None, "99999")),
        "/v1/organizations/usage_report/messages": _usage(),
    }
    routes.update(overrides)
    return FakeAnthropic(routes)


def _check(tmp_path: Path, fake: FakeAnthropic, cap: str = "10.00", **config: Any):
    return check_anthropic_reserve(
        acknowledged_pair_cap_usd=Decimal(cap),
        config_path=_config(tmp_path, **config),
        api_key=KEY,
        admin_key=ADMIN,
        getter=fake,
        now=NOW,
    )


def test_preflight_reports_headroom_from_the_workspace_only(tmp_path: Path) -> None:
    fake = _fence()
    receipt = _check(tmp_path, fake)

    # 4250.6 cents in the bench workspace; the default workspace's cost is ignored.
    assert receipt["month_to_date_cost_usd"] == "42.51"
    assert receipt["headroom_before_pair_usd"] == "357.49"
    assert receipt["headroom_after_pair_usd"] == "347.49"
    assert receipt["model_provider"] == "anthropic"
    assert receipt["key_fingerprint"] == KEY_SHA256[:12]
    assert KEY not in json.dumps(receipt)
    assert ADMIN not in json.dumps(receipt)

    by_path = {path: (query, key) for path, query, key in fake.calls}
    assert by_path["/v1/models"][1] == KEY
    assert by_path[f"/v1/organizations/api_keys/{KEY_ID}"][1] == ADMIN
    cost_query, cost_key = by_path["/v1/organizations/cost_report"]
    assert cost_key == ADMIN
    assert cost_query["starting_at"] == ["2026-10-01T00:00:00Z"]
    assert cost_query["ending_at"] == ["2026-10-11T00:00:00Z"]
    assert cost_query["group_by[]"] == ["workspace_id"]


def test_preflight_refuses_a_cap_the_monthly_limit_cannot_absorb(tmp_path: Path) -> None:
    with pytest.raises(GatewayReserveError, match="does not cover the acknowledged"):
        _check(tmp_path, _fence(), cap="358.00")


def test_preflight_refuses_an_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(GatewayReserveError, match="not the configured dedicated eval key"):
        _check(tmp_path, _fence(), key_sha256="0" * 64)


def test_preflight_refuses_a_stale_attestation(tmp_path: Path) -> None:
    with pytest.raises(GatewayReserveError, match="last checked 40 days ago"):
        _check(tmp_path, _fence(), calibrated_at="2026-08-31")


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"status": "inactive"}, "not active"),
        ({"name": "someone-else"}, "different name"),
        (
            {"scope": {"type": "workspace", "workspace_id": "wrkspc_01Other"}},
            "configured benchmark workspace",
        ),
        ({"partial_key_hint": "sk-ant-api03-zzz...AAAA"}, "key hint"),
    ],
)
def test_preflight_refuses_a_misplaced_key(
    tmp_path: Path, record: dict[str, Any], message: str
) -> None:
    fake = _fence(**{f"/v1/organizations/api_keys/{KEY_ID}": _key_record(**record)})
    with pytest.raises(GatewayReserveError, match=message):
        _check(tmp_path, fake)


def test_a_rejected_eval_key_is_an_auth_error(tmp_path: Path) -> None:
    with pytest.raises(GatewayAuthError):
        _check(tmp_path, _fence(**{"/v1/models": (401, {"type": "error"})}))


def test_the_default_workspace_is_not_accepted(tmp_path: Path) -> None:
    with pytest.raises(GatewayReserveError, match="dedicated workspace"):
        load_reserve_config(_config(tmp_path, workspace_id="default"))


def test_the_eval_and_admin_keys_must_differ(tmp_path: Path) -> None:
    with pytest.raises(GatewayReserveError, match="must be different"):
        check_anthropic_reserve(
            acknowledged_pair_cap_usd=Decimal("1.00"),
            config_path=_config(tmp_path),
            api_key=KEY,
            admin_key=KEY,
            getter=_fence(),
            now=NOW,
        )


def test_a_missing_admin_key_fails_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY_EVAL", raising=False)
    fake = _fence()
    with pytest.raises(GatewayReserveError, match="ANTHROPIC_ADMIN_KEY_EVAL is not exported"):
        check_anthropic_reserve(
            acknowledged_pair_cap_usd=Decimal("1.00"),
            config_path=_config(tmp_path),
            api_key=KEY,
            getter=fake,
            now=NOW,
        )
    assert fake.calls == []


def _usage(*rows: dict[str, Any], has_more: bool = False, page: str | None = None):
    return 200, {
        "data": [
            {
                "starting_at": "2026-10-10T12:00:00Z",
                "ending_at": "2026-10-10T12:01:00Z",
                "results": list(rows),
            }
        ],
        "has_more": has_more,
        "next_page": page,
    }


def _row(model: str, **tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "uncached_input_tokens": tokens.get("uncached", 0),
        "cache_read_input_tokens": tokens.get("read", 0),
        "cache_creation": {
            "ephemeral_5m_input_tokens": tokens.get("write5m", 0),
            "ephemeral_1h_input_tokens": 0,
        },
        "output_tokens": tokens.get("output", 0),
    }


PREFLIGHT = {
    "checked_at": "2026-10-10T12:00:30Z",
    "acknowledged_pair_cap_usd": "2.00",
    "key_id": KEY_ID,
}


def test_drain_prices_the_key_usage_since_preflight() -> None:
    # Haiku 4.5: 1M uncached in = $1, 100k out = $0.50, 1M cache read = $0.10,
    # 200k five-minute cache writes = $0.25. Paginated across two pages.
    fake = FakeAnthropic(
        {
            "/v1/organizations/usage_report/messages": [
                _usage(
                    _row("claude-haiku-4-5", uncached=1_000_000, output=100_000),
                    has_more=True,
                    page="page_2",
                ),
                _usage(_row("claude-haiku-4-5", read=1_000_000, write5m=200_000)),
            ]
        }
    )
    observation = check_anthropic_drain(
        preflight=PREFLIGHT,
        admin_key=ADMIN,
        getter=fake,
        now=datetime(2026, 10, 10, 13, tzinfo=UTC),
    )
    assert observation["observed"] is True
    assert observation["spent_usd"] == "1.85"
    query = fake.calls[0][1]
    assert query["starting_at"] == ["2026-10-10T12:00:00Z"]
    assert query["bucket_width"] == ["1m"]
    assert query["api_key_ids[]"] == [KEY_ID]
    assert fake.calls[1][1]["page"] == ["page_2"]


def test_drain_trips_past_the_declared_cap() -> None:
    fake = FakeAnthropic(
        {
            "/v1/organizations/usage_report/messages": _usage(
                _row("claude-sonnet-5-5", output=300_000)
            )
        }
    )
    with pytest.raises(GatewayDrainError) as error:
        check_anthropic_drain(preflight=PREFLIGHT, admin_key=ADMIN, getter=fake, now=NOW)
    assert error.value.observation["spent_usd"] == "3.00"


def test_drain_fails_open_on_transport_errors_but_not_on_unpriced_usage() -> None:
    flaky = FakeAnthropic(
        {"/v1/organizations/usage_report/messages": GatewayReserveError("request failed")}
    )
    observation = check_anthropic_drain(preflight=PREFLIGHT, admin_key=ADMIN, getter=flaky, now=NOW)
    assert observation["observed"] is False

    unpriced = FakeAnthropic(
        {"/v1/organizations/usage_report/messages": _usage(_row("claude-mystery-9", output=1))}
    )
    with pytest.raises(UnpricedUsageError):
        check_anthropic_drain(preflight=PREFLIGHT, admin_key=ADMIN, getter=unpriced, now=NOW)

    rejected = FakeAnthropic({"/v1/organizations/usage_report/messages": (403, None)})
    with pytest.raises(GatewayAuthError):
        check_anthropic_drain(preflight=PREFLIGHT, admin_key=ADMIN, getter=rejected, now=NOW)


def test_long_runs_switch_the_drain_to_hourly_buckets() -> None:
    fake = FakeAnthropic({"/v1/organizations/usage_report/messages": _usage()})
    check_anthropic_drain(
        preflight=PREFLIGHT,
        admin_key=ADMIN,
        getter=fake,
        now=datetime(2026, 10, 11, 13, tzinfo=UTC),
    )
    query = fake.calls[0][1]
    assert query["bucket_width"] == ["1h"]
    assert query["starting_at"] == ["2026-10-10T12:00:00Z"]


def test_the_live_getter_sends_the_key_only_in_a_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            return b'{"data": []}'

    def fake_urlopen(request: Any, timeout: float) -> Response:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        return Response()

    real = _REAL_GET_JSON
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert real("https://api.anthropic.com/v1/models?limit=1", KEY) == (200, {"data": []})
    assert KEY not in captured["url"]
    assert captured["headers"]["X-api-key"] == KEY
    assert captured["headers"]["Anthropic-version"] == "2023-06-01"


def test_preflight_refuses_an_admin_key_from_another_organization(tmp_path: Path) -> None:
    fake = _fence(**{"/v1/organizations/me": (200, {"id": "11111111-2222-3333-4444-555555555555"})})
    with pytest.raises(GatewayReserveError, match="different organization"):
        _check(tmp_path, fake)


def test_drain_prices_dated_snapshot_ids_like_their_alias() -> None:
    fake = FakeAnthropic(
        {
            "/v1/organizations/usage_report/messages": _usage(
                _row("claude-haiku-4-5-20251001", output=200_000)
            )
        }
    )
    observation = check_anthropic_drain(preflight=PREFLIGHT, admin_key=ADMIN, getter=fake, now=NOW)
    assert observation["spent_usd"] == "1.00"


def test_month_to_date_counts_todays_usage_before_the_cost_report_settles(
    tmp_path: Path,
) -> None:
    # The cost report has nothing for today yet; usage must still count
    # against the monthly limit.
    fake = _fence(
        **{
            "/v1/organizations/cost_report": _cost(),
            "/v1/organizations/usage_report/messages": _usage(
                _row("claude-opus-5-5", output=3_000_000)
            ),
        }
    )
    receipt = _check(tmp_path, fake)
    assert receipt["month_to_date_settled_cost_usd"] == "0.00"
    # The fake answers both usage queries with the same $60 row.
    assert receipt["month_to_date_priced_usage_usd"] == "120.00"
    assert receipt["month_to_date_cost_usd"] == "120.00"
    usage_queries = [q for path, q, _ in fake.calls if path.endswith("usage_report/messages")]
    assert all(q["workspace_ids[]"] == [WORKSPACE] for q in usage_queries)
    # Today from hourly buckets (daily ones hold only finished days), then the
    # finished days of the month up to midnight.
    assert usage_queries[0]["bucket_width"] == ["1h"]
    assert usage_queries[0]["starting_at"] == ["2026-10-10T00:00:00Z"]
    assert usage_queries[1]["bucket_width"] == ["1d"]
    assert usage_queries[1]["ending_at"] == ["2026-10-10T00:00:00Z"]
