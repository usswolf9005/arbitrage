from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arbitrage.store import ArbitrageStore

from .base import (
    STATUS_DEGRADED,
    STATUS_OK,
    CollectorResult,
    ProviderPayloadError,
    ensure_payload_mapping,
    monotonic_cursor_value,
    normalize_observed_at_ms,
    provider_payload_error,
    redact_provider_payload,
)


_CLEAN_CODE_RE = re.compile(r"[^A-Z0-9]+")
_QUOTE_SUFFIXES = ("USDT", "USDC", "KRW", "USD", "BTC", "ETH")


@dataclass(frozen=True, slots=True)
class FxRateObservation:
    pair: str
    source: str
    observed_at_ms: int
    rate: float
    stale: bool
    payload: dict[str, Any]


def ingest_fx_fixture(
    store: ArbitrageStore,
    payload: Any,
    *,
    provider_key: str = "fx_public",
    scope_key: str = "USDT-KRW",
    now_ms: int | None = None,
    stale_evidence: bool = False,
) -> CollectorResult:
    provider_key = str(provider_key or "fx_public").strip()
    scope_key = str(scope_key or "USDT-KRW").strip()
    cursor_before = store.get_collect_cursor(provider_key, scope_key)

    try:
        payload_map = ensure_payload_mapping(payload, provider_key=provider_key, scope_key=scope_key)
        observations = _parse_observations(
            payload_map,
            provider_key=provider_key,
            scope_key=scope_key,
            now_ms=now_ms,
        )
    except ProviderPayloadError as exc:
        store.record_collect_failure(
            provider_key=provider_key,
            scope_key=scope_key,
            cursor_before=cursor_before,
            error_code=exc.error_code,
            retryable=False,
            raw_payload=exc.to_deadletter_payload(),
        )
        return CollectorResult(
            provider_key=provider_key,
            scope_key=scope_key,
            cursor_before=cursor_before,
            cursor_after=cursor_before,
            status=STATUS_DEGRADED,
            inserted_count=0,
            deadletter_count=1,
        )

    before_rows = _fx_rate_row_count(store)
    for observation in observations:
        _write_observation(store, observation)
    inserted_count = _fx_rate_row_count(store) - before_rows

    if stale_evidence and any(observation.stale for observation in observations):
        store.record_collect_failure(
            provider_key=provider_key,
            scope_key=scope_key,
            cursor_before=cursor_before,
            error_code="provider_result_stale",
            retryable=True,
            raw_payload={
                "provider": provider_key,
                "capability": "fx_rate",
                "stale_source": True,
                "retry_count": 0,
                "payload_summary": redact_provider_payload(payload_map),
            },
        )
        return CollectorResult(
            provider_key=provider_key,
            scope_key=scope_key,
            cursor_before=cursor_before,
            cursor_after=cursor_before,
            status=STATUS_DEGRADED,
            inserted_count=inserted_count,
            deadletter_count=1,
        )

    cursor_after = monotonic_cursor_value(
        cursor_before,
        max(observation.observed_at_ms for observation in observations),
    )
    store.record_collect_success(
        provider_key=provider_key,
        scope_key=scope_key,
        cursor_value=cursor_after,
        collected_count=len(observations),
        inserted_count=inserted_count,
    )
    return CollectorResult(
        provider_key=provider_key,
        scope_key=scope_key,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        status=STATUS_OK,
        inserted_count=inserted_count,
        deadletter_count=0,
    )


def _parse_observations(
    payload: Mapping[str, Any],
    *,
    provider_key: str,
    scope_key: str,
    now_ms: int | None,
) -> list[FxRateObservation]:
    rate_payloads = _rate_payloads(payload)
    if not rate_payloads:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="empty_fx_payload",
            message="FX payload must contain at least one rate",
            field_path="payload",
            payload=payload,
        )

    return [
        _parse_rate(
            item,
            root_payload=payload,
            provider_key=provider_key,
            scope_key=scope_key,
            field_path=field_path,
            now_ms=now_ms,
        )
        for field_path, item in rate_payloads
    ]


def _rate_payloads(payload: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    rates = payload.get("rates")
    if isinstance(rates, Sequence) and not isinstance(rates, (str, bytes, bytearray)):
        return [(f"rates[{idx}]", item) for idx, item in enumerate(rates) if isinstance(item, Mapping)]
    if isinstance(rates, Mapping):
        items: list[tuple[str, Mapping[str, Any]]] = []
        for pair, rate in rates.items():
            if isinstance(rate, Mapping):
                item = dict(rate)
                item.setdefault("pair", pair)
            else:
                item = {"pair": pair, "rate": rate}
            items.append((f"rates.{pair}", item))
        return items

    data = payload.get("data")
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        return [(f"data[{idx}]", item) for idx, item in enumerate(data) if isinstance(item, Mapping)]
    if isinstance(data, Mapping):
        return [("data", data)]

    return [("payload", payload)]


def _parse_rate(
    item: Mapping[str, Any],
    *,
    root_payload: Mapping[str, Any],
    provider_key: str,
    scope_key: str,
    field_path: str,
    now_ms: int | None,
) -> FxRateObservation:
    pair = _pair(item, root_payload=root_payload, scope_key=scope_key)
    if not pair:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="missing_fx_pair",
            message="FX payload is missing pair/base/quote information",
            field_path=f"{field_path}.pair",
            payload=item,
        )

    rate = _optional_number(
        _first_value(
            item,
            ("rate", "fx_rate", "implied_rate", "usdt_krw", "price", "last", "close"),
        )
    )
    if rate is None or rate <= 0:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="missing_fx_rate",
            message="FX payload is missing a positive numeric rate",
            field_path=f"{field_path}.rate",
            payload=item,
        )

    observed_at_ms = _observed_at_ms(
        _first_value(
            item,
            ("observed_at_ms", "observedAt", "timestamp", "ts", "time"),
            _first_value(root_payload, ("observed_at_ms", "observedAt", "timestamp", "ts", "time")),
        ),
        provider_key=provider_key,
        scope_key=scope_key,
        field_path=f"{field_path}.observed_at_ms",
        payload=item,
        now_ms=now_ms,
    )

    source = _first_text(
        item,
        ("source", "provider", "venue", "exchange"),
        _first_text(root_payload, ("source", "provider", "venue", "exchange"), provider_key),
    )
    return FxRateObservation(
        pair=pair,
        source=source or provider_key,
        observed_at_ms=observed_at_ms,
        rate=rate,
        stale=_is_stale(item) or _is_stale(root_payload),
        payload=redact_provider_payload(item),
    )


def _write_observation(store: ArbitrageStore, observation: FxRateObservation) -> int:
    with store.conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO arb_fx_rates(pair, source, observed_at_ms, rate, stale, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                observation.pair,
                observation.source,
                int(observation.observed_at_ms),
                float(observation.rate),
                1 if observation.stale else 0,
                json.dumps(observation.payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        row = conn.execute(
            "SELECT id FROM arb_fx_rates WHERE pair = ? AND source = ? AND observed_at_ms = ?",
            (observation.pair, observation.source, int(observation.observed_at_ms)),
        ).fetchone()
        return int(row["id"])


def _fx_rate_row_count(store: ArbitrageStore) -> int:
    with store.conn() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM arb_fx_rates").fetchone()["n"])


def _pair(item: Mapping[str, Any], *, root_payload: Mapping[str, Any], scope_key: str) -> str:
    base = _code(_first_text(item, ("base", "base_asset"), _first_text(root_payload, ("base", "base_asset"))))
    quote = _code(_first_text(item, ("quote", "quote_asset"), _first_text(root_payload, ("quote", "quote_asset"))))
    if base and quote:
        return f"{base}/{quote}"

    value = _first_text(
        item,
        ("pair", "symbol", "market", "currency_pair"),
        _first_text(root_payload, ("pair", "symbol", "market", "currency_pair")),
    )
    if not value:
        value = scope_key.rsplit(":", 1)[-1]
    return _normalize_pair(value)


def _normalize_pair(value: str) -> str:
    text = str(value or "").strip()
    for separator in ("/", "-", "_"):
        if separator in text:
            left, right = text.split(separator, 1)
            base = _code(left)
            quote = _code(right)
            return f"{base}/{quote}" if base and quote else ""

    compact = _code(text)
    for suffix in _QUOTE_SUFFIXES:
        if compact.endswith(suffix) and len(compact) > len(suffix):
            return f"{compact[: -len(suffix)]}/{suffix}"
    return compact


def _observed_at_ms(
    value: Any,
    *,
    provider_key: str,
    scope_key: str,
    field_path: str,
    payload: Mapping[str, Any],
    now_ms: int | None,
) -> int:
    try:
        return normalize_observed_at_ms(value, now_ms=now_ms)
    except ProviderPayloadError as exc:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code=exc.error_code,
            message=exc.message,
            field_path=field_path,
            payload=payload,
        ) from exc


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_value(payload: Mapping[str, Any], keys: tuple[str, ...], fallback: Any = None) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return fallback


def _first_text(payload: Mapping[str, Any], keys: tuple[str, ...], fallback: Any = None) -> str:
    value = _first_value(payload, keys, fallback)
    return str(value or "").strip()


def _is_stale(payload: Mapping[str, Any]) -> bool:
    value = payload.get("stale")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "stale"}
    return False


def _code(value: str) -> str:
    return _CLEAN_CODE_RE.sub("_", str(value or "").upper()).strip("_")


def _payload_error(
    *,
    provider_key: str,
    scope_key: str,
    error_code: str,
    message: str,
    field_path: str,
    payload: Mapping[str, Any],
) -> ProviderPayloadError:
    return provider_payload_error(
        provider_key=provider_key,
        scope_key=scope_key,
        error_code=error_code,
        message=message,
        field_path=field_path,
        payload=payload,
    )
