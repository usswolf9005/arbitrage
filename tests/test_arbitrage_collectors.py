from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from arbitrage.collectors.base import (
    STATUS_DEGRADED,
    STATUS_OK,
    CollectorResult,
    ProviderPayloadError,
    ensure_payload_mapping,
    monotonic_cursor_value,
    normalize_observed_at_ms,
    provider_payload_error,
)
from arbitrage.collectors.cex import ingest_cex_orderbook_fixture
from arbitrage.collectors.dex import ingest_dex_fixture
from arbitrage.collectors.fx import ingest_fx_fixture
from arbitrage.collectors.rpc import ingest_rpc_freshness_fixture
from arbitrage.store import ArbitrageStore


DEX_OBSERVED_AT_MS = 1_779_539_696_000
CEX_OBSERVED_AT_MS = 1_779_539_697_000
KRW_OBSERVED_AT_MS = 1_779_539_698_000
FX_OBSERVED_AT_MS = 1_779_539_699_000
RPC_OBSERVED_AT_MS = 1_779_539_700_000
RPC_LATEST_BLOCK = 1_234_567


def _store(tmp_path: Path) -> ArbitrageStore:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    return store


def _table_rows(store: ArbitrageStore, table: str) -> list[dict[str, Any]]:
    with store.conn() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]


def _table_count(store: ArbitrageStore, table: str) -> int:
    with store.conn() as conn:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _dexscreener_payload() -> dict[str, Any]:
    return {
        "schemaVersion": "1.0.0",
        "pairs": [
            {
                "chainId": "polygon",
                "dexId": "quickswap",
                "pairAddress": "0x2222222222222222222222222222222222222222",
                "baseToken": {
                    "address": "0x1111111111111111111111111111111111111111",
                    "name": "Solana",
                    "symbol": "SOL",
                    "decimals": 18,
                },
                "quoteToken": {
                    "address": "0x3333333333333333333333333333333333333333",
                    "name": "USD Coin",
                    "symbol": "USDC",
                    "decimals": 6,
                },
                "priceNative": "70.5",
                "priceUsd": "70.50",
                "priceKrw": "98700.25",
                "liquidity": {"usd": "1234567.89", "base": "1000.5", "quote": "70500.25"},
                "volume": {"h24": "456789.12"},
                "blockNumber": 12345678,
                "observed_at_ms": DEX_OBSERVED_AT_MS,
            }
        ],
    }


def _binance_orderbook_payload() -> dict[str, Any]:
    return {
        "symbol": "SOLUSDT",
        "lastUpdateId": 987654321,
        "deposit_network": "solana",
        "observed_at_ms": CEX_OBSERVED_AT_MS,
        "bids": [["70.10", "12.5"], ["70.00", "5"]],
        "asks": [["70.20", "8.1"], ["70.40", "4"]],
    }


def _okx_orderbook_payload() -> dict[str, Any]:
    return {
        "arg": {"channel": "books", "instId": "SOL-USDT"},
        "data": [
            {
                "ts": str(CEX_OBSERVED_AT_MS),
                "bids": [["70.11", "9.5", "0", "1"]],
                "asks": [["70.22", "7.1", "0", "1"]],
            }
        ],
    }


def _bybit_orderbook_payload() -> dict[str, Any]:
    return {
        "retCode": 0,
        "result": {
            "s": "SOLUSDT",
            "ts": CEX_OBSERVED_AT_MS,
            "b": [["70.12", "11.0"], ["70.05", "3"]],
            "a": [["70.24", "6.0"], ["70.50", "2"]],
        },
    }


def _fx_rate_payload(*, stale: bool = False) -> dict[str, Any]:
    return {
        "pair": "USDT-KRW",
        "source": "upbit_usdt_krw",
        "observed_at_ms": FX_OBSERVED_AT_MS,
        "rate": "1390.25",
        "stale": stale,
        "evidence": {"bid": "1390.0", "ask": "1390.5"},
    }


def _rpc_success_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "observed_at_ms": RPC_OBSERVED_AT_MS,
        "result": {
            "number": hex(RPC_LATEST_BLOCK),
            "hash": "0xabc123",
        },
    }


def test_collector_result_contract_is_stable_and_serializable() -> None:
    result = CollectorResult(
        provider_key=" dexscreener ",
        scope_key=" eth:usdc ",
        cursor_before="1700000000000",
        cursor_after="1700000001000",
        status=STATUS_OK,
        inserted_count=3,
        deadletter_count=0,
    )

    assert result.provider_key == "dexscreener"
    assert result.scope_key == "eth:usdc"
    assert result.status == "OK"
    assert result.cursor_advanced is True
    assert result.to_dict() == {
        "provider_key": "dexscreener",
        "scope_key": "eth:usdc",
        "cursor_before": "1700000000000",
        "cursor_after": "1700000001000",
        "status": "OK",
        "inserted_count": 3,
        "deadletter_count": 0,
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"provider_key": ""}, "provider_key must be a non-empty string"),
        ({"scope_key": ""}, "scope_key must be a non-empty string"),
        ({"status": "FAILED"}, "unsupported collector status"),
        ({"inserted_count": -1}, "inserted_count must be non-negative"),
        ({"deadletter_count": -1}, "deadletter_count must be non-negative"),
    ],
)
def test_collector_result_rejects_invalid_contract_values(kwargs: dict[str, object], message: str) -> None:
    fields: dict[str, object] = {
        "provider_key": "dexscreener",
        "scope_key": "eth:usdc",
        "cursor_before": "",
        "cursor_after": "",
        "status": STATUS_DEGRADED,
        "inserted_count": 0,
        "deadletter_count": 1,
    }
    fields.update(kwargs)

    with pytest.raises(ValueError, match=message):
        CollectorResult(**fields)


def test_normalize_observed_at_ms_defaults_to_now_and_accepts_common_timestamp_shapes() -> None:
    assert normalize_observed_at_ms(None, now_ms=1_700_000_000_123) == 1_700_000_000_123
    assert normalize_observed_at_ms("") > 0
    assert normalize_observed_at_ms(1_700_000_000) == 1_700_000_000_000
    assert normalize_observed_at_ms("1700000000123") == 1_700_000_000_123
    assert normalize_observed_at_ms("2026-05-23T12:34:56Z") == 1_779_539_696_000
    assert (
        normalize_observed_at_ms(datetime(2026, 5, 23, 12, 34, 56, tzinfo=timezone.utc))
        == 1_779_539_696_000
    )


def test_monotonic_cursor_value_preserves_existing_high_watermark() -> None:
    assert monotonic_cursor_value("", 100) == "100"
    assert monotonic_cursor_value("100", 101) == "101"
    assert monotonic_cursor_value("100", 99) == "100"


@pytest.mark.parametrize("bad_value", [False, -1, "not-a-timestamp", object()])
def test_normalize_observed_at_ms_raises_redacted_provider_payload_error(bad_value: object) -> None:
    with pytest.raises(ProviderPayloadError) as exc_info:
        normalize_observed_at_ms(bad_value)

    assert exc_info.value.error_code == "invalid_observed_at_ms"
    assert "observed_at_ms" in str(exc_info.value)


def test_provider_payload_error_shape_redacts_sensitive_values() -> None:
    raw_secret = "redaction_fixture_secret_collector_123"
    payload = {
        "pair": "SOL/USDC",
        "api_key": raw_secret,
        "nested": {
            "Authorization": f"Bearer {raw_secret}",
            "priceUsd": None,
        },
        "events": [{"token": raw_secret}, {"message": f"token={raw_secret}"}],
    }

    error = provider_payload_error(
        provider_key="dexscreener",
        scope_key="solana:sol-usdc",
        error_code="missing_price_usd",
        message=f"provider payload token={raw_secret} is missing priceUsd",
        field_path="pairs[0].priceUsd",
        payload=payload,
    )

    deadletter_payload = error.to_deadletter_payload()
    rendered = f"{error!s} {deadletter_payload!r}"

    assert raw_secret not in rendered
    assert deadletter_payload == {
        "provider_key": "dexscreener",
        "scope_key": "solana:sol-usdc",
        "error_code": "missing_price_usd",
        "message": "provider payload token=<redacted> is missing priceUsd",
        "field_path": "pairs[0].priceUsd",
        "payload": {
            "pair": "SOL/USDC",
            "api_key": "<redacted>",
            "nested": {
                "Authorization": "<redacted>",
                "priceUsd": None,
            },
            "events": [{"token": "<redacted>"}, {"message": "token=<redacted>"}],
        },
    }


def test_ensure_payload_mapping_rejects_non_object_payloads_without_raw_payload_leakage() -> None:
    with pytest.raises(ProviderPayloadError) as exc_info:
        ensure_payload_mapping(["not", "an", "object"], provider_key="upbit_public", scope_key="KRW-SOL")

    payload = exc_info.value.to_deadletter_payload()
    assert payload["provider_key"] == "upbit_public"
    assert payload["scope_key"] == "KRW-SOL"
    assert payload["error_code"] == "invalid_provider_payload"
    assert payload["payload"] == {"received_type": "list"}


def test_dexscreener_fixture_ingests_dex_observations(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = ingest_dex_fixture(
        store,
        _dexscreener_payload(),
        provider_key="dexscreener",
        scope_key="polygon:sol-usdc",
    )

    assert result.status == STATUS_OK
    assert result.cursor_before == ""
    assert result.cursor_after == str(DEX_OBSERVED_AT_MS)
    assert result.inserted_count == 2
    assert store.get_collect_cursor("dexscreener", "polygon:sol-usdc") == str(DEX_OBSERVED_AT_MS)

    assets = {row["symbol"]: row for row in _table_rows(store, "arb_assets")}
    assert {"SOL", "USDC"}.issubset(assets)
    token = _table_rows(store, "arb_tokens")[0]
    assert token["chain_id"] == "137"
    assert token["chain_code"] == "POLYGON"
    assert token["contract_address"] == "0x1111111111111111111111111111111111111111"

    venue = _table_rows(store, "arb_venues")[0]
    assert venue["venue_code"] == "QUICKSWAP"
    assert venue["venue_type"] == "DEX"

    market = _table_rows(store, "arb_markets")[0]
    assert market["market_type"] == "DEX_POOL"
    assert market["market_key"] == "POLYGON:QUICKSWAP:SOL-USDC:0x2222222222222222222222222222222222222222"
    assert market["pool_address"] == "0x2222222222222222222222222222222222222222"
    assert market["market_symbol"] == "SOL/USDC"
    assert market["quote_asset"] == "USDC"

    tick = _table_rows(store, "arb_market_ticks")[0]
    assert tick["source"] == "dexscreener"
    assert tick["observed_at_ms"] == DEX_OBSERVED_AT_MS
    assert tick["raw_price"] == 70.5
    assert tick["price_usd"] == 70.5
    assert tick["price_krw"] == 98700.25
    assert tick["liquidity_usd"] == 1234567.89
    assert tick["volume_24h"] == 456789.12

    pool = _table_rows(store, "arb_pool_snapshots")[0]
    assert pool["reserve0_raw"] == "1000.5"
    assert pool["reserve1_raw"] == "70500.25"
    assert pool["liquidity_usd"] == 1234567.89
    assert pool["block_number"] == 12345678

    health = store.fetch_provider_health()[0]
    assert health["provider_key"] == "dexscreener"
    assert health["status"] == "OK"
    assert health["consecutive_failures"] == 0
    assert _table_count(store, "arb_opportunities") == 0


def test_geckoterminal_style_fixture_ingests_through_dex_collector(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = {
        "observed_at_ms": "2026-05-23T12:34:56Z",
        "data": {
            "id": "base_0x4444444444444444444444444444444444444444",
            "type": "pool",
            "attributes": {
                "name": "VIRTUAL / WETH",
                "address": "0x4444444444444444444444444444444444444444",
                "base_token_price_usd": "1.23",
                "base_token_price_quote_token": "0.00037",
                "reserve_in_usd": "555000.5",
                "volume_usd": {"h24": "12000"},
            },
            "relationships": {
                "network": {"data": {"id": "base"}},
                "dex": {"data": {"id": "baseswap"}},
                "base_token": {"data": {"id": "base_0x5555555555555555555555555555555555555555"}},
                "quote_token": {"data": {"id": "base_0x6666666666666666666666666666666666666666"}},
            },
        },
        "included": [
            {
                "id": "base_0x5555555555555555555555555555555555555555",
                "type": "token",
                "attributes": {
                    "address": "0x5555555555555555555555555555555555555555",
                    "name": "Virtuals Protocol",
                    "symbol": "VIRTUAL",
                    "decimals": 18,
                },
            },
            {
                "id": "base_0x6666666666666666666666666666666666666666",
                "type": "token",
                "attributes": {
                    "address": "0x6666666666666666666666666666666666666666",
                    "name": "Wrapped Ether",
                    "symbol": "WETH",
                    "decimals": 18,
                },
            },
        ],
    }

    result = ingest_dex_fixture(
        store,
        payload,
        provider_key="geckoterminal_fixture",
        scope_key="base:virtual-weth",
    )

    assert result.status == STATUS_OK
    assert result.inserted_count == 2
    market = _table_rows(store, "arb_markets")[0]
    assert market["market_key"] == "BASE:BASESWAP:VIRTUAL-WETH:0x4444444444444444444444444444444444444444"
    assert market["chain_code"] == "BASE"
    assert market["quote_asset"] == "WETH"
    tick = _table_rows(store, "arb_market_ticks")[0]
    assert tick["price_usd"] == 1.23
    assert tick["raw_price"] == 0.00037
    pool = _table_rows(store, "arb_pool_snapshots")[0]
    assert pool["liquidity_usd"] == 555000.5


def test_dex_fixture_duplicate_payload_is_idempotent_for_observations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _dexscreener_payload()

    first = ingest_dex_fixture(store, payload, provider_key="dexscreener", scope_key="polygon:sol-usdc")
    second = ingest_dex_fixture(store, payload, provider_key="dexscreener", scope_key="polygon:sol-usdc")

    assert first.inserted_count == 2
    assert second.status == STATUS_OK
    assert second.cursor_before == str(DEX_OBSERVED_AT_MS)
    assert second.cursor_after == str(DEX_OBSERVED_AT_MS)
    assert second.inserted_count == 0
    assert _table_count(store, "arb_market_ticks") == 1
    assert _table_count(store, "arb_pool_snapshots") == 1


def test_dex_fixture_success_does_not_regress_collect_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope_key = "polygon:sol-usdc"
    newer_payload = _dexscreener_payload()
    newer_payload["pairs"][0]["observed_at_ms"] = DEX_OBSERVED_AT_MS + 1_000

    first = ingest_dex_fixture(store, newer_payload, provider_key="dexscreener", scope_key=scope_key)
    second = ingest_dex_fixture(store, _dexscreener_payload(), provider_key="dexscreener", scope_key=scope_key)

    assert first.cursor_after == str(DEX_OBSERVED_AT_MS + 1_000)
    assert second.status == STATUS_OK
    assert second.cursor_before == str(DEX_OBSERVED_AT_MS + 1_000)
    assert second.cursor_after == str(DEX_OBSERVED_AT_MS + 1_000)
    assert store.get_collect_cursor("dexscreener", scope_key) == str(DEX_OBSERVED_AT_MS + 1_000)


def test_malformed_dex_payload_deadletters_and_does_not_advance_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope_key = "polygon:sol-usdc"
    ingest_dex_fixture(store, _dexscreener_payload(), provider_key="dexscreener", scope_key=scope_key)
    bad_payload = _dexscreener_payload()
    del bad_payload["pairs"][0]["priceUsd"]

    result = ingest_dex_fixture(store, bad_payload, provider_key="dexscreener", scope_key=scope_key)

    assert result.status == STATUS_DEGRADED
    assert result.cursor_before == str(DEX_OBSERVED_AT_MS)
    assert result.cursor_after == str(DEX_OBSERVED_AT_MS)
    assert result.deadletter_count == 1
    assert store.get_collect_cursor("dexscreener", scope_key) == str(DEX_OBSERVED_AT_MS)
    assert _table_count(store, "arb_market_ticks") == 1
    assert _table_count(store, "arb_pool_snapshots") == 1

    health = store.fetch_provider_health()[0]
    assert health["status"] == "DEGRADED"
    assert health["consecutive_failures"] == 1
    assert health["error_code"] == "missing_price_usd"

    deadletter = store.fetch_dead_letters()[-1]
    assert deadletter["reason"] == "collect_failure"
    assert deadletter["error_code"] == "missing_price_usd"
    assert deadletter["payload"]["provider_key"] == "dexscreener"
    assert deadletter["payload"]["scope_key"] == scope_key
    assert deadletter["payload"]["cursor_before"] == str(DEX_OBSERVED_AT_MS)
    assert deadletter["payload"]["raw_payload"]["field_path"] == "pairs[0].priceUsd"


@pytest.mark.parametrize(
    ("provider_key", "payload", "expected_bid", "expected_ask"),
    [
        ("binance_public", _binance_orderbook_payload(), 70.1, 70.2),
        ("okx_public", _okx_orderbook_payload(), 70.11, 70.22),
        ("bybit_public", _bybit_orderbook_payload(), 70.12, 70.24),
    ],
)
def test_global_cex_orderbook_fixtures_ingest_market_ticks_and_snapshots(
    tmp_path: Path,
    provider_key: str,
    payload: dict[str, Any],
    expected_bid: float,
    expected_ask: float,
) -> None:
    store = _store(tmp_path)

    result = ingest_cex_orderbook_fixture(
        store,
        payload,
        provider_key=provider_key,
        scope_key="SOL-USDT",
    )

    assert result.status == STATUS_OK
    assert result.cursor_before == ""
    assert result.cursor_after == str(CEX_OBSERVED_AT_MS)
    assert result.inserted_count == 2
    assert store.get_collect_cursor(provider_key, "SOL-USDT") == str(CEX_OBSERVED_AT_MS)

    assets = {row["symbol"]: row for row in _table_rows(store, "arb_assets")}
    assert "SOL" in assets

    market = _table_rows(store, "arb_markets")[0]
    assert market["market_type"] == "CEX_ORDERBOOK"
    assert market["market_key"].endswith(":SOL-USDT")
    assert market["market_symbol"] == "SOL/USDT"
    assert market["quote_asset"] == "USDT"

    tick = _table_rows(store, "arb_market_ticks")[0]
    assert tick["source"] == provider_key
    assert tick["best_bid"] == expected_bid
    assert tick["best_ask"] == expected_ask
    assert tick["price_usd"] == pytest.approx((expected_bid + expected_ask) / 2)

    orderbook = _table_rows(store, "arb_orderbook_snapshots")[0]
    assert orderbook["best_bid"] == expected_bid
    assert orderbook["best_ask"] == expected_ask
    assert json.loads(orderbook["depth_json"])[0]["side"] == "bid"
    assert _table_count(store, "arb_opportunities") == 0

    health = store.fetch_provider_health()[0]
    assert health["provider_key"] == provider_key
    assert health["status"] == "OK"
    assert health["consecutive_failures"] == 0


@pytest.mark.parametrize(
    ("provider_key", "payload", "expected_market_key"),
    [
        (
            "upbit_public",
            {
                "market": "KRW-SOL",
                "timestamp": KRW_OBSERVED_AT_MS,
                "orderbook_units": [
                    {"bid_price": 115000, "bid_size": 30, "ask_price": 115100, "ask_size": 21},
                    {"bid_price": 114900, "bid_size": 12, "ask_price": 115200, "ask_size": 18},
                ],
            },
            "UPBIT:SOL-KRW",
        ),
        (
            "bithumb_public",
            {
                "status": "0000",
                "data": {
                    "order_currency": "SOL",
                    "payment_currency": "KRW",
                    "timestamp": str(KRW_OBSERVED_AT_MS),
                    "bids": [{"price": "115010", "quantity": "15"}],
                    "asks": [{"price": "115120", "quantity": "11"}],
                },
            },
            "BITHUMB:SOL-KRW",
        ),
    ],
)
def test_krw_orderbook_fixtures_store_krw_quote_asset(
    tmp_path: Path,
    provider_key: str,
    payload: dict[str, Any],
    expected_market_key: str,
) -> None:
    store = _store(tmp_path)

    result = ingest_cex_orderbook_fixture(
        store,
        payload,
        provider_key=provider_key,
        scope_key="KRW-SOL",
    )

    assert result.status == STATUS_OK
    assert result.cursor_after == str(KRW_OBSERVED_AT_MS)

    venue = _table_rows(store, "arb_venues")[0]
    assert venue["venue_type"] == "CEX"

    market = _table_rows(store, "arb_markets")[0]
    assert market["market_key"] == expected_market_key
    assert market["market_symbol"] == "SOL/KRW"
    assert market["quote_asset"] == "KRW"

    tick = _table_rows(store, "arb_market_ticks")[0]
    assert tick["price_krw"] == pytest.approx((tick["best_bid"] + tick["best_ask"]) / 2)
    assert _table_count(store, "arb_orderbook_snapshots") == 1
    assert _table_count(store, "arb_opportunities") == 0


def test_cex_orderbook_duplicate_payload_is_idempotent_for_observations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _binance_orderbook_payload()

    first = ingest_cex_orderbook_fixture(store, payload, provider_key="binance_public", scope_key="SOL-USDT")
    second = ingest_cex_orderbook_fixture(store, payload, provider_key="binance_public", scope_key="SOL-USDT")

    assert first.inserted_count == 2
    assert second.status == STATUS_OK
    assert second.cursor_before == str(CEX_OBSERVED_AT_MS)
    assert second.cursor_after == str(CEX_OBSERVED_AT_MS)
    assert second.inserted_count == 0
    assert _table_count(store, "arb_market_ticks") == 1
    assert _table_count(store, "arb_orderbook_snapshots") == 1


def test_cex_orderbook_success_does_not_regress_collect_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope_key = "SOL-USDT"
    newer_payload = _binance_orderbook_payload()
    newer_payload["observed_at_ms"] = CEX_OBSERVED_AT_MS + 1_000

    first = ingest_cex_orderbook_fixture(store, newer_payload, provider_key="binance_public", scope_key=scope_key)
    second = ingest_cex_orderbook_fixture(store, _binance_orderbook_payload(), provider_key="binance_public", scope_key=scope_key)

    assert first.cursor_after == str(CEX_OBSERVED_AT_MS + 1_000)
    assert second.status == STATUS_OK
    assert second.cursor_before == str(CEX_OBSERVED_AT_MS + 1_000)
    assert second.cursor_after == str(CEX_OBSERVED_AT_MS + 1_000)
    assert store.get_collect_cursor("binance_public", scope_key) == str(CEX_OBSERVED_AT_MS + 1_000)


def test_malformed_cex_orderbook_deadletters_and_does_not_advance_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope_key = "SOL-USDT"
    ingest_cex_orderbook_fixture(store, _binance_orderbook_payload(), provider_key="binance_public", scope_key=scope_key)
    bad_payload = _binance_orderbook_payload()
    bad_payload["asks"] = []

    result = ingest_cex_orderbook_fixture(store, bad_payload, provider_key="binance_public", scope_key=scope_key)

    assert result.status == STATUS_DEGRADED
    assert result.cursor_before == str(CEX_OBSERVED_AT_MS)
    assert result.cursor_after == str(CEX_OBSERVED_AT_MS)
    assert result.deadletter_count == 1
    assert store.get_collect_cursor("binance_public", scope_key) == str(CEX_OBSERVED_AT_MS)
    assert _table_count(store, "arb_market_ticks") == 1
    assert _table_count(store, "arb_orderbook_snapshots") == 1

    health = store.fetch_provider_health()[0]
    assert health["status"] == "DEGRADED"
    assert health["consecutive_failures"] == 1
    assert health["error_code"] == "missing_best_ask"

    deadletter = store.fetch_dead_letters()[-1]
    assert deadletter["reason"] == "collect_failure"
    assert deadletter["error_code"] == "missing_best_ask"
    assert deadletter["payload"]["provider_key"] == "binance_public"
    assert deadletter["payload"]["scope_key"] == scope_key
    assert deadletter["payload"]["cursor_before"] == str(CEX_OBSERVED_AT_MS)
    assert deadletter["payload"]["raw_payload"]["field_path"] == "payload.asks"


def test_stale_cex_orderbook_deadletters_without_cursor_advance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stale_payload = _binance_orderbook_payload()
    stale_payload["stale"] = True

    result = ingest_cex_orderbook_fixture(
        store,
        stale_payload,
        provider_key="binance_public",
        scope_key="SOL-USDT",
    )

    assert result.status == STATUS_DEGRADED
    assert result.cursor_before == ""
    assert result.cursor_after == ""
    assert result.deadletter_count == 1
    assert store.get_collect_cursor("binance_public", "SOL-USDT") == ""
    assert store.fetch_dead_letters()[-1]["error_code"] == "stale_orderbook_payload"


def test_fx_fixture_ingests_rate_observation(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = ingest_fx_fixture(
        store,
        _fx_rate_payload(),
        provider_key="fx_implied",
        scope_key="USDT-KRW",
    )

    assert result.status == STATUS_OK
    assert result.cursor_before == ""
    assert result.cursor_after == str(FX_OBSERVED_AT_MS)
    assert result.inserted_count == 1
    assert store.get_collect_cursor("fx_implied", "USDT-KRW") == str(FX_OBSERVED_AT_MS)

    fx_rate = _table_rows(store, "arb_fx_rates")[0]
    assert fx_rate["pair"] == "USDT/KRW"
    assert fx_rate["source"] == "upbit_usdt_krw"
    assert fx_rate["observed_at_ms"] == FX_OBSERVED_AT_MS
    assert fx_rate["rate"] == 1390.25
    assert fx_rate["stale"] == 0
    assert json.loads(fx_rate["payload_json"])["evidence"] == {"ask": "1390.5", "bid": "1390.0"}

    health = store.fetch_provider_health()[0]
    assert health["provider_key"] == "fx_implied"
    assert health["status"] == "OK"
    assert health["consecutive_failures"] == 0
    assert _table_count(store, "arb_opportunities") == 0


def test_stale_fx_fixture_is_stored_with_stale_flag(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = ingest_fx_fixture(
        store,
        _fx_rate_payload(stale=True),
        provider_key="fx_implied",
        scope_key="USDT-KRW",
    )

    assert result.status == STATUS_OK
    assert result.inserted_count == 1
    fx_rate = _table_rows(store, "arb_fx_rates")[0]
    assert fx_rate["stale"] == 1
    assert store.fetch_provider_health()[0]["status"] == "OK"


def test_fx_fixture_success_does_not_regress_collect_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    newer_payload = _fx_rate_payload()
    newer_payload["observed_at_ms"] = FX_OBSERVED_AT_MS + 1_000

    first = ingest_fx_fixture(store, newer_payload, provider_key="fx_implied", scope_key="USDT-KRW")
    second = ingest_fx_fixture(store, _fx_rate_payload(), provider_key="fx_implied", scope_key="USDT-KRW")

    assert first.cursor_after == str(FX_OBSERVED_AT_MS + 1_000)
    assert second.status == STATUS_OK
    assert second.cursor_before == str(FX_OBSERVED_AT_MS + 1_000)
    assert second.cursor_after == str(FX_OBSERVED_AT_MS + 1_000)
    assert store.get_collect_cursor("fx_implied", "USDT-KRW") == str(FX_OBSERVED_AT_MS + 1_000)


def test_malformed_fx_payload_deadletters_and_degrades_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ingest_fx_fixture(store, _fx_rate_payload(), provider_key="fx_implied", scope_key="USDT-KRW")
    bad_payload = _fx_rate_payload()
    del bad_payload["rate"]

    result = ingest_fx_fixture(
        store,
        bad_payload,
        provider_key="fx_implied",
        scope_key="USDT-KRW",
    )

    assert result.status == STATUS_DEGRADED
    assert result.cursor_before == str(FX_OBSERVED_AT_MS)
    assert result.cursor_after == str(FX_OBSERVED_AT_MS)
    assert result.deadletter_count == 1
    assert store.get_collect_cursor("fx_implied", "USDT-KRW") == str(FX_OBSERVED_AT_MS)
    assert _table_count(store, "arb_fx_rates") == 1

    health = store.fetch_provider_health()[0]
    assert health["status"] == "DEGRADED"
    assert health["consecutive_failures"] == 1
    assert health["error_code"] == "missing_fx_rate"

    deadletter = store.fetch_dead_letters()[-1]
    assert deadletter["reason"] == "collect_failure"
    assert deadletter["error_code"] == "missing_fx_rate"
    assert deadletter["payload"]["cursor_before"] == str(FX_OBSERVED_AT_MS)
    assert deadletter["payload"]["raw_payload"]["field_path"] == "payload.rate"


def test_rpc_freshness_success_updates_health_and_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = ingest_rpc_freshness_fixture(
        store,
        _rpc_success_payload(),
        provider_key="alchemy:polygon",
        scope_key="polygon:latest_block",
    )

    assert result.status == STATUS_OK
    assert result.cursor_before == ""
    assert result.cursor_after == str(RPC_LATEST_BLOCK)
    assert result.inserted_count == 0
    assert store.get_collect_cursor("alchemy:polygon", "polygon:latest_block") == str(RPC_LATEST_BLOCK)

    health = store.fetch_provider_health()[0]
    assert health["provider_key"] == "alchemy:polygon"
    assert health["status"] == "OK"
    assert health["consecutive_failures"] == 0
    assert _table_count(store, "arb_opportunities") == 0


def test_rpc_freshness_success_does_not_regress_collect_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider_key = "alchemy:polygon"
    scope_key = "polygon:latest_block"
    newer_payload = _rpc_success_payload()
    newer_payload["result"]["number"] = hex(RPC_LATEST_BLOCK + 10)

    first = ingest_rpc_freshness_fixture(store, newer_payload, provider_key=provider_key, scope_key=scope_key)
    second = ingest_rpc_freshness_fixture(store, _rpc_success_payload(), provider_key=provider_key, scope_key=scope_key)

    assert first.cursor_after == str(RPC_LATEST_BLOCK + 10)
    assert second.status == STATUS_OK
    assert second.cursor_before == str(RPC_LATEST_BLOCK + 10)
    assert second.cursor_after == str(RPC_LATEST_BLOCK + 10)
    assert store.get_collect_cursor(provider_key, scope_key) == str(RPC_LATEST_BLOCK + 10)


@pytest.mark.parametrize(
    ("bad_payload", "expected_error_code"),
    [
        ({"jsonrpc": "2.0", "result": None, "observed_at_ms": RPC_OBSERVED_AT_MS}, "rpc_result_null"),
        ({"timeout": True, "observed_at_ms": RPC_OBSERVED_AT_MS}, "rpc_timeout"),
        ({"jsonrpc": "2.0", "result": {"hash": "0xabc123"}, "observed_at_ms": RPC_OBSERVED_AT_MS}, "malformed_rpc_payload"),
        (
            {
                "partial_failure": True,
                "result": {"number": RPC_LATEST_BLOCK + 1},
                "observed_at_ms": RPC_OBSERVED_AT_MS,
            },
            "rpc_partial_failure",
        ),
    ],
)
def test_rpc_failure_payloads_deadletter_exact_error_code_without_cursor_advance(
    tmp_path: Path,
    bad_payload: dict[str, Any],
    expected_error_code: str,
) -> None:
    store = _store(tmp_path)
    provider_key = "alchemy:polygon"
    scope_key = "polygon:latest_block"
    ingest_rpc_freshness_fixture(store, _rpc_success_payload(), provider_key=provider_key, scope_key=scope_key)

    result = ingest_rpc_freshness_fixture(
        store,
        bad_payload,
        provider_key=provider_key,
        scope_key=scope_key,
    )

    assert result.status == STATUS_DEGRADED
    assert result.cursor_before == str(RPC_LATEST_BLOCK)
    assert result.cursor_after == str(RPC_LATEST_BLOCK)
    assert result.deadletter_count == 1
    assert store.get_collect_cursor(provider_key, scope_key) == str(RPC_LATEST_BLOCK)

    health = store.fetch_provider_health()[0]
    assert health["status"] == "DEGRADED"
    assert health["consecutive_failures"] == 1
    assert health["error_code"] == expected_error_code

    deadletter = store.fetch_dead_letters()[-1]
    assert deadletter["reason"] == "collect_failure"
    assert deadletter["error_code"] == expected_error_code
    assert deadletter["payload"]["provider_key"] == provider_key
    assert deadletter["payload"]["scope_key"] == scope_key
    assert deadletter["payload"]["cursor_before"] == str(RPC_LATEST_BLOCK)
    assert deadletter["payload"]["raw_payload"]["error_code"] == expected_error_code
