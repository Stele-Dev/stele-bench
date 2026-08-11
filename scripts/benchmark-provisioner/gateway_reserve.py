#!/usr/bin/env python3
"""Verify shared Gateway headroom before a paid benchmark run may spend anything.

The eval key draws on the **same** Vercel AI Gateway credit balance as
production, so a benchmark run that drains it takes the hosted assistant down
with it. Acknowledging a per-pair cap does not prevent that: the cap bounds
what one run may spend, not what is left for everything else.

This module adds the missing half of the fence. Before a paid pair starts it
requires, in order:

1. **Key identity.** The exported key must match a configured SHA-256
   fingerprint. This is what stops a run from being billed to the production
   key because the wrong value was exported into the shell.
2. **A per-key budget that actually exists.** ``run-pair``'s acknowledgement
   already asserts that ``AI_GATEWAY_KEY_EVAL`` is "a dedicated eval key with
   an external budget ceiling" — until now nothing checked that claim. The
   Gateway quota API is asked directly, and an absent budget fails.
3. **Shared headroom.** The team balance must cover a configured production
   reserve *plus* the acknowledged pair ceiling, so a run cannot consume the
   float production depends on.

and then, once the run is in flight:

4. **Observed drain.** The preflight proves the pair *fits*; it cannot prove
   the pair *behaves*. :func:`check_gateway_drain` re-reads the balance between
   arms and stops before the next one if more has drained than was declared.
   The incident this all exists for was a run that stayed comfortably under its
   own budget and drained the shared balance anyway.

Every failure is raised before the caller creates state, and every value the
caller may persist is non-secret. The key itself is never written, logged, or
returned; only a short prefix of its fingerprint identifies it in a receipt.

The Gateway's own numbers are **advisory**. Credits move for reasons this
process cannot see, and the Vercel dashboard remains authoritative for actual
billing — which is why the configured reserve carries a calibration date and
goes stale on purpose.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
CREDITS_URL = f"{GATEWAY_BASE_URL}/credits"
QUOTAS_URL = f"{GATEWAY_BASE_URL}/quotas"
DEFAULT_CONFIG_NAME = "gateway-reserve.json"
DEFAULT_MAX_CALIBRATION_AGE_DAYS = 30
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 64 * 1024
_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "key_sha256",
        "key_id",
        "key_name",
        "production_reserve_usd",
        "calibrated_at",
        "max_calibration_age_days",
        "require_key_budget",
        "exclusive_key_use_attested",
        "note",
    }
)

CONFIG_TEMPLATE = """{
  "schema_version": 1,
  "key_sha256": "<sha256 of the dedicated eval key>",
  "key_id": "<AI Gateway API key id>",
  "key_name": "<the key's name in the Vercel dashboard>",
  "production_reserve_usd": "25.00",
  "calibrated_at": "YYYY-MM-DD",
  "max_calibration_age_days": 30,
  "require_key_budget": true,
  "exclusive_key_use_attested": false
}"""

ADVISORY = (
    "Gateway-reported figures are advisory; the Vercel dashboard remains "
    "authoritative for actual billing and for recalibrating the reserve."
)


class GatewayReserveError(RuntimeError):
    """A paid run's spending fence could not be verified; never proceed."""


class GatewayAuthError(GatewayReserveError):
    """The Gateway rejected the key. Never transient, so never fail open on it."""


class GatewayDrainError(GatewayReserveError):
    """Observed spend outran the acknowledged ceiling; do not start the next arm."""

    def __init__(self, message: str, *, observation: dict[str, Any]) -> None:
        super().__init__(message)
        self.observation = observation


@dataclass(frozen=True)
class ReserveConfig:
    """Operator-owned, non-secret configuration for the spending fence."""

    key_sha256: str
    key_id: str
    key_name: str
    production_reserve_usd: Decimal
    calibrated_at: date
    max_calibration_age_days: int = DEFAULT_MAX_CALIBRATION_AGE_DAYS
    require_key_budget: bool = True
    exclusive_key_use_attested: bool = False


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


def default_config_path(state_root: Path) -> Path:
    return state_root / DEFAULT_CONFIG_NAME


def load_reserve_config(path: Path) -> ReserveConfig:
    """Read the operator's fence configuration, rejecting anything ambiguous."""

    try:
        metadata = path.lstat()
    except OSError:
        raise GatewayReserveError(
            f"no Gateway reserve configuration at {path}. A paid run needs one; "
            f"create it with mode 0600 in this shape:\n{CONFIG_TEMPLATE}"
        ) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise GatewayReserveError(f"Gateway reserve configuration is not a regular file: {path}")
    if metadata.st_size > MAX_CONFIG_BYTES:
        raise GatewayReserveError("Gateway reserve configuration is implausibly large")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GatewayReserveError(f"could not read {path}") from error
    if not isinstance(payload, dict):
        raise GatewayReserveError("Gateway reserve configuration must be a JSON object")
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
        raise GatewayReserveError("key_id must be a portable AI Gateway key identifier")
    key_name = str(payload.get("key_name", "")).strip()
    if not key_name:
        raise GatewayReserveError("key_name must name the key as the dashboard shows it")

    max_age = payload.get("max_calibration_age_days", DEFAULT_MAX_CALIBRATION_AGE_DAYS)
    if isinstance(max_age, bool) or not isinstance(max_age, int) or not 1 <= max_age <= 365:
        raise GatewayReserveError("max_calibration_age_days must be an integer between 1 and 365")
    require_budget = payload.get("require_key_budget", True)
    if not isinstance(require_budget, bool):
        raise GatewayReserveError("require_key_budget must be true or false")
    exclusive_key_use = payload.get("exclusive_key_use_attested", False)
    if not isinstance(exclusive_key_use, bool):
        raise GatewayReserveError("exclusive_key_use_attested must be true or false")

    return ReserveConfig(
        key_sha256=key_sha256,
        key_id=key_id,
        key_name=key_name,
        production_reserve_usd=_money(
            payload.get("production_reserve_usd"), "production_reserve_usd"
        ),
        calibrated_at=_calibration_date(payload.get("calibrated_at")),
        max_calibration_age_days=max_age,
        require_key_budget=require_budget,
        exclusive_key_use_attested=exclusive_key_use,
    )


@contextmanager
def exclusive_gateway_interval(config_path: Path):
    """Serialize one paid interval for an operator-attested exclusive eval key.

    The provider quota endpoint is cumulative. It becomes attributable to one
    benchmark session only when the configured key is dedicated to this
    apparatus and every apparatus caller holds the same non-blocking lock.
    """

    config = load_reserve_config(config_path)
    if not config.exclusive_key_use_attested:
        raise GatewayReserveError(
            "exclusive Gateway billing requires exclusive_key_use_attested=true "
            "after confirming no other process or service uses this eval key"
        )
    lock_path = config_path.with_name(f"{config_path.name}.exclusive.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise GatewayReserveError(
                "another benchmark session already owns the exclusive Gateway interval"
            ) from error
        yield {
            "exclusive": True,
            "key_fingerprint": config.key_sha256[:12],
            "basis": "operator-attested-dedicated-key-plus-process-lock",
        }
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _calibration_date(value: object) -> date:
    if not isinstance(value, str) or not value.strip():
        raise GatewayReserveError(
            "calibrated_at must record when the reserve was last reconciled "
            "against the Vercel dashboard (YYYY-MM-DD)"
        )
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise GatewayReserveError("calibrated_at must be an ISO date (YYYY-MM-DD)") from None


def _get_json(url: str, api_key: str) -> tuple[int, Any]:
    """GET one Gateway endpoint. The key travels in a header and never elsewhere."""

    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
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
        raise GatewayReserveError(f"AI Gateway request failed: {urllib.parse.urlsplit(url).path}")
    if len(payload) > MAX_RESPONSE_BYTES:
        raise GatewayReserveError("AI Gateway response exceeded the expected size")
    if not payload:
        return status, None
    try:
        return status, json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayReserveError(
            f"AI Gateway returned non-JSON for {urllib.parse.urlsplit(url).path}"
        ) from None


def check_gateway_reserve(
    *,
    acknowledged_pair_cap_usd: Decimal,
    api_key: str | None = None,
    config_path: Path,
    getter: JsonGetter | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify identity, per-key budget, and shared headroom; return non-secret values.

    Raises :class:`GatewayReserveError` on any unverifiable condition. Callers
    must run this *before* creating run state so a refusal leaves nothing
    behind.
    """

    fetch = getter or _get_json
    ceiling = _money(acknowledged_pair_cap_usd, "acknowledged pair cap")
    key = api_key if api_key is not None else os.environ.get("AI_GATEWAY_KEY_EVAL")
    if not key:
        raise GatewayReserveError("AI_GATEWAY_KEY_EVAL is not exported")
    config = load_reserve_config(config_path)

    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
    if fingerprint != config.key_sha256:
        raise GatewayReserveError(
            "the exported AI_GATEWAY_KEY_EVAL is not the configured dedicated eval key; "
            "refusing to bill an unknown key"
        )

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    calibration_age = (moment.date() - config.calibrated_at).days
    if calibration_age < 0:
        raise GatewayReserveError("calibrated_at is in the future")
    if calibration_age > config.max_calibration_age_days:
        raise GatewayReserveError(
            f"the production reserve was last calibrated {calibration_age} days ago, past the "
            f"{config.max_calibration_age_days}-day limit; reconcile it against the Vercel "
            "dashboard and update calibrated_at"
        )

    balance, total_used = _shared_balance(fetch, key)
    headroom_after_reserve = balance - config.production_reserve_usd
    if headroom_after_reserve < ceiling:
        raise GatewayReserveError(
            f"shared Gateway balance {_usd(balance)} USD leaves {_usd(headroom_after_reserve)} "
            f"USD above the {_usd(config.production_reserve_usd)} USD production reserve, which "
            f"does not cover the acknowledged {_usd(ceiling)} USD pair ceiling"
        )

    budget = _key_budget(fetch, key, config, ceiling) if config.require_key_budget else None

    return {
        "schema_version": 1,
        "checked_at": moment.isoformat().replace("+00:00", "Z"),
        "key_fingerprint": config.key_sha256[:12],
        "key_id": config.key_id,
        "key_name": config.key_name,
        "shared_balance_usd": _usd(balance),
        "shared_total_used_usd": _usd(total_used),
        "production_reserve_usd": _usd(config.production_reserve_usd),
        "acknowledged_pair_cap_usd": _usd(ceiling),
        "headroom_after_reserve_usd": _usd(headroom_after_reserve),
        "headroom_after_pair_usd": _usd(headroom_after_reserve - ceiling),
        "key_budget": budget,
        "calibrated_at": config.calibrated_at.isoformat(),
        "calibration_age_days": calibration_age,
        "calibration_authority": "vercel-dashboard",
        "advisory": ADVISORY,
    }


def check_gateway_drain(
    *,
    preflight: dict[str, Any],
    api_key: str | None = None,
    getter: JsonGetter | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Re-read the balance between arms and stop if more drained than declared.

    The preflight proves the pair *fits* above the reserve; it cannot prove the
    pair *behaves*. A declared cap is a post-response stop condition, and the
    2026-07-12 incident was a run that stayed under its own budget and still
    drained the shared balance. So the balance itself is measured again before
    the next arm starts.

    Observed drain is the drop in the **shared** balance, so it also counts
    draws by anything else on the account. That over-attribution is deliberate:
    whoever is spending it, the next dollar past this line comes out of the
    reserve, and stopping is the right response either way.

    There is deliberately no separate "balance fell into the reserve" check.
    The preflight already guarantees ceiling <= balance - reserve, so the
    balance cannot reach the reserve until the drain exceeds the ceiling — this
    check is strictly earlier, and a reserve check here would be unreachable
    code pretending to be a safety net.

    A transport failure fails **open** and says so in the returned observation:
    one flaky GET should not abandon a pair that has already been paid for, and
    an unobserved check that is recorded as unobserved is honest. Anything the
    check can actually see, it acts on.

    A rejected key is *not* a transport failure and does not fail open. The
    preflight already proved the key was good, so a 401 here means it changed
    underneath the run, and continuing would spend against something nobody
    verified.
    """

    fetch = getter or _get_json
    key = api_key if api_key is not None else os.environ.get("AI_GATEWAY_KEY_EVAL")
    if not key:
        raise GatewayReserveError("AI_GATEWAY_KEY_EVAL is not exported")
    start = _money(preflight.get("shared_balance_usd"), "preflight shared balance")
    ceiling = _money(preflight.get("acknowledged_pair_cap_usd"), "preflight pair cap")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    checked_at = moment.isoformat().replace("+00:00", "Z")

    try:
        balance, _used = _shared_balance(fetch, key)
    except GatewayAuthError:
        raise
    except GatewayReserveError as error:
        return {
            "checked_at": checked_at,
            "observed": False,
            "reason": str(error),
            "balance_usd": None,
            "drained_usd": None,
        }

    drained = max(Decimal(0), start - balance)
    observation = {
        "checked_at": checked_at,
        "observed": True,
        "reason": None,
        "balance_usd": _usd(balance),
        "drained_usd": _usd(drained),
    }
    if drained > ceiling:
        raise GatewayDrainError(
            f"the shared Gateway balance has dropped {_usd(drained)} USD since this pair's "
            f"preflight, past its acknowledged {_usd(ceiling)} USD ceiling (balance is now "
            f"{_usd(balance)} USD). Either the pair costs more than estimated or something "
            "else is drawing on the balance; the next dollar comes out of the production "
            "reserve, so the remaining arm is not started",
            observation=observation,
        )
    return observation


def observe_gateway_key_spend(
    *,
    config_path: Path,
    api_key: str | None = None,
    getter: JsonGetter | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the dedicated eval key's cumulative spend without rounding it.

    The reserve receipt intentionally rounds money to cents because it is a
    safety rail. A benchmark boundary needs the provider's finer-grained value
    so two observations can attribute a small sequential session. The result is
    still marked as observed rather than magically exclusive: another process
    using the same eval key during the interval would be included in the delta.
    """

    fetch = getter or _get_json
    key = api_key if api_key is not None else os.environ.get("AI_GATEWAY_KEY_EVAL")
    if not key:
        raise GatewayReserveError("AI_GATEWAY_KEY_EVAL is not exported")
    config = load_reserve_config(config_path)
    if hashlib.sha256(key.encode("utf-8")).hexdigest() != config.key_sha256:
        raise GatewayReserveError(
            "the exported AI_GATEWAY_KEY_EVAL is not the configured dedicated eval key"
        )
    entity = f"api_key_id_{config.key_id}"
    url = f"{QUOTAS_URL}?{urllib.parse.urlencode({'quotaEntityId': entity})}"
    status, payload = fetch(url, key)
    if status in (401, 403):
        raise GatewayAuthError(f"the AI Gateway rejected the eval key (HTTP {status})")
    if status != 200 or not isinstance(payload, dict):
        raise GatewayReserveError(f"the eval key's spend could not be observed (HTTP {status})")
    if payload.get("active") is not True or payload.get("apiKeyName") != config.key_name:
        raise GatewayReserveError("the AI Gateway spend belongs to a different or inactive key")
    spend = _money(payload.get("currentSpend", "0"), "key budget currentSpend")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return {
        "observed": True,
        "checked_at": moment.isoformat().replace("+00:00", "Z"),
        "key_fingerprint": config.key_sha256[:12],
        "current_spend_usd": format(spend, "f"),
        "source": "vercel-ai-gateway-quota",
        "exclusive_to_interval": False,
        "exclusive_key_use_attested": config.exclusive_key_use_attested,
    }


def _shared_balance(getter: JsonGetter, key: str) -> tuple[Decimal, Decimal]:
    status, payload = getter(CREDITS_URL, key)
    if status in (401, 403):
        raise GatewayAuthError(f"the AI Gateway rejected the eval key (HTTP {status})")
    if status != 200 or not isinstance(payload, dict):
        raise GatewayReserveError(f"shared Gateway balance could not be verified (HTTP {status})")
    if "balance" not in payload:
        raise GatewayReserveError("the AI Gateway credits response has no balance")
    return _money(payload["balance"], "shared balance"), _money(
        payload.get("total_used", "0"), "shared total_used"
    )


def _key_budget(
    getter: JsonGetter, key: str, config: ReserveConfig, ceiling: Decimal
) -> dict[str, Any]:
    entity = f"api_key_id_{config.key_id}"
    url = f"{QUOTAS_URL}?{urllib.parse.urlencode({'quotaEntityId': entity})}"
    status, payload = getter(url, key)
    if status == 404:
        raise GatewayReserveError(
            f"the eval key {config.key_name!r} has no AI Gateway budget, but a paid run's "
            "acknowledgement asserts an external budget ceiling. Set one on the key, or set "
            "require_key_budget to false to run without that ceiling deliberately"
        )
    if status != 200 or not isinstance(payload, dict):
        raise GatewayReserveError(f"the eval key's budget could not be verified (HTTP {status})")
    if payload.get("active") is not True:
        raise GatewayReserveError("the eval key's AI Gateway budget is not active")
    reported_name = payload.get("apiKeyName")
    if reported_name != config.key_name:
        raise GatewayReserveError(
            "the AI Gateway budget belongs to a different key than the configured eval key"
        )
    limit = _money(payload.get("limitAmount"), "key budget limitAmount")
    spend = _money(payload.get("currentSpend", "0"), "key budget currentSpend")
    remaining = limit - spend
    if remaining < ceiling:
        raise GatewayReserveError(
            f"the eval key's budget leaves {_usd(remaining)} USD, which does not cover the "
            f"acknowledged {_usd(ceiling)} USD pair ceiling"
        )
    refresh = payload.get("refreshPeriod")
    return {
        "limit_amount_usd": _usd(limit),
        "current_spend_usd": _usd(spend),
        "remaining_usd": _usd(remaining),
        "refresh_period": refresh if isinstance(refresh, str) else None,
        "active": True,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect the paid-run spending fence.")
    parser.add_argument("--config", type=Path, required=True, help="reserve configuration path")
    parser.add_argument(
        "--pair-cap-usd",
        default="0",
        help="the two-arm cap to test headroom against; 0 only reports the current state",
    )
    args = parser.parse_args(argv)
    try:
        receipt = check_gateway_reserve(
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
