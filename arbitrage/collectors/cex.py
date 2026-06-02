from __future__ import annotations

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
_COMPACT_QUOTE_SUFFIXES = ("USDT", "USDC", "KRW", "USD", "BTC", "ETH")
_QUOTE_FIRST_VENUES = {"UPBIT", "BITHUMB"}
_PROVIDER_VENUES: dict[str, str] = {
    "binance_public": "BINANCE",
    "binance": "BINANCE",
    "okx_public": "OKX",
    "okx": "OKX",
    "bybit_public": "BYBIT",
    "bybit": "BYBIT",
    "upbit_public": "UPBIT",
    "upbit": "UPBIT",
    "bithumb_public": "BITHUMB",
    "bithumb": "BITHUMB",
}


@dataclass(frozen=True, slots=True)
class CexOrderbookObservation:
    venue_code: str
    venue_name: str
    base_symbol: str
    quote_asset: str
    market_symbol: str
    observed_at_ms: int
    best_bid: float
    best_ask: float
    depth: list[dict[str, Any]]
    deposit_network: str
    stale: bool
    payload: dict[str, Any]

    @property
    def market_key(self) -> str:
        return f"{self.venue_code}:{self.base_symbol}-{self.quote_asset}"


def ingest_cex_orderbook_fixture(
    store: ArbitrageStore,
    payload: Any,
    *,
    provider_key: str = "cex_public",
    scope_key: str = "cex",
    now_ms: int | None = None,
    stale_evidence: bool = False,
) -> CollectorResult:
    provider_key = str(provider_key or "cex_public").strip()
    scope_key = str(scope_key or "cex").strip()
    cursor_before = store.get_collect_cursor(provider_key, scope_key)

    try:
        payload_map = ensure_payload_mapping(payload, provider_key=provider_key, scope_key=scope_key)
        observations = _parse_observations(
            payload_map,
            provider_key=provider_key,
            scope_key=scope_key,
            now_ms=now_ms,
            allow_stale=stale_evidence,
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

    before_rows = _observation_row_count(store)
    for observation in observations:
        _write_observation(store, observation, provider_key=provider_key)
    inserted_count = _observation_row_count(store) - before_rows

    if stale_evidence and any(observation.stale for observation in observations):
        store.record_collect_failure(
            provider_key=provider_key,
            scope_key=scope_key,
            cursor_before=cursor_before,
            error_code="provider_result_stale",
            retryable=True,
            raw_payload={
                "provider": provider_key,
                "capability": "cex_orderbook",
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
    allow_stale: bool = False,
) -> list[CexOrderbookObservation]:
    books = _orderbook_payloads(payload)
    if not books:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="empty_orderbook_payload",
            message="CEX orderbook payload must contain at least one orderbook",
            field_path="payload",
            payload=payload,
        )

    return [
        _parse_orderbook(
            book,
            root_payload=payload,
            provider_key=provider_key,
            scope_key=scope_key,
            field_path=field_path,
            now_ms=now_ms,
            allow_stale=allow_stale,
        )
        for field_path, book in books
    ]


def _orderbook_payloads(payload: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    orderbooks = payload.get("orderbooks")
    if isinstance(orderbooks, Sequence) and not isinstance(orderbooks, (str, bytes, bytearray)):
        return [(f"orderbooks[{idx}]", item) for idx, item in enumerate(orderbooks) if isinstance(item, Mapping)]

    result = payload.get("result")
    if isinstance(result, Mapping):
        return [("result", result)]

    data = payload.get("data")
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        return [(f"data[{idx}]", item) for idx, item in enumerate(data) if isinstance(item, Mapping)]
    if isinstance(data, Mapping):
        return [("data", data)]

    return [("payload", payload)]


def _parse_orderbook(
    book: Mapping[str, Any],
    *,
    root_payload: Mapping[str, Any],
    provider_key: str,
    scope_key: str,
    field_path: str,
    now_ms: int | None,
    allow_stale: bool = False,
) -> CexOrderbookObservation:
    venue_code = _venue_code(
        _first_text(book, ("venue_code", "venue", "exchange"), _first_text(root_payload, ("venue_code", "venue", "exchange")))
        or _venue_from_provider(provider_key)
    )
    venue_name = str(
        _first_text(book, ("venue_name", "exchange_name"), _first_text(root_payload, ("venue_name", "exchange_name")))
        or venue_code
    )

    stale = _is_stale(book) or _is_stale(root_payload)
    if stale and not allow_stale:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="stale_orderbook_payload",
            message="CEX orderbook payload is marked stale",
            field_path=field_path,
            payload=book,
        )

    market_symbol = _market_symbol(book, root_payload=root_payload, scope_key=scope_key)
    base_symbol, quote_asset = _market_parts(
        market_symbol,
        venue_code=venue_code,
        book=book,
        root_payload=root_payload,
    )
    if not base_symbol:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="missing_base_symbol",
            message="CEX orderbook payload is missing base symbol",
            field_path=f"{field_path}.market_symbol",
            payload=book,
        )
    if not quote_asset:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="missing_quote_asset",
            message="CEX orderbook payload is missing quote asset",
            field_path=f"{field_path}.market_symbol",
            payload=book,
        )

    observed_at_ms = _observed_at_ms(
        _first_value(
            book,
            ("observed_at_ms", "observedAt", "timestamp", "ts", "time", "E"),
            _first_value(root_payload, ("observed_at_ms", "observedAt", "timestamp", "ts", "time", "E")),
        ),
        provider_key=provider_key,
        scope_key=scope_key,
        field_path=f"{field_path}.observed_at_ms",
        payload=book,
        now_ms=now_ms,
    )
    depth = _depth_from_payload(book)
    best_bid = _best_price(depth, "bid")
    best_ask = _best_price(depth, "ask")
    best_bid = _optional_number(_first_value(book, ("best_bid", "bid_price"))) or best_bid
    best_ask = _optional_number(_first_value(book, ("best_ask", "ask_price"))) or best_ask

    if best_bid is None or best_bid <= 0:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="missing_best_bid",
            message="CEX orderbook payload is missing a positive best bid",
            field_path=f"{field_path}.bids",
            payload=book,
        )
    if best_ask is None or best_ask <= 0:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="missing_best_ask",
            message="CEX orderbook payload is missing a positive best ask",
            field_path=f"{field_path}.asks",
            payload=book,
        )
    if best_bid >= best_ask:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="stale_orderbook_payload",
            message="CEX orderbook payload has crossed or stale top of book",
            field_path=field_path,
            payload=book,
        )

    return CexOrderbookObservation(
        venue_code=venue_code,
        venue_name=venue_name,
        base_symbol=base_symbol,
        quote_asset=quote_asset,
        market_symbol=f"{base_symbol}/{quote_asset}",
        observed_at_ms=observed_at_ms,
        best_bid=best_bid,
        best_ask=best_ask,
        depth=depth,
        deposit_network=_code(
            _first_text(book, ("deposit_network", "network"), _first_text(root_payload, ("deposit_network", "network")))
        ),
        stale=stale,
        payload=redact_provider_payload(book),
    )


def _write_observation(
    store: ArbitrageStore,
    observation: CexOrderbookObservation,
    *,
    provider_key: str,
) -> None:
    asset_id = store.ensure_asset(
        symbol=observation.base_symbol,
        name=observation.base_symbol,
        canonical_source=provider_key,
    )
    venue_id = store.ensure_venue(observation.venue_code, "CEX", observation.venue_name)
    market_id = store.ensure_market(
        market_key=observation.market_key,
        asset_id=asset_id,
        venue_id=venue_id,
        market_type="CEX_ORDERBOOK",
        chain_code=observation.quote_asset,
        market_symbol=observation.market_symbol,
        quote_asset=observation.quote_asset,
        deposit_network=observation.deposit_network,
        payload={
            "provider_key": provider_key,
            "venue_code": observation.venue_code,
            "quote_asset": observation.quote_asset,
        },
    )
    store.record_orderbook_snapshot(
        market_id=market_id,
        source=provider_key,
        observed_at_ms=observation.observed_at_ms,
        best_bid=observation.best_bid,
        best_ask=observation.best_ask,
        depth=observation.depth,
        stale=observation.stale,
    )
    mid_price = (observation.best_bid + observation.best_ask) / 2
    store.record_market_tick(
        market_id=market_id,
        source=provider_key,
        observed_at_ms=observation.observed_at_ms,
        raw_price=mid_price,
        price_usd=mid_price if observation.quote_asset in {"USD", "USDT", "USDC"} else None,
        price_krw=mid_price if observation.quote_asset == "KRW" else None,
        best_bid=observation.best_bid,
        best_ask=observation.best_ask,
        stale=observation.stale,
        payload=observation.payload,
    )


def _depth_from_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    units = payload.get("orderbook_units")
    if isinstance(units, Sequence) and not isinstance(units, (str, bytes, bytearray)):
        return [
            row
            for unit in units
            if isinstance(unit, Mapping)
            for row in (
                _depth_row("bid", unit.get("bid_price"), unit.get("bid_size")),
                _depth_row("ask", unit.get("ask_price"), unit.get("ask_size")),
            )
            if row is not None
        ]

    depth: list[dict[str, Any]] = []
    for side, keys in (("bid", ("bids", "b", "bid")), ("ask", ("asks", "a", "ask"))):
        levels = _first_value(payload, keys)
        depth.extend(_depth_rows(side, levels))
    return depth


def _depth_rows(side: str, levels: Any) -> list[dict[str, Any]]:
    if levels is None or levels == "":
        return []
    if isinstance(levels, Mapping):
        row = _depth_row(side, levels.get("price"), _first_value(levels, ("quantity", "qty", "size", "amount")))
        return [row] if row is not None else []
    if not isinstance(levels, Sequence) or isinstance(levels, (str, bytes, bytearray)):
        return []

    rows: list[dict[str, Any]] = []
    for level in levels:
        if isinstance(level, Mapping):
            row = _depth_row(
                side,
                _first_value(level, ("price", "rate")),
                _first_value(level, ("quantity", "qty", "size", "amount")),
            )
        elif isinstance(level, Sequence) and not isinstance(level, (str, bytes, bytearray)):
            row = _depth_row(
                side,
                level[0] if len(level) > 0 else None,
                level[1] if len(level) > 1 else None,
            )
        else:
            row = None
        if row is not None:
            rows.append(row)
    return rows


def _depth_row(side: str, price: Any, quantity: Any) -> dict[str, Any] | None:
    normalized_price = _optional_number(price)
    if normalized_price is None or normalized_price <= 0:
        return None
    normalized_quantity = _optional_number(quantity)
    return {
        "side": side,
        "price": normalized_price,
        "quantity": normalized_quantity if normalized_quantity is not None else 0.0,
    }


def _best_price(depth: Sequence[Mapping[str, Any]], side: str) -> float | None:
    prices = [
        price
        for row in depth
        if str(row.get("side")) == side
        for price in (_optional_number(row.get("price")),)
        if price is not None
    ]
    if not prices:
        return None
    return max(prices) if side == "bid" else min(prices)


def _market_symbol(book: Mapping[str, Any], *, root_payload: Mapping[str, Any], scope_key: str) -> str:
    arg = root_payload.get("arg") if isinstance(root_payload.get("arg"), Mapping) else {}
    value = _first_text(
        book,
        ("market_symbol", "symbol", "s", "instId", "market"),
        _first_text(arg, ("instId", "symbol")) or _first_text(root_payload, ("market_symbol", "symbol", "s", "instId", "market")),
    )
    if value:
        return value
    if ":" in scope_key:
        return scope_key.rsplit(":", 1)[-1]
    return scope_key


def _market_parts(
    market_symbol: str,
    *,
    venue_code: str,
    book: Mapping[str, Any],
    root_payload: Mapping[str, Any],
) -> tuple[str, str]:
    base = _symbol(
        _first_text(
            book,
            ("base_symbol", "base_asset", "base", "order_currency"),
            _first_text(root_payload, ("base_symbol", "base_asset", "base", "order_currency")),
        )
    )
    quote = _code(
        _first_text(
            book,
            ("quote_asset", "quote_symbol", "quote", "payment_currency"),
            _first_text(root_payload, ("quote_asset", "quote_symbol", "quote", "payment_currency")),
        )
    )
    if base and quote:
        return base, quote

    parts = _split_delimited_market(market_symbol)
    if parts is not None:
        left, right = parts
        if venue_code in _QUOTE_FIRST_VENUES and left in _COMPACT_QUOTE_SUFFIXES:
            return right, left
        return left, right

    compact = _symbol(market_symbol)
    for suffix in _COMPACT_QUOTE_SUFFIXES:
        if compact.endswith(suffix) and len(compact) > len(suffix):
            return compact[: -len(suffix)], suffix
    return base or compact, quote


def _split_delimited_market(value: str) -> tuple[str, str] | None:
    for separator in ("/", "-", "_"):
        if separator in value:
            left, right = value.split(separator, 1)
            return _symbol(left), _code(right)
    return None


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


def _observation_row_count(store: ArbitrageStore) -> int:
    with store.conn() as conn:
        tick_count = conn.execute("SELECT COUNT(*) AS n FROM arb_market_ticks").fetchone()["n"]
        book_count = conn.execute("SELECT COUNT(*) AS n FROM arb_orderbook_snapshots").fetchone()["n"]
        return int(tick_count) + int(book_count)


def _venue_from_provider(provider_key: str) -> str:
    return _PROVIDER_VENUES.get(str(provider_key).lower(), provider_key.rsplit("_", 1)[0] or "cex")


def _venue_code(value: str) -> str:
    return _code(value) or "CEX"


def _code(value: str) -> str:
    return _CLEAN_CODE_RE.sub("_", str(value or "").upper()).strip("_")


def _symbol(value: str) -> str:
    return _CLEAN_CODE_RE.sub("", str(value or "").upper())


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
