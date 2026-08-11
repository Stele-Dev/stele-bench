from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import gateway_reserve
import pytest
from gateway_reserve import GatewayReserveError, check_gateway_reserve, load_reserve_config

KEY = "eval-key-value"
KEY_SHA256 = hashlib.sha256(KEY.encode("utf-8")).hexdigest()
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _config(tmp_path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "key_sha256": KEY_SHA256,
        "key_id": "abc123",
        "key_name": "stele-eval",
        "production_reserve_usd": "50.00",
        "calibrated_at": "2026-07-20",
    }
    payload.update(overrides)
    for key in [name for name, value in payload.items() if value is None]:
        payload.pop(key)
    path = tmp_path / "gateway-reserve.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


class FakeGateway:
    """Record every Gateway call and replay canned responses in order."""

    def __init__(self, *responses: tuple[int, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, api_key: str) -> tuple[int, Any]:
        self.calls.append((url, api_key))
        if not self.responses:
            raise AssertionError(f"unexpected Gateway call: {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _credits(balance: str = "500.00", used: str = "12.00") -> tuple[int, Any]:
    return 200, {"balance": balance, "total_used": used}


def _quota(**overrides: Any) -> tuple[int, Any]:
    payload = {
        "quotaEntityId": "api_key_id_abc123",
        "apiKeyName": "stele-eval",
        "limitAmount": 25,
        "currentSpend": 1.04,
        "refreshPeriod": "monthly",
        "active": True,
    }
    payload.update(overrides)
    return 200, payload


def _check(config_path: Path, gateway: FakeGateway, cap: str = "10.00") -> dict[str, Any]:
    return check_gateway_reserve(
        acknowledged_pair_cap_usd=Decimal(cap),
        api_key=KEY,
        config_path=config_path,
        getter=gateway,
        now=NOW,
    )


def test_a_verified_fence_returns_only_non_secret_values(tmp_path: Path) -> None:
    gateway = FakeGateway(_credits(), _quota())

    receipt = _check(_config(tmp_path), gateway)

    assert receipt["shared_balance_usd"] == "500.00"
    assert receipt["production_reserve_usd"] == "50.00"
    assert receipt["acknowledged_pair_cap_usd"] == "10.00"
    assert receipt["headroom_after_reserve_usd"] == "450.00"
    assert receipt["headroom_after_pair_usd"] == "440.00"
    assert receipt["key_budget"] == {
        "limit_amount_usd": "25.00",
        "current_spend_usd": "1.04",
        "remaining_usd": "23.96",
        "refresh_period": "monthly",
        "active": True,
    }
    assert receipt["calibration_age_days"] == 3
    assert receipt["calibration_authority"] == "vercel-dashboard"
    # The key is never returned, and only a short fingerprint prefix identifies it.
    serialized = json.dumps(receipt)
    assert KEY not in serialized
    assert KEY_SHA256 not in serialized
    assert receipt["key_fingerprint"] == KEY_SHA256[:12]
    # It travelled only as the getter's argument, i.e. an Authorization header.
    assert [url for url, _ in gateway.calls] == [
        gateway_reserve.CREDITS_URL,
        f"{gateway_reserve.QUOTAS_URL}?quotaEntityId=api_key_id_abc123",
    ]
    assert {key for _, key in gateway.calls} == {KEY}


def test_the_wrong_exported_key_is_refused_before_any_network_call(tmp_path: Path) -> None:
    gateway = FakeGateway()  # any call raises

    with pytest.raises(GatewayReserveError, match="not the configured dedicated eval key"):
        check_gateway_reserve(
            acknowledged_pair_cap_usd=Decimal("10.00"),
            api_key="production-key",
            config_path=_config(tmp_path),
            getter=gateway,
            now=NOW,
        )
    assert gateway.calls == []


def test_an_unexported_key_fails_rather_than_defaulting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_GATEWAY_KEY_EVAL", raising=False)
    with pytest.raises(GatewayReserveError, match="AI_GATEWAY_KEY_EVAL is not exported"):
        check_gateway_reserve(
            acknowledged_pair_cap_usd=Decimal("10.00"),
            config_path=_config(tmp_path),
            getter=FakeGateway(),
            now=NOW,
        )


def test_a_missing_configuration_explains_the_shape_it_needs(tmp_path: Path) -> None:
    with pytest.raises(GatewayReserveError, match="production_reserve_usd"):
        _check(tmp_path / "absent.json", FakeGateway())


def test_stale_or_future_calibration_fails_because_the_reserve_is_unverifiable(
    tmp_path: Path,
) -> None:
    stale = _config(tmp_path, calibrated_at="2026-05-01")
    with pytest.raises(GatewayReserveError, match="last calibrated 83 days ago"):
        _check(stale, FakeGateway())

    future = _config(tmp_path, calibrated_at="2027-01-01")
    with pytest.raises(GatewayReserveError, match="in the future"):
        _check(future, FakeGateway())

    # A deliberately wider window is honoured.
    generous = _config(tmp_path, calibrated_at="2026-05-01", max_calibration_age_days=180)
    assert _check(generous, FakeGateway(_credits(), _quota()))["calibration_age_days"] == 83


def test_the_shared_balance_must_cover_the_reserve_and_the_pair_ceiling(tmp_path: Path) -> None:
    # 55 balance - 50 reserve = 5 headroom, which does not cover a 10 ceiling.
    with pytest.raises(GatewayReserveError, match="does not cover the acknowledged 10.00"):
        _check(_config(tmp_path), FakeGateway(_credits(balance="55.00")))

    # Exactly enough is enough.
    receipt = _check(_config(tmp_path), FakeGateway(_credits(balance="60.00"), _quota()))
    assert receipt["headroom_after_pair_usd"] == "0.00"


def test_an_unreadable_balance_is_a_failure_not_an_assumption(tmp_path: Path) -> None:
    for response, expected in (
        ((401, {"error": "unauthorized"}), "rejected the eval key"),
        ((500, {"error": "boom"}), "could not be verified"),
        ((200, {"total_used": "1.00"}), "has no balance"),
        ((200, {"balance": "not-money"}), "not a monetary value"),
    ):
        with pytest.raises(GatewayReserveError, match=expected):
            _check(_config(tmp_path), FakeGateway(response))


def test_a_key_with_no_budget_contradicts_the_paid_acknowledgement(tmp_path: Path) -> None:
    gateway = FakeGateway(_credits(), (404, {"error": "Quota not found"}))

    with pytest.raises(GatewayReserveError, match="has no AI Gateway budget"):
        _check(_config(tmp_path), gateway)


def test_an_inactive_mismatched_or_exhausted_budget_fails(tmp_path: Path) -> None:
    cases = (
        (_quota(active=False), "budget is not active"),
        (_quota(apiKeyName="production"), "belongs to a different key"),
        (_quota(limitAmount=25, currentSpend=20), "does not cover the acknowledged"),
    )
    for quota, expected in cases:
        with pytest.raises(GatewayReserveError, match=expected):
            _check(_config(tmp_path), FakeGateway(_credits(), quota))


def test_running_without_a_key_budget_must_be_opted_into_explicitly(tmp_path: Path) -> None:
    gateway = FakeGateway(_credits())

    receipt = _check(_config(tmp_path, require_key_budget=False), gateway)

    assert receipt["key_budget"] is None
    assert [url for url, _ in gateway.calls] == [gateway_reserve.CREDITS_URL]


def test_configuration_rejects_ambiguity_rather_than_guessing(tmp_path: Path) -> None:
    cases = (
        ({"schema_version": 2}, "schema_version must be 1"),
        ({"key_sha256": "short"}, "64-character hexadecimal"),
        ({"key_id": "bad id/with slash"}, "portable AI Gateway key identifier"),
        ({"key_name": "   "}, "key_name must name the key"),
        ({"production_reserve_usd": "-1"}, "non-negative"),
        ({"production_reserve_usd": "abc"}, "not a monetary value"),
        ({"calibrated_at": "not-a-date"}, "ISO date"),
        ({"calibrated_at": None}, "last reconciled"),
        ({"max_calibration_age_days": 0}, "between 1 and 365"),
        ({"max_calibration_age_days": True}, "between 1 and 365"),
        ({"require_key_budget": "yes"}, "must be true or false"),
        ({"unexpected": 1}, "unknown reserve configuration keys: unexpected"),
    )
    for overrides, expected in cases:
        with pytest.raises(GatewayReserveError, match=expected):
            load_reserve_config(_config(tmp_path, **overrides))


def test_a_symlinked_or_oversized_configuration_is_refused(tmp_path: Path) -> None:
    real = _config(tmp_path)
    link = tmp_path / "linked.json"
    link.symlink_to(real)
    with pytest.raises(GatewayReserveError, match="not a regular file"):
        load_reserve_config(link)

    oversized = tmp_path / "big.json"
    oversized.write_text(" " * (gateway_reserve.MAX_CONFIG_BYTES + 1), encoding="utf-8")
    with pytest.raises(GatewayReserveError, match="implausibly large"):
        load_reserve_config(oversized)


def test_default_config_path_sits_beside_the_run_state(tmp_path: Path) -> None:
    assert gateway_reserve.default_config_path(tmp_path).name == "gateway-reserve.json"


def test_key_spend_observation_preserves_sub_cent_precision(tmp_path: Path) -> None:
    gateway = FakeGateway(_quota(currentSpend="1.047321"))

    receipt = gateway_reserve.observe_gateway_key_spend(
        config_path=_config(tmp_path), api_key=KEY, getter=gateway, now=NOW
    )

    assert receipt["current_spend_usd"] == "1.047321"
    assert receipt["source"] == "vercel-ai-gateway-quota"
    assert receipt["exclusive_to_interval"] is False
    assert KEY not in json.dumps(receipt)
    assert [url for url, _ in gateway.calls] == [
        f"{gateway_reserve.QUOTAS_URL}?quotaEntityId=api_key_id_abc123"
    ]


def test_exclusive_interval_requires_attestation_and_serializes_callers(
    tmp_path: Path,
) -> None:
    with pytest.raises(GatewayReserveError, match="exclusive_key_use_attested"):
        with gateway_reserve.exclusive_gateway_interval(_config(tmp_path)):
            pass

    config = _config(tmp_path, exclusive_key_use_attested=True)
    with gateway_reserve.exclusive_gateway_interval(config) as interval:
        assert interval["exclusive"] is True
        assert interval["key_fingerprint"] == KEY_SHA256[:12]
        with pytest.raises(GatewayReserveError, match="already owns"):
            with gateway_reserve.exclusive_gateway_interval(config):
                pass
    with gateway_reserve.exclusive_gateway_interval(config) as next_interval:
        assert next_interval["exclusive"] is True


def test_spend_observation_records_exclusive_key_attestation(tmp_path: Path) -> None:
    receipt = gateway_reserve.observe_gateway_key_spend(
        config_path=_config(tmp_path, exclusive_key_use_attested=True),
        api_key=KEY,
        getter=FakeGateway(_quota(currentSpend="2.00")),
        now=NOW,
    )

    assert receipt["exclusive_key_use_attested"] is True
    assert receipt["exclusive_to_interval"] is False


# ── The between-arms tripwire ────────────────────────────────────────────────
# A preflight proves the pair FITS above the reserve; it cannot prove the pair
# BEHAVES. The 2026-07-12 incident was a run that stayed under its own budget
# and still drained the shared balance, so the balance itself is re-measured.

PREFLIGHT = {"shared_balance_usd": "500.00", "acknowledged_pair_cap_usd": "10.00"}


def _drain(gateway: FakeGateway) -> dict[str, Any]:
    return gateway_reserve.check_gateway_drain(
        preflight=PREFLIGHT, api_key=KEY, getter=gateway, now=NOW
    )


def test_a_drain_within_the_ceiling_is_recorded_and_allowed() -> None:
    observation = _drain(FakeGateway(_credits(balance="496.00")))

    assert observation["observed"] is True
    assert observation["drained_usd"] == "4.00"
    assert observation["balance_usd"] == "496.00"


def test_a_drain_past_the_ceiling_stops_the_remaining_arm() -> None:
    with pytest.raises(gateway_reserve.GatewayDrainError) as caught:
        _drain(FakeGateway(_credits(balance="480.00")))

    assert "dropped 20.00 USD" in str(caught.value)
    # The observation rides along so the caller can persist what it saw.
    assert caught.value.observation["drained_usd"] == "20.00"


def test_a_balance_that_rose_is_not_a_negative_drain() -> None:
    # Somebody topped the account up mid-pair; that is not spend.
    assert _drain(FakeGateway(_credits(balance="600.00")))["drained_usd"] == "0.00"


def test_a_transport_failure_fails_open_but_says_so() -> None:
    """One flaky GET must not abandon a pair that has already been paid for."""

    observation = _drain(FakeGateway(GatewayReserveError("AI Gateway request failed: /v1/credits")))

    assert observation["observed"] is False
    assert "request failed" in observation["reason"]
    assert observation["drained_usd"] is None


def test_a_rejected_key_does_not_fail_open() -> None:
    """The preflight proved the key was good, so a 401 means it changed underneath."""

    with pytest.raises(gateway_reserve.GatewayAuthError, match="rejected the eval key"):
        _drain(FakeGateway((401, {"error": "unauthorized"})))
