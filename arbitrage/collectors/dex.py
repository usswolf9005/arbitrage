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


_CHAIN_ALIASES: dict[str, tuple[str, str]] = {
    "1": ("1", "ETHEREUM"),
    "eth": ("1", "ETHEREUM"),
    "ethereum": ("1", "ETHEREUM"),
    "10": ("10", "OPTIMISM"),
    "optimism": ("10", "OPTIMISM"),
    "56": ("56", "BSC"),
    "bsc": ("56", "BSC"),
    "binance-smart-chain": ("56", "BSC"),
    "137": ("137", "POLYGON"),
    "matic": ("137", "POLYGON"),
    "polygon": ("137", "POLYGON"),
    "polygon_pos": ("137", "POLYGON"),
    "8453": ("8453", "BASE"),
    "base": ("8453", "BASE"),
    "42161": ("42161", "ARBITRUM"),
    "arbitrum": ("42161", "ARBITRUM"),
    "arbitrum_one": ("42161", "ARBITRUM"),
    "43114": ("43114", "AVALANCHE"),
    "avalanche": ("43114", "AVALANCHE"),
    "solana": ("solana", "SOLANA"),
}
_CLEAN_CODE_RE = re.compile(r"[^A-Z0-9]+")


@dataclass(frozen=True, slots=True)
class DexPoolObservation:
    chain_id: str
    chain_code: str
    asset_symbol: str
    asset_name: str
    token_address: str
    token_decimals: int
    quote_symbol: str
    quote_name: str
    quote_token_address: str
    quote_token_decimals: int
    venue_code: str
    venue_name: str
    pool_address: str
    market_symbol: str
    observed_at_ms: int
    raw_price: float
    price_usd: float
    price_krw: float | None
    liquidity_usd: float | None
    volume_24h: float | None
    reserve0_raw: str
    reserve1_raw: str
    block_number: int | None
    stale: bool
    payload: dict[str, Any]

    @property
    def market_key(self) -> str:
        return f"{self.chain_code}:{self.venue_code}:{self.asset_symbol}-{self.quote_symbol}:{self.pool_address}"

    @property
    def has_pool_snapshot(self) -> bool:
        return any(
            value not in (None, "")
            for value in (self.reserve0_raw, self.reserve1_raw, self.liquidity_usd, self.block_number)
        )


def ingest_dex_fixture(
    store: ArbitrageStore,
    payload: Any,
    *,
    provider_key: str = "dexscreener",
    scope_key: str = "dex",
    now_ms: int | None = None,
    stale_evidence: bool = False,
) -> CollectorResult:
    provider_key = str(provider_key or "dexscreener").strip()
    scope_key = str(scope_key or "dex").strip()
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
                "capability": "dex_pool",
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
) -> list[DexPoolObservation]:
    included = _included_by_id(payload.get("included"))
    pairs = _pair_payloads(payload)
    if not pairs:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="empty_dex_payload",
            message="DEX payload must contain at least one pool or pair",
            field_path="payload",
            payload=payload,
        )

    observations = [
        _parse_pair(
            pair,
            root_payload=payload,
            included=included,
            provider_key=provider_key,
            scope_key=scope_key,
            field_path=field_path,
            now_ms=now_ms,
        )
        for field_path, pair in pairs
    ]
    return observations


def _pair_payloads(payload: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    pairs = payload.get("pairs")
    if isinstance(pairs, Sequence) and not isinstance(pairs, (str, bytes, bytearray)):
        return [(f"pairs[{idx}]", item) for idx, item in enumerate(pairs) if isinstance(item, Mapping)]

    pair = payload.get("pair")
    if isinstance(pair, Mapping):
        return [("pair", pair)]

    data = payload.get("data")
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        return [(f"data[{idx}]", item) for idx, item in enumerate(data) if isinstance(item, Mapping)]
    if isinstance(data, Mapping):
        return [("data", data)]

    return [("payload", payload)]


def _parse_pair(
    pair: Mapping[str, Any],
    *,
    root_payload: Mapping[str, Any],
    included: Mapping[str, Mapping[str, Any]],
    provider_key: str,
    scope_key: str,
    field_path: str,
    now_ms: int | None,
) -> DexPoolObservation:
    if "attributes" in pair or "relationships" in pair:
        return _parse_geckoterminal_pair(
            pair,
            root_payload=root_payload,
            included=included,
            provider_key=provider_key,
            scope_key=scope_key,
            field_path=field_path,
            now_ms=now_ms,
        )
    return _parse_dexscreener_pair(
        pair,
        root_payload=root_payload,
        provider_key=provider_key,
        scope_key=scope_key,
        field_path=field_path,
        now_ms=now_ms,
    )


def _parse_dexscreener_pair(
    pair: Mapping[str, Any],
    *,
    root_payload: Mapping[str, Any],
    provider_key: str,
    scope_key: str,
    field_path: str,
    now_ms: int | None,
) -> DexPoolObservation:
    base_token = _mapping_field(pair, "baseToken", provider_key, scope_key, f"{field_path}.baseToken")
    quote_token = _mapping_field(pair, "quoteToken", provider_key, scope_key, f"{field_path}.quoteToken")
    chain_id, chain_code = _normalize_chain(
        _first_text(pair, ("chainId", "chain_id", "chain"), root_payload.get("chainId") or root_payload.get("chain")),
    )
    pool_address = _address(
        _required_text(pair, ("pairAddress", "pair_address", "poolAddress", "pool_address", "address"), provider_key, scope_key, f"{field_path}.pairAddress"),
    )
    asset_symbol = _symbol(
        _required_text(base_token, ("symbol",), provider_key, scope_key, f"{field_path}.baseToken.symbol")
    )
    quote_symbol = _symbol(
        _required_text(quote_token, ("symbol",), provider_key, scope_key, f"{field_path}.quoteToken.symbol")
    )
    venue_code = _venue_code(
        _required_text(pair, ("dexId", "dex_id", "venue", "venue_code"), provider_key, scope_key, f"{field_path}.dexId")
    )
    observed_at_ms = _observed_at_ms(
        _first_value(pair, ("observed_at_ms", "observedAt", "timestamp", "updatedAt"), root_payload.get("observed_at_ms")),
        provider_key=provider_key,
        scope_key=scope_key,
        field_path=f"{field_path}.observed_at_ms",
        payload=pair,
        now_ms=now_ms,
    )
    price_usd = _required_number(
        _first_value(pair, ("priceUsd", "price_usd")),
        provider_key,
        scope_key,
        f"{field_path}.priceUsd",
        pair,
    )
    raw_price = _optional_number(_first_value(pair, ("priceNative", "price_native", "raw_price"))) or price_usd
    liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), Mapping) else {}
    volume = pair.get("volume") if isinstance(pair.get("volume"), Mapping) else {}

    return DexPoolObservation(
        chain_id=chain_id,
        chain_code=chain_code,
        asset_symbol=asset_symbol,
        asset_name=str(base_token.get("name") or asset_symbol),
        token_address=_address(
            _required_text(base_token, ("address", "contract_address"), provider_key, scope_key, f"{field_path}.baseToken.address")
        ),
        token_decimals=_optional_int(_first_value(base_token, ("decimals",))) or 18,
        quote_symbol=quote_symbol,
        quote_name=str(quote_token.get("name") or quote_symbol),
        quote_token_address=_address(str(quote_token.get("address") or quote_token.get("contract_address") or "")),
        quote_token_decimals=_optional_int(_first_value(quote_token, ("decimals",))) or 18,
        venue_code=venue_code,
        venue_name=str(pair.get("dexId") or pair.get("dex_id") or venue_code),
        pool_address=pool_address,
        market_symbol=f"{asset_symbol}/{quote_symbol}",
        observed_at_ms=observed_at_ms,
        raw_price=raw_price,
        price_usd=price_usd,
        price_krw=_optional_number(_first_value(pair, ("priceKrw", "price_krw"))),
        liquidity_usd=_optional_number(_first_value(pair, ("liquidityUsd", "liquidity_usd"), liquidity.get("usd"))),
        volume_24h=_optional_number(_first_value(pair, ("volume24h", "volume_24h"), volume.get("h24") or volume.get("24h"))),
        reserve0_raw=_raw_text(_first_value(pair, ("reserve0_raw", "reserve0"), liquidity.get("base"))),
        reserve1_raw=_raw_text(_first_value(pair, ("reserve1_raw", "reserve1"), liquidity.get("quote"))),
        block_number=_optional_int(_first_value(pair, ("blockNumber", "block_number"))),
        stale=_is_stale(pair) or _is_stale(root_payload),
        payload=redact_provider_payload(pair),
    )


def _parse_geckoterminal_pair(
    pair: Mapping[str, Any],
    *,
    root_payload: Mapping[str, Any],
    included: Mapping[str, Mapping[str, Any]],
    provider_key: str,
    scope_key: str,
    field_path: str,
    now_ms: int | None,
) -> DexPoolObservation:
    attrs = pair.get("attributes") if isinstance(pair.get("attributes"), Mapping) else {}
    relationships = pair.get("relationships") if isinstance(pair.get("relationships"), Mapping) else {}
    base_token = _relationship_resource(relationships, "base_token", included)
    quote_token = _relationship_resource(relationships, "quote_token", included)
    base_attrs = base_token.get("attributes", {}) if isinstance(base_token.get("attributes"), Mapping) else {}
    quote_attrs = quote_token.get("attributes", {}) if isinstance(quote_token.get("attributes"), Mapping) else {}
    name_symbols = _symbols_from_pair_name(str(attrs.get("name") or ""))

    chain_hint = (
        _relationship_id(relationships, "network")
        or _chain_from_resource_id(str(pair.get("id") or ""))
        or root_payload.get("network")
        or attrs.get("network")
    )
    chain_id, chain_code = _normalize_chain(str(chain_hint or ""))
    pool_address = _address(
        str(attrs.get("address") or attrs.get("pool_address") or _address_from_resource_id(str(pair.get("id") or "")))
    )
    if not pool_address:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="missing_pool_address",
            message="DEX pool payload is missing pool address",
            field_path=f"{field_path}.attributes.address",
            payload=pair,
        )

    asset_symbol = _symbol(str(base_attrs.get("symbol") or attrs.get("base_token_symbol") or name_symbols[0] or ""))
    quote_symbol = _symbol(str(quote_attrs.get("symbol") or attrs.get("quote_token_symbol") or name_symbols[1] or ""))
    if not asset_symbol:
        raise _payload_error(provider_key=provider_key, scope_key=scope_key, error_code="missing_symbol", message="DEX pool payload is missing base token symbol", field_path=f"{field_path}.base_token.symbol", payload=pair)
    if not quote_symbol:
        raise _payload_error(provider_key=provider_key, scope_key=scope_key, error_code="missing_quote_symbol", message="DEX pool payload is missing quote token symbol", field_path=f"{field_path}.quote_token.symbol", payload=pair)

    token_address = _address(str(base_attrs.get("address") or attrs.get("base_token_address") or _address_from_resource_id(str(base_token.get("id") or ""))))
    if not token_address:
        raise _payload_error(provider_key=provider_key, scope_key=scope_key, error_code="missing_token_address", message="DEX pool payload is missing base token address", field_path=f"{field_path}.base_token.address", payload=pair)

    venue_hint = _relationship_id(relationships, "dex") or attrs.get("dex_id") or attrs.get("dex") or "geckoterminal"
    observed_at_ms = _observed_at_ms(
        _first_value(attrs, ("observed_at_ms", "observedAt", "updated_at", "timestamp"), root_payload.get("observed_at_ms")),
        provider_key=provider_key,
        scope_key=scope_key,
        field_path=f"{field_path}.attributes.observed_at_ms",
        payload=pair,
        now_ms=now_ms,
    )
    price_usd = _required_number(
        _first_value(attrs, ("base_token_price_usd", "token_price_usd", "price_usd")),
        provider_key,
        scope_key,
        f"{field_path}.attributes.base_token_price_usd",
        pair,
    )
    raw_price = _optional_number(_first_value(attrs, ("base_token_price_quote_token", "price_in_quote", "raw_price"))) or price_usd
    volume = attrs.get("volume_usd") if isinstance(attrs.get("volume_usd"), Mapping) else {}

    return DexPoolObservation(
        chain_id=chain_id,
        chain_code=chain_code,
        asset_symbol=asset_symbol,
        asset_name=str(base_attrs.get("name") or asset_symbol),
        token_address=token_address,
        token_decimals=_optional_int(base_attrs.get("decimals")) or 18,
        quote_symbol=quote_symbol,
        quote_name=str(quote_attrs.get("name") or quote_symbol),
        quote_token_address=_address(str(quote_attrs.get("address") or _address_from_resource_id(str(quote_token.get("id") or "")))),
        quote_token_decimals=_optional_int(quote_attrs.get("decimals")) or 18,
        venue_code=_venue_code(str(venue_hint)),
        venue_name=str(venue_hint),
        pool_address=pool_address,
        market_symbol=f"{asset_symbol}/{quote_symbol}",
        observed_at_ms=observed_at_ms,
        raw_price=raw_price,
        price_usd=price_usd,
        price_krw=_optional_number(_first_value(attrs, ("price_krw", "base_token_price_krw"))),
        liquidity_usd=_optional_number(_first_value(attrs, ("reserve_in_usd", "liquidity_usd", "liquidityUsd"))),
        volume_24h=_optional_number(_first_value(attrs, ("volume_24h",), volume.get("h24") or volume.get("24h"))),
        reserve0_raw=_raw_text(_first_value(attrs, ("reserve0_raw", "reserve0"))),
        reserve1_raw=_raw_text(_first_value(attrs, ("reserve1_raw", "reserve1"))),
        block_number=_optional_int(_first_value(attrs, ("block_number", "blockNumber"))),
        stale=_is_stale(attrs) or _is_stale(pair) or _is_stale(root_payload),
        payload=redact_provider_payload(pair),
    )


def _write_observation(store: ArbitrageStore, observation: DexPoolObservation, *, provider_key: str) -> None:
    asset_id = store.ensure_asset(
        symbol=observation.asset_symbol,
        name=observation.asset_name,
        canonical_source=provider_key,
    )
    store.ensure_token(
        asset_id=asset_id,
        chain_id=observation.chain_id,
        chain_code=observation.chain_code,
        contract_address=observation.token_address,
        decimals=observation.token_decimals,
    )
    if observation.quote_symbol:
        quote_asset_id = store.ensure_asset(
            symbol=observation.quote_symbol,
            name=observation.quote_name,
            canonical_source=provider_key,
        )
        if observation.quote_token_address:
            store.ensure_token(
                asset_id=quote_asset_id,
                chain_id=observation.chain_id,
                chain_code=observation.chain_code,
                contract_address=observation.quote_token_address,
                decimals=observation.quote_token_decimals,
            )

    venue_id = store.ensure_venue(observation.venue_code, "DEX", observation.venue_name)
    market_id = store.ensure_market(
        market_key=observation.market_key,
        asset_id=asset_id,
        venue_id=venue_id,
        market_type="DEX_POOL",
        chain_code=observation.chain_code,
        pool_address=observation.pool_address,
        market_symbol=observation.market_symbol,
        quote_asset=observation.quote_symbol,
        payload={
            "provider_key": provider_key,
            "chain_id": observation.chain_id,
            "pool_address": observation.pool_address,
            "token_contract_address": observation.token_address,
            "base_token_address": observation.token_address,
        },
    )
    store.record_market_tick(
        market_id=market_id,
        source=provider_key,
        observed_at_ms=observation.observed_at_ms,
        raw_price=observation.raw_price,
        price_usd=observation.price_usd,
        price_krw=observation.price_krw,
        liquidity_usd=observation.liquidity_usd,
        volume_24h=observation.volume_24h,
        stale=observation.stale,
        payload=observation.payload,
    )
    if observation.has_pool_snapshot:
        _record_pool_snapshot(store, market_id=market_id, source=provider_key, observation=observation)


def _record_pool_snapshot(
    store: ArbitrageStore,
    *,
    market_id: int,
    source: str,
    observation: DexPoolObservation,
) -> int:
    with store.conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO arb_pool_snapshots(
                market_id, source, observed_at_ms, reserve0_raw, reserve1_raw,
                liquidity_usd, block_number, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(market_id),
                str(source),
                int(observation.observed_at_ms),
                observation.reserve0_raw,
                observation.reserve1_raw,
                observation.liquidity_usd,
                observation.block_number,
                json.dumps(observation.payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        row = conn.execute(
            "SELECT id FROM arb_pool_snapshots WHERE market_id = ? AND source = ? AND observed_at_ms = ?",
            (int(market_id), str(source), int(observation.observed_at_ms)),
        ).fetchone()
        return int(row["id"])


def _observation_row_count(store: ArbitrageStore) -> int:
    with store.conn() as conn:
        tick_count = conn.execute("SELECT COUNT(*) AS n FROM arb_market_ticks").fetchone()["n"]
        pool_count = conn.execute("SELECT COUNT(*) AS n FROM arb_pool_snapshots").fetchone()["n"]
        return int(tick_count) + int(pool_count)


def _included_by_id(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return {}
    return {
        str(item.get("id")): item
        for item in value
        if isinstance(item, Mapping) and item.get("id") is not None
    }


def _relationship_resource(
    relationships: Mapping[str, Any],
    name: str,
    included: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    resource_id = _relationship_id(relationships, name)
    if not resource_id:
        return {}
    return included.get(resource_id, {"id": resource_id})


def _relationship_id(relationships: Mapping[str, Any], name: str) -> str:
    value = relationships.get(name)
    if not isinstance(value, Mapping):
        return ""
    data = value.get("data")
    if isinstance(data, Mapping):
        return str(data.get("id") or "")
    return ""


def _mapping_field(
    payload: Mapping[str, Any],
    key: str,
    provider_key: str,
    scope_key: str,
    field_path: str,
) -> Mapping[str, Any]:
    value = payload.get(key)
    if isinstance(value, Mapping):
        return value
    raise _payload_error(
        provider_key=provider_key,
        scope_key=scope_key,
        error_code="missing_field",
        message=f"DEX payload is missing object field {key}",
        field_path=field_path,
        payload=payload,
    )


def _required_text(
    payload: Mapping[str, Any],
    keys: tuple[str, ...],
    provider_key: str,
    scope_key: str,
    field_path: str,
) -> str:
    value = _first_value(payload, keys)
    text = str(value).strip() if value is not None else ""
    if text:
        return text
    raise _payload_error(
        provider_key=provider_key,
        scope_key=scope_key,
        error_code="missing_field",
        message=f"DEX payload is missing required field {keys[0]}",
        field_path=field_path,
        payload=payload,
    )


def _required_number(
    value: Any,
    provider_key: str,
    scope_key: str,
    field_path: str,
    payload: Mapping[str, Any],
) -> float:
    number = _optional_number(value)
    if number is None or number <= 0:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="missing_price_usd" if field_path.endswith("priceUsd") or "price_usd" in field_path else "invalid_number",
            message="DEX payload is missing a positive numeric price_usd",
            field_path=field_path,
            payload=payload,
        )
    return number


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


def _normalize_chain(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    key = raw.lower()
    if key in _CHAIN_ALIASES:
        return _CHAIN_ALIASES[key]
    code = _CLEAN_CODE_RE.sub("_", raw.upper()).strip("_")
    return (raw or code or "unknown", code or "UNKNOWN")


def _venue_code(value: str) -> str:
    code = _CLEAN_CODE_RE.sub("_", str(value or "").upper()).strip("_")
    return code or "DEX"


def _symbol(value: str) -> str:
    return _CLEAN_CODE_RE.sub("", str(value or "").upper())


def _address(value: str) -> str:
    return str(value or "").strip().lower()


def _raw_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_stale(payload: Mapping[str, Any]) -> bool:
    value = payload.get("stale") or payload.get("is_stale") or payload.get("expired")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "stale", "expired"}
    status = str(payload.get("status") or payload.get("state") or "").strip().lower()
    return status in {"stale", "expired"}


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
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


def _symbols_from_pair_name(value: str) -> tuple[str, str]:
    for separator in (" / ", "/", "-"):
        if separator in value:
            left, right = value.split(separator, 1)
            return _symbol(left), _symbol(right)
    return "", ""


def _chain_from_resource_id(value: str) -> str:
    return value.split("_", 1)[0] if "_" in value else ""


def _address_from_resource_id(value: str) -> str:
    if "_" not in value:
        return value if value.startswith("0x") else ""
    return value.rsplit("_", 1)[-1]


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
