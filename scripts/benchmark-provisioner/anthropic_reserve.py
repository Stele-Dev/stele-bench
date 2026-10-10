#!/usr/bin/env python3
"""Verify the first-party Anthropic spending fence before a paid run spends anything.

This is the ``anthropic`` model provider's counterpart of :mod:`gateway_reserve`
and keeps its call shapes, so the oracle's injected ``reserve_checker`` and
``drain_checker`` seams work unchanged.

The risk is different from the Gateway's. The Gateway fence protects a balance
that other services draw on too. A dedicated Anthropic workspace shares its
budget with nothing else, so a run cannot starve another service. What a run
*can* do is spend past the credits it was meant to use and onto the
organization's card. Two things stop that:

* a **monthly spend limit on a dedicated Console workspace**, which Anthropic
  enforces server-side. It is the hard stop, and nothing here can replace it;
* this module, which refuses to start a run that the limit cannot absorb and
  which catches a misconfigured key before the first token.

Before a paid run starts it requires, in order:

1. **Key identity.** ``ANTHROPIC_API_KEY_EVAL`` must match the configured
   SHA-256 fingerprint.
2. **A fresh attestation.** Anthropic exposes neither the credit balance nor
   the workspace spend limit through its API, so the operator records the limit
   with a calibration date, and the fence fails once that date is stale.
3. **A live key.** ``GET /v1/models`` must accept it. This costs nothing.
4. **The right key in the right workspace.** The Admin API must report the key
   active, under the configured name, in the configured non-default workspace,
   with a ``partial_key_hint`` that matches the exported key.
5. **Headroom.** The workspace's month-to-date cost plus the acknowledged cap
   must fit under the attested monthly limit.

Between arms, :func:`check_anthropic_drain` prices the eval key's usage since
the preflight from the Admin usage report and stops before the next arm if it
exceeds the acknowledged cap. The report is documented to lag about five
minutes and was observed lagging an hour, so this check undercounts recent
arms; the server-side limit is what catches the remainder.

The Admin API's figures are advisory. The Console's Cost page is authoritative
for billing. No key is ever written, logged, or returned; only a short prefix of
its fingerprint identifies it in a receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import model_provider
from gateway_reserve import GatewayAuthError, GatewayDrainError, GatewayReserveError

API_BASE_URL = "https://api.anthropic.com/v1"
MODELS_URL = f"{API_BASE_URL}/models"
API_KEYS_URL = f"{API_BASE_URL}/organizations/api_keys"
ORGANIZATION_URL = f"{API_BASE_URL}/organizations/me"
COST_REPORT_URL = f"{API_BASE_URL}/organizations/cost_report"
USAGE_REPORT_URL = f"{API_BASE_URL}/organizations/usage_report/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_CONFIG_NAME = "anthropic-reserve.json"
DEFAULT_MAX_CALIBRATION_AGE_DAYS = 30
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_REPORT_PAGES = 20
_KEY_ID = re.compile(r"^apikey_[A-Za-z0-9]{1,128}$")
_WORKSPACE_ID = re.compile(r"^wrkspc_[A-Za-z0-9]{1,128}$")
_ORGANIZATION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "key_sha256",
        "key_id",
        "key_name",
        "organization_id",
        "workspace_id",
        "monthly_spend_limit_usd",
        "calibrated_at",
        "max_calibration_age_days",
        "note",
    }
)

CONFIG_TEMPLATE = """{
  "schema_version": 1,
  "key_sha256": "<sha256 of the dedicated eval API key>",
  "key_id": "apikey_<the key's id in the Console>",
  "key_name": "<the key's name in the Console>",
  "organization_id": "<the organization's id, from Console settings>",
  "workspace_id": "wrkspc_<the dedicated benchmark workspace>",
  "monthly_spend_limit_usd": "400.00",
  "calibrated_at": "YYYY-MM-DD"
}"""

ADVISORY = (
    "Admin API figures are advisory; the Claude Console Cost page is authoritative "
    "for billing, and the workspace spend limit is the hard stop."
)


@dataclass(frozen=True)
class AnthropicReserveConfig:
    """Operator-owned, non-secret configuration for the direct-API fence."""

    key_sha256: str
    key_id: str
    key_name: str
    organization_id: str
    workspace_id: str
    monthly_spend_limit_usd: Decimal
    calibrated_at: date
    max_calibration_age_days: int = DEFAULT_MAX_CALIBRATION_AGE_DAYS


class UnpricedUsageError(GatewayReserveError):
    """The usage report was read but cannot be priced. Never fail open on it."""


# (url, api_key) -> (status, decoded json)
JsonGetter = Callable[[str, str], tuple[int, Any]]


def _money(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise GatewayReserveError(f"{label} is not a monetary value")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise GatewayReserveError(f"{label} is not a monetary value") from None
    if not amount.is_finite() or amount < 0:
        raise GatewayReserveError(f"{label} must be a finite, non-negative amount")
    return amount


def _usd(amount: Decimal) -> str:
    return f"{amount:.2f}"


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def default_config_path(state_root: Path) -> Path:
    return state_root / DEFAULT_CONFIG_NAME


def load_reserve_config(path: Path) -> AnthropicReserveConfig:
    """Read the operator's fence configuration, rejecting anything ambiguous."""

    try:
        metadata = path.lstat()
    except OSError:
        raise GatewayReserveError(
            f"no Anthropic reserve configuration at {path}. A paid run on the anthropic "
            f"provider needs one; create it with mode 0600 in this shape:\n{CONFIG_TEMPLATE}"
        ) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise GatewayReserveError(f"Anthropic reserve configuration is not a regular file: {path}")
    if metadata.st_size > MAX_CONFIG_BYTES:
        raise GatewayReserveError("Anthropic reserve configuration is implausibly large")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GatewayReserveError(f"could not read {path}") from error
    if not isinstance(payload, dict):
        raise GatewayReserveError("Anthropic reserve configuration must be a JSON object")
    unknown = sorted(set(payload) - _ALLOWED_CONFIG_KEYS)
    if unknown:
        raise GatewayReserveError(f"unknown reserve configuration keys: {', '.join(unknown)}")
    if payload.get("schema_version") != 1:
        raise GatewayReserveError("reserve configuration schema_version must be 1")

    key_sha256 = str(payload.get("key_sha256", "")).strip().lower()
    if not _SHA256.fullmatch(key_sha256):
        raise GatewayReserveError("key_sha256 must be a 64-character hexadecimal digest")
    key_id = str(payload.get("key_id", "")).strip()
    if not _KEY_ID.fullmatch(key_id):
        raise GatewayReserveError("key_id must be the key's Console id (apikey_...)")
    key_name = str(payload.get("key_name", "")).strip()
    if not key_name:
        raise GatewayReserveError("key_name must name the key as the Console shows it")
    organization_id = str(payload.get("organization_id", "")).strip().lower()
    if not _ORGANIZATION_ID.fullmatch(organization_id):
        raise GatewayReserveError(
            "organization_id must be the organization's id (a UUID). It is what stops an "
            "admin key from another organization verifying this run"
        )
    workspace_id = str(payload.get("workspace_id", "")).strip()
    if not _WORKSPACE_ID.fullmatch(workspace_id):
        raise GatewayReserveError(
            "workspace_id must be a dedicated workspace's id (wrkspc_...). The default "
            "workspace is not allowed: its costs cannot be attributed in the cost report"
        )
    limit = _money(payload.get("monthly_spend_limit_usd"), "monthly_spend_limit_usd")
    if limit <= 0:
        raise GatewayReserveError("monthly_spend_limit_usd must be greater than zero")
    max_age = payload.get("max_calibration_age_days", DEFAULT_MAX_CALIBRATION_AGE_DAYS)
    if isinstance(max_age, bool) or not isinstance(max_age, int) or not 1 <= max_age <= 365:
        raise GatewayReserveError("max_calibration_age_days must be an integer between 1 and 365")

    return AnthropicReserveConfig(
        key_sha256=key_sha256,
        key_id=key_id,
        key_name=key_name,
        organization_id=organization_id,
        workspace_id=workspace_id,
        monthly_spend_limit_usd=limit,
        calibrated_at=_calibration_date(payload.get("calibrated_at")),
        max_calibration_age_days=max_age,
    )


def _calibration_date(value: object) -> date:
    if not isinstance(value, str) or not value.strip():
        raise GatewayReserveError(
            "calibrated_at must record when the workspace spend limit was last checked "
            "in the Claude Console (YYYY-MM-DD)"
        )
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise GatewayReserveError("calibrated_at must be an ISO date (YYYY-MM-DD)") from None


def _get_json(url: str, api_key: str) -> tuple[int, Any]:
    """GET one Anthropic endpoint. The key travels in a header and never elsewhere."""

    request = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Accept": "application/json",
            "User-Agent": "stele-bench-oracle/1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = int(response.status)
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = int(error.code)
        try:
            payload = error.read(MAX_RESPONSE_BYTES + 1)
        except OSError:
            payload = b""
    except (urllib.error.URLError, TimeoutError, OSError):
        # Deliberately opaque: a transport error message can echo the request.
        raise GatewayReserveError(
            f"Anthropic API request failed: {urllib.parse.urlsplit(url).path}"
        )
    if len(payload) > MAX_RESPONSE_BYTES:
        raise GatewayReserveError("Anthropic API response exceeded the expected size")
    if not payload:
        return status, None
    try:
        return status, json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayReserveError(
            f"Anthropic API returned non-JSON for {urllib.parse.urlsplit(url).path}"
        ) from None


def _require(name: str, value: str | None) -> str:
    key = value if value is not None else os.environ.get(name)
    if not key:
        raise GatewayReserveError(f"{name} is not exported")
    return key


def check_anthropic_reserve(
    *,
    acknowledged_pair_cap_usd: Decimal,
    config_path: Path,
    api_key: str | None = None,
    admin_key: str | None = None,
    getter: JsonGetter | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify identity, key placement and monthly headroom; return non-secret values.

    Raises :class:`GatewayReserveError` on any unverifiable condition. Callers
    must run this *before* creating run state so a refusal leaves nothing
    behind.
    """

    fetch = getter or _get_json
    ceiling = _money(acknowledged_pair_cap_usd, "acknowledged pair cap")
    key = _require(model_provider.ANTHROPIC_KEY_ENV, api_key)
    admin = _require(model_provider.ANTHROPIC_ADMIN_KEY_ENV, admin_key)
    config = load_reserve_config(config_path)

    if hashlib.sha256(key.encode("utf-8")).hexdigest() != config.key_sha256:
        raise GatewayReserveError(
            f"the exported {model_provider.ANTHROPIC_KEY_ENV} is not the configured dedicated "
            "eval key; refusing to bill an unknown key"
        )
    if key == admin:
        raise GatewayReserveError("the eval key and the Admin API key must be different keys")

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    calibration_age = (moment.date() - config.calibrated_at).days
    if calibration_age < 0:
        raise GatewayReserveError("calibrated_at is in the future")
    if calibration_age > config.max_calibration_age_days:
        raise GatewayReserveError(
            f"the workspace spend limit was last checked {calibration_age} days ago, past the "
            f"{config.max_calibration_age_days}-day limit; confirm it in the Claude Console "
            "and update calibrated_at"
        )

    status, _ = fetch(f"{MODELS_URL}?limit=1", key)
    if status in (401, 403):
        raise GatewayAuthError(f"the Anthropic API rejected the eval key (HTTP {status})")
    if status != 200:
        raise GatewayReserveError(f"the eval key could not be verified (HTTP {status})")

    _verify_key_placement(fetch, admin, key, config)
    settled_cost = _workspace_month_to_date(fetch, admin, config.workspace_id, moment)
    priced_usage = _workspace_usage_month_to_date(fetch, admin, config.workspace_id, moment)
    month_to_date = max(settled_cost, priced_usage)
    headroom = config.monthly_spend_limit_usd - month_to_date
    if headroom < ceiling:
        raise GatewayReserveError(
            f"workspace month-to-date cost {_usd(month_to_date)} USD leaves {_usd(headroom)} USD "
            f"under the {_usd(config.monthly_spend_limit_usd)} USD monthly spend limit, which "
            f"does not cover the acknowledged {_usd(ceiling)} USD cap"
        )

    return {
        "schema_version": 1,
        "model_provider": model_provider.ANTHROPIC,
        "checked_at": _timestamp(moment),
        "key_fingerprint": config.key_sha256[:12],
        "key_id": config.key_id,
        "key_name": config.key_name,
        "organization_id": config.organization_id,
        "workspace_id": config.workspace_id,
        "monthly_spend_limit_usd": _usd(config.monthly_spend_limit_usd),
        "month_to_date_cost_usd": _usd(month_to_date),
        "month_to_date_settled_cost_usd": _usd(settled_cost),
        "month_to_date_priced_usage_usd": _usd(priced_usage),
        "acknowledged_pair_cap_usd": _usd(ceiling),
        "headroom_before_pair_usd": _usd(headroom),
        "headroom_after_pair_usd": _usd(headroom - ceiling),
        "calibrated_at": config.calibrated_at.isoformat(),
        "calibration_age_days": calibration_age,
        "calibration_authority": "claude-console",
        "advisory": ADVISORY,
    }


def _verify_key_placement(
    fetch: JsonGetter, admin: str, key: str, config: AnthropicReserveConfig
) -> None:
    status, organization = fetch(ORGANIZATION_URL, admin)
    if status in (401, 403):
        raise GatewayAuthError(f"the Anthropic Admin API rejected the admin key (HTTP {status})")
    if status != 200 or not isinstance(organization, dict):
        raise GatewayReserveError(f"the admin key's organization could not be read (HTTP {status})")
    if str(organization.get("id", "")).lower() != config.organization_id:
        raise GatewayReserveError(
            "the admin key belongs to a different organization than the configured one"
        )
    status, payload = fetch(f"{API_KEYS_URL}/{config.key_id}", admin)
    if status in (401, 403):
        raise GatewayAuthError(f"the Anthropic Admin API rejected the admin key (HTTP {status})")
    if status == 404:
        raise GatewayReserveError(f"the Admin API has no API key {config.key_id}")
    if status != 200 or not isinstance(payload, dict):
        raise GatewayReserveError(f"the eval key's record could not be read (HTTP {status})")
    if payload.get("status") != "active":
        raise GatewayReserveError("the eval key is not active")
    if payload.get("name") != config.key_name:
        raise GatewayReserveError("the Admin API key record has a different name than configured")
    scope = payload.get("scope")
    workspace = scope.get("workspace_id") if isinstance(scope, dict) else None
    workspace = workspace or payload.get("workspace_id")
    if workspace != config.workspace_id:
        raise GatewayReserveError(
            "the eval key does not belong to the configured benchmark workspace"
        )
    if not _hint_matches(payload.get("partial_key_hint"), key):
        raise GatewayReserveError(
            "the exported eval key does not match the Admin API record's key hint"
        )


def _hint_matches(hint: object, key: str) -> bool:
    """Compare a redacted hint such as ``sk-ant-api03-R2D...igAA`` with the key."""

    if not isinstance(hint, str) or "..." not in hint:
        return False
    head, _, tail = hint.partition("...")
    return bool(head) and bool(tail) and key.startswith(head) and key.endswith(tail)


def _report_pages(
    fetch: JsonGetter, admin: str, url: str, params: list[tuple[str, str]]
) -> Iterator[dict]:
    page: str | None = None
    for _ in range(MAX_REPORT_PAGES):
        query = list(params) + ([("page", page)] if page else [])
        status, payload = fetch(f"{url}?{urllib.parse.urlencode(query)}", admin)
        if status in (401, 403):
            raise GatewayAuthError(
                f"the Anthropic Admin API rejected the admin key (HTTP {status})"
            )
        if status != 200 or not isinstance(payload, dict):
            raise GatewayReserveError(
                f"{urllib.parse.urlsplit(url).path} could not be read (HTTP {status})"
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise GatewayReserveError(f"{urllib.parse.urlsplit(url).path} returned no data")
        for bucket in data:
            if isinstance(bucket, dict):
                yield bucket
        if payload.get("has_more") is not True:
            return
        page = payload.get("next_page")
        if not isinstance(page, str) or not page:
            raise GatewayReserveError("a report claimed more pages without a cursor")
    raise GatewayReserveError("a report had more pages than the fence will read")


def _workspace_month_to_date(
    fetch: JsonGetter, admin: str, workspace_id: str, moment: datetime
) -> Decimal:
    """Sum this calendar month's cost for one workspace, in USD.

    The cost report counts in cents and buckets by UTC day; asking up to the
    start of tomorrow includes today's partial bucket.
    """

    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    params = [
        ("starting_at", _timestamp(start)),
        ("ending_at", _timestamp(end)),
        ("bucket_width", "1d"),
        ("group_by[]", "workspace_id"),
        ("limit", "31"),
    ]
    cents = Decimal(0)
    for bucket in _report_pages(fetch, admin, COST_REPORT_URL, params):
        for result in bucket.get("results") or ():
            if not isinstance(result, dict) or result.get("workspace_id") != workspace_id:
                continue
            if result.get("currency") != "USD":
                raise GatewayReserveError("the cost report returned a non-USD amount")
            cents += _money(result.get("amount"), "cost report amount")
    return cents / 100


def _priced_usage(fetch: JsonGetter, admin: str, params: list[tuple[str, str]]) -> Decimal:
    """Price every row of one usage-report query at first-party list prices."""

    total = Decimal(0)
    for bucket in _report_pages(fetch, admin, USAGE_REPORT_URL, params):
        for row in bucket.get("results") or ():
            if not isinstance(row, dict):
                continue
            model = row.get("model")
            creation = row.get("cache_creation") or {}
            try:
                total += model_provider.usage_cost_usd(
                    str(model),
                    uncached_input_tokens=int(row.get("uncached_input_tokens") or 0),
                    cache_read_input_tokens=int(row.get("cache_read_input_tokens") or 0),
                    cache_write_5m_tokens=int(creation.get("ephemeral_5m_input_tokens") or 0),
                    cache_write_1h_tokens=int(creation.get("ephemeral_1h_input_tokens") or 0),
                    output_tokens=int(row.get("output_tokens") or 0),
                )
            except model_provider.ModelProviderError:
                raise UnpricedUsageError(
                    f"the eval key used {model!r}, which has no recorded price; the fence "
                    "cannot tell how much was spent"
                ) from None
            except (TypeError, ValueError):
                raise UnpricedUsageError("the usage report returned a malformed row") from None
    return total


def _workspace_usage_month_to_date(
    fetch: JsonGetter, admin: str, workspace_id: str, moment: datetime
) -> Decimal:
    """Price this calendar month's usage for one workspace from the usage report.

    The cost report settles by UTC day and shows nothing for today until the
    day closes. Daily usage buckets likewise hold only finished days, so today
    is read from hourly buckets, which fill in within about an hour.
    """

    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    today = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    finished_days = [
        ("starting_at", _timestamp(month_start)),
        ("ending_at", _timestamp(today)),
        ("bucket_width", "1d"),
        ("workspace_ids[]", workspace_id),
        ("group_by[]", "model"),
        ("limit", "31"),
    ]
    today_hours = [
        ("starting_at", _timestamp(today)),
        ("bucket_width", "1h"),
        ("workspace_ids[]", workspace_id),
        ("group_by[]", "model"),
        ("limit", "24"),
    ]
    total = _priced_usage(fetch, admin, today_hours)
    if today > month_start:
        total += _priced_usage(fetch, admin, finished_days)
    return total


def observe_key_spend_since(
    *,
    key_id: str,
    since: datetime,
    admin_key: str | None = None,
    getter: JsonGetter | None = None,
    now: datetime | None = None,
) -> Decimal:
    """Price the eval key's usage since ``since`` at first-party list prices."""

    fetch = getter or _get_json
    admin = _require(model_provider.ANTHROPIC_ADMIN_KEY_ENV, admin_key)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    start = since.astimezone(UTC).replace(second=0, microsecond=0)
    hourly = moment - start > timedelta(hours=23)
    if hourly:
        start = start.replace(minute=0)
    params = [
        ("starting_at", _timestamp(start)),
        ("bucket_width", "1h" if hourly else "1m"),
        ("api_key_ids[]", key_id),
        ("group_by[]", "model"),
        ("limit", "168" if hourly else "1440"),
    ]
    return _priced_usage(fetch, admin, params)


def check_anthropic_drain(
    *,
    preflight: dict[str, Any],
    admin_key: str | None = None,
    getter: JsonGetter | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Re-measure the eval key's spend between arms and stop past the declared cap.

    Same contract as :func:`gateway_reserve.check_gateway_drain`: a transport
    failure fails open and says so; a rejected admin key does not; anything the
    check can see, it acts on.
    """

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    checked_at = _timestamp(moment)
    ceiling = _money(preflight.get("acknowledged_pair_cap_usd"), "preflight pair cap")
    key_id = str(preflight.get("key_id", ""))
    if not _KEY_ID.fullmatch(key_id):
        raise GatewayReserveError("the preflight receipt does not name the eval key")
    try:
        since = datetime.fromisoformat(str(preflight.get("checked_at")).replace("Z", "+00:00"))
    except ValueError:
        raise GatewayReserveError("the preflight receipt has no valid checked_at") from None

    try:
        spent = observe_key_spend_since(
            key_id=key_id, since=since, admin_key=admin_key, getter=getter, now=moment
        )
    except (GatewayAuthError, UnpricedUsageError):
        raise
    except GatewayReserveError as error:
        return {
            "checked_at": checked_at,
            "observed": False,
            "reason": str(error),
            "spent_usd": None,
            "drained_usd": None,
        }

    observation = {
        "checked_at": checked_at,
        "observed": True,
        "reason": None,
        "source": "anthropic-usage-report",
        "spent_usd": _usd(spent),
        "drained_usd": _usd(spent),
        "lag_note": "the usage report can lag an hour or more and undercounts recent arms",
    }
    if spent > ceiling:
        raise GatewayDrainError(
            f"the eval key has spent about {_usd(spent)} USD since this run's preflight, past "
            f"its acknowledged {_usd(ceiling)} USD ceiling; the remaining arm is not started",
            observation=observation,
        )
    return observation


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect the direct-Anthropic spending fence.")
    parser.add_argument("--config", type=Path, required=True, help="reserve configuration path")
    parser.add_argument(
        "--pair-cap-usd",
        default="0",
        help="the cap to test headroom against; 0 only reports the current state",
    )
    args = parser.parse_args(argv)
    try:
        receipt = check_anthropic_reserve(
            acknowledged_pair_cap_usd=_money(args.pair_cap_usd, "pair cap"),
            config_path=args.config,
        )
    except GatewayReserveError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
