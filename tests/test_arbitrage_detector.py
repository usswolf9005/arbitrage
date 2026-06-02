from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arbitrage.detector import ArbitrageDetector, detect_dex_drawdowns
from arbitrage.engine import ArbitrageEngine
from arbitrage.normalizer import (
    IDENTITY_AMBIGUOUS,
    IDENTITY_UNKNOWN,
    IDENTITY_VERIFIED,
    IdentityNormalizer,
)
from arbitrage.store import ArbitrageStore


POLYGON_SOL_CA = "0x1111111111111111111111111111111111111111"
POLYGON_FAKE_CA = "0xffffffffffffffffffffffffffffffffffffffff"
POLYGON_POOL_CA = "0x2222222222222222222222222222222222222222"
BASE_SOL_CA = "0x3333333333333333333333333333333333333333"
BASE_POOL_CA = "0x4444444444444444444444444444444444444444"


def _store(tmp_path: Path) -> ArbitrageStore:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    return store


def _table_rows(store: ArbitrageStore, table: str) -> list[dict[str, Any]]:
    with store.conn() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]


def _seed_dex_market(
    store: ArbitrageStore,
    *,
    asset_id: int,
    token_contract_address: str | None = POLYGON_SOL_CA,
    venue_code: str = "QUICKSWAP",
    venue_name: str = "QuickSwap",
    chain_id: str = "137",
    chain_code: str = "POLYGON",
    pool_address: str = POLYGON_POOL_CA,
    market_symbol: str = "SOL/USDC",
    quote_asset: str = "USDC",
) -> int:
    venue_id = store.ensure_venue(venue_code, "DEX", venue_name)
    payload = {"chain_id": chain_id}
    if token_contract_address is not None:
        payload["token_contract_address"] = token_contract_address
    return store.ensure_market(
        market_key=f"{chain_code}:{venue_code}:{market_symbol}:{pool_address}",
        asset_id=asset_id,
        venue_id=venue_id,
        market_type="DEX_POOL",
        chain_code=chain_code,
        pool_address=pool_address,
        market_symbol=market_symbol,
        quote_asset=quote_asset,
        payload=payload,
    )


def _seed_cex_market(
    store: ArbitrageStore,
    *,
    asset_id: int,
    venue_code: str,
    market_symbol: str,
    market_key: str,
    quote_asset: str = "KRW",
    deposit_network: str = "",
) -> int:
    venue_id = store.ensure_venue(venue_code, "CEX", venue_code.title())
    return store.ensure_market(
        market_key=market_key,
        asset_id=asset_id,
        venue_id=venue_id,
        market_type="CEX_ORDERBOOK",
        chain_code=quote_asset,
        market_symbol=market_symbol,
        quote_asset=quote_asset,
        deposit_network=deposit_network,
    )


def _seed_pool_snapshot(
    store: ArbitrageStore,
    *,
    market_id: int,
    observed_at_ms: int,
    token_contract_address: str = POLYGON_SOL_CA,
    source: str = "dexscreener",
    reserve0_raw: str = "1000",
    reserve1_raw: str = "90000",
    liquidity_usd: float = 1_000_000.0,
    block_number: int = 12345678,
) -> None:
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
                source,
                int(observed_at_ms),
                reserve0_raw,
                reserve1_raw,
                liquidity_usd,
                block_number,
                json.dumps({"baseToken": {"address": token_contract_address}}, sort_keys=True),
            ),
        )


def _record_fx_rate(
    store: ArbitrageStore,
    *,
    pair: str,
    rate: float,
    observed_at_ms: int,
    source: str = "upbit_public",
    stale: bool = False,
) -> None:
    with store.conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO arb_fx_rates(pair, source, observed_at_ms, rate, stale, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                pair,
                source,
                int(observed_at_ms),
                float(rate),
                1 if stale else 0,
                json.dumps({"implied": True}, sort_keys=True),
            ),
        )


def test_normalizer_verifies_dex_identity_by_chain_id_and_contract_address(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    token_id = store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    market_id = _seed_dex_market(store, asset_id=asset_id)

    result = IdentityNormalizer(store).normalize_market(market_id)

    assert result.identity_status == IDENTITY_VERIFIED
    assert result.executable is True
    assert result.asset_id == asset_id
    assert result.market_id == market_id
    assert result.token_id == token_id
    assert result.warning_reasons == ()
    assert result.to_dict()["contract_address"] == POLYGON_SOL_CA
    assert set(result.to_dict()).issuperset(
        {"asset_id", "market_id", "identity_status", "warning_reasons", "executable"}
    )
    assert _table_rows(store, "arb_dead_letters") == []


def test_normalizer_rejects_symbol_only_dex_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    market_id = _seed_dex_market(store, asset_id=asset_id, token_contract_address=None)

    result = IdentityNormalizer(store).normalize_market(market_id)

    assert result.identity_status == IDENTITY_UNKNOWN
    assert result.executable is False
    assert result.asset_id == asset_id
    assert result.warning_reasons == ("missing_token_contract_address",)
    dead_letters = _table_rows(store, "arb_dead_letters")
    assert [row["error_code"] for row in dead_letters] == ["missing_token_contract_address"]


def test_normalizer_rejects_dex_market_when_contract_asset_does_not_match_market_asset(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    sol_asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    fake_asset_id = store.ensure_asset(symbol="FAKE", name="Fake Token")
    store.ensure_token(
        asset_id=sol_asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    store.ensure_token(
        asset_id=fake_asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_FAKE_CA,
        decimals=18,
    )
    market_id = _seed_dex_market(store, asset_id=sol_asset_id, token_contract_address=POLYGON_FAKE_CA)

    result = IdentityNormalizer(store).normalize_market(market_id)

    assert result.identity_status == IDENTITY_UNKNOWN
    assert result.executable is False
    assert result.asset_id == sol_asset_id
    assert result.warning_reasons == ("asset_identity_mismatch",)
    dead_letter = _table_rows(store, "arb_dead_letters")[0]
    assert dead_letter["error_code"] == "asset_identity_mismatch"


def test_normalizer_verifies_cex_identity_by_venue_and_market_symbol(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    upbit_market_id = _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="UPBIT",
        market_symbol="SOL/KRW",
        market_key="UPBIT:SOL-KRW",
    )
    binance_market_id = _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="BINANCE",
        market_symbol="SOL/KRW",
        market_key="BINANCE:SOL-KRW",
    )

    normalizer = IdentityNormalizer(store)
    upbit_result = normalizer.normalize_cex_market("UPBIT", "SOL/KRW")
    binance_result = normalizer.normalize_cex_market("BINANCE", "SOL/KRW")

    assert upbit_result.identity_status == IDENTITY_VERIFIED
    assert upbit_result.executable is True
    assert upbit_result.market_id == upbit_market_id
    assert upbit_result.venue_code == "UPBIT"
    assert binance_result.identity_status == IDENTITY_VERIFIED
    assert binance_result.market_id == binance_market_id
    assert binance_result.venue_code == "BINANCE"
    assert _table_rows(store, "arb_dead_letters") == []


def test_normalizer_deadletters_unknown_and_ambiguous_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    unknown_market_id = _seed_dex_market(
        store,
        asset_id=asset_id,
        token_contract_address="0x9999999999999999999999999999999999999999",
    )
    _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="UPBIT",
        market_symbol="SOL/KRW",
        market_key="UPBIT:SOL-KRW:primary",
    )
    _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="UPBIT",
        market_symbol="SOL/KRW",
        market_key="UPBIT:SOL-KRW:duplicate-provider",
    )

    normalizer = IdentityNormalizer(store)
    unknown_result = normalizer.normalize_market(unknown_market_id)
    ambiguous_result = normalizer.normalize_cex_market("UPBIT", "SOL/KRW")

    assert unknown_result.identity_status == IDENTITY_UNKNOWN
    assert unknown_result.executable is False
    assert unknown_result.warning_reasons == ("unknown_onchain_token_identity",)
    assert ambiguous_result.identity_status == IDENTITY_AMBIGUOUS
    assert ambiguous_result.executable is False
    assert ambiguous_result.warning_reasons == ("ambiguous_cex_market_identity",)

    dead_letters = _table_rows(store, "arb_dead_letters")
    assert [row["error_code"] for row in dead_letters] == [
        "unknown_onchain_token_identity",
        "ambiguous_cex_market_identity",
    ]


def test_dex_drawdown_detector_creates_opportunity_and_same_dex_route(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    market_id = _seed_dex_market(store, asset_id=asset_id)
    baseline_ms = 1_000
    current_ms = 2_000
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=baseline_ms,
        price_usd=100.0,
        liquidity_usd=1_000_000,
    )
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=current_ms,
        price_usd=90.0,
        liquidity_usd=950_000,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    _seed_pool_snapshot(store, market_id=market_id, observed_at_ms=current_ms)

    result = ArbitrageDetector(store, drawdown_threshold_bps=500).detect_dex_drawdowns(now_ms=current_ms)

    assert result.to_dict() == {
        "opportunities_upserted": 1,
        "routes_upserted": 1,
        "blocked_identities": 0,
        "skipped": 0,
    }
    opportunities = _table_rows(store, "arb_opportunities")
    routes = _table_rows(store, "arb_routes")
    assert len(opportunities) == 1
    assert len(routes) == 1

    opportunity = opportunities[0]
    route = routes[0]
    assert opportunity["anomaly_type"] == "dex_drawdown"
    assert opportunity["lifecycle_status"] == "DETECTED"
    assert opportunity["buy_market_id"] == market_id
    assert opportunity["sell_market_id"] == market_id
    assert opportunity["spread_bps"] == 1000.0
    assert opportunity["selected_route_id"] == route["id"]
    opportunity_payload = json.loads(opportunity["payload_json"])
    assert opportunity_payload["identity"]["executable"] is True
    assert opportunity_payload["pool_snapshot"]["block_number"] == 12345678
    assert opportunity_payload["candidate_only"] is True

    assert route["route_type"] == "same_dex_sell"
    assert route["buy_market_id"] == market_id
    assert route["sell_market_id"] == market_id
    assert route["route_status"] == "WAIT"
    assert route["edge_worst_verified"] == 0
    route_payload = json.loads(route["payload_json"])
    assert route_payload["edge_worst_verified"] is False
    assert json.loads(route["warning_reasons_json"]) == ["candidate_only", "edge_worst_unverified"]


def test_dex_drawdown_detector_is_idempotent_for_duplicate_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    market_id = _seed_dex_market(store, asset_id=asset_id)
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=1_000,
        price_usd=100.0,
    )
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=2_000,
        price_usd=85.0,
    )

    first = detect_dex_drawdowns(store, drawdown_threshold_bps=500, now_ms=2_000)
    second = detect_dex_drawdowns(store, drawdown_threshold_bps=500, now_ms=2_000)

    assert first.opportunities_upserted == 1
    assert second.opportunities_upserted == 1
    assert len(_table_rows(store, "arb_opportunities")) == 1
    assert len(_table_rows(store, "arb_routes")) == 1


def test_dex_drawdown_detector_blocks_unknown_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    market_id = _seed_dex_market(store, asset_id=asset_id, token_contract_address=POLYGON_FAKE_CA)
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=1_000,
        price_usd=100.0,
    )
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=2_000,
        price_usd=80.0,
        payload={"baseToken": {"address": POLYGON_FAKE_CA}},
    )

    result = ArbitrageDetector(store, drawdown_threshold_bps=500).detect_dex_drawdowns(now_ms=2_000)

    assert result.blocked_identities == 1
    assert _table_rows(store, "arb_opportunities") == []
    assert _table_rows(store, "arb_routes") == []
    dead_letters = _table_rows(store, "arb_dead_letters")
    assert [row["error_code"] for row in dead_letters] == ["unknown_onchain_token_identity"]


def test_dex_cex_spread_detector_creates_opportunity_and_direct_cex_route(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    dex_market_id = _seed_dex_market(store, asset_id=asset_id)
    cex_market_id = _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="BINANCE",
        market_symbol="SOL/USDT",
        market_key="BINANCE:SOL-USDT",
        quote_asset="USDT",
        deposit_network="POLYGON",
    )
    observed_ms = 2_000
    store.record_market_tick(
        market_id=dex_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=100.0,
        liquidity_usd=1_000_000,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    store.record_market_tick(
        market_id=cex_market_id,
        source="binance_orderbook",
        observed_at_ms=observed_ms,
        price_usd=110.5,
        best_bid=110.0,
        best_ask=111.0,
    )
    _seed_pool_snapshot(store, market_id=dex_market_id, observed_at_ms=observed_ms)

    result = ArbitrageDetector(store).detect_dex_cex_spreads(now_ms=observed_ms)

    assert result.to_dict() == {
        "opportunities_upserted": 1,
        "routes_upserted": 1,
        "blocked_identities": 0,
        "skipped": 0,
    }
    opportunity = _table_rows(store, "arb_opportunities")[0]
    route = _table_rows(store, "arb_routes")[0]
    payload = json.loads(opportunity["payload_json"])

    assert opportunity["anomaly_type"] == "dex_cex_spread"
    assert opportunity["lifecycle_status"] == "DETECTED"
    assert opportunity["buy_market_id"] == dex_market_id
    assert opportunity["sell_market_id"] == cex_market_id
    assert opportunity["spread_bps"] == 1000.0
    assert opportunity["edge_worst_bps"] == 0.0
    assert opportunity["selected_route_id"] == route["id"]
    assert payload["buy_venue"] == "QUICKSWAP"
    assert payload["sell_venue"] == "BINANCE"
    assert payload["anomaly_type"] == "dex_cex_spread"
    assert payload["detection_reason"] == "dex_cex_spread_positive_spread"
    assert payload["chain"] == "POLYGON"
    assert payload["token_ca"] == POLYGON_SOL_CA
    assert payload["pool_ca"] == POLYGON_POOL_CA
    assert payload["cex_market"] == "SOL/USDT"
    assert payload["deposit_network"] == "POLYGON"
    assert payload["edge_worst_bps"] == 0.0
    assert payload["source_freshness"]["dex_tick"]["status"] == "fresh"
    assert payload["source_freshness"]["cex_orderbook"]["status"] == "fresh"
    assert payload["source_freshness"]["rpc_freshness"]["status"] == "missing"
    assert payload["status"] == "DETECTED"
    assert payload["selected_route"]["route_type"] == "direct_cex_sell"
    assert payload["edge_worst_verified"] is False

    assert route["route_type"] == "direct_cex_sell"
    assert route["route_status"] == "WAIT"
    assert route["safety_status"] == "WARN"
    assert route["edge_worst_verified"] == 0
    assert json.loads(route["warning_reasons_json"]) == ["candidate_only", "edge_worst_unverified"]


def test_dex_krw_spread_detector_prefers_non_stale_usdt_krw_implied_fx(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    dex_market_id = _seed_dex_market(store, asset_id=asset_id)
    cex_market_id = _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="UPBIT",
        market_symbol="SOL/KRW",
        market_key="UPBIT:SOL-KRW",
        quote_asset="KRW",
        deposit_network="POLYGON",
    )
    observed_ms = 2_000
    _record_fx_rate(store, pair="USDT/KRW", rate=900.0, observed_at_ms=1_900, stale=True)
    _record_fx_rate(store, pair="USDT/KRW", rate=1000.0, observed_at_ms=1_950, source="bithumb_public")
    store.record_market_tick(
        market_id=dex_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=100.0,
        price_krw=125_000.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    store.record_market_tick(
        market_id=cex_market_id,
        source="upbit_orderbook",
        observed_at_ms=observed_ms,
        price_krw=110_500.0,
        best_bid=110_000.0,
        best_ask=111_000.0,
    )

    result = ArbitrageDetector(store).detect_dex_krw_spreads(now_ms=observed_ms)

    assert result.opportunities_upserted == 1
    opportunity = _table_rows(store, "arb_opportunities")[0]
    payload = json.loads(opportunity["payload_json"])
    assert opportunity["anomaly_type"] == "dex_krw_spread"
    assert opportunity["spread_bps"] == 1000.0
    assert payload["buy"]["price"] == 100_000.0
    assert payload["buy"]["price_source"] == "usdt_krw_implied"
    assert payload["fx_rate"]["source"] == "bithumb_public"
    assert payload["fx_rate"]["effective_rate"] == 1000.0


def test_spread_detector_is_idempotent_for_duplicate_provider_observations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    dex_market_id = _seed_dex_market(store, asset_id=asset_id)
    cex_market_id = _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="BINANCE",
        market_symbol="SOL/USDT",
        market_key="BINANCE:SOL-USDT",
        quote_asset="USDT",
        deposit_network="POLYGON",
    )
    observed_ms = 2_000
    for source in ("dexscreener", "geckoterminal"):
        store.record_market_tick(
            market_id=dex_market_id,
            source=source,
            observed_at_ms=observed_ms,
            price_usd=100.0,
            payload={"baseToken": {"address": POLYGON_SOL_CA}},
        )
    for source in ("binance_rest", "binance_ws"):
        store.record_market_tick(
            market_id=cex_market_id,
            source=source,
            observed_at_ms=observed_ms,
            price_usd=110.5,
            best_bid=110.0,
            best_ask=111.0,
        )

    first = ArbitrageDetector(store).detect_dex_cex_spreads(now_ms=observed_ms)
    second = ArbitrageDetector(store).detect_dex_cex_spreads(now_ms=observed_ms)

    assert first.opportunities_upserted == 1
    assert second.opportunities_upserted == 1
    assert len(_table_rows(store, "arb_opportunities")) == 1
    assert len(_table_rows(store, "arb_routes")) == 1
    assert _table_rows(store, "arb_alerts") == []


def test_spread_detector_marks_unknown_cex_deposit_network_as_route_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    dex_market_id = _seed_dex_market(store, asset_id=asset_id)
    cex_market_id = _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="BINANCE",
        market_symbol="SOL/USDT",
        market_key="BINANCE:SOL-USDT",
        quote_asset="USDT",
        deposit_network="",
    )
    observed_ms = 2_000
    store.record_market_tick(
        market_id=dex_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=100.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    store.record_market_tick(
        market_id=cex_market_id,
        source="binance_orderbook",
        observed_at_ms=observed_ms,
        price_usd=110.5,
        best_bid=110.0,
        best_ask=111.0,
    )

    result = ArbitrageDetector(store).detect_dex_cex_spreads(now_ms=observed_ms)

    assert result.opportunities_upserted == 1
    route = _table_rows(store, "arb_routes")[0]
    assert route["safety_status"] == "WARN"
    assert route["route_status"] == "WAIT"
    assert "unknown_cex_deposit_network" in json.loads(route["warning_reasons_json"])
    route_payload = json.loads(route["payload_json"])
    assert route_payload["edge_worst_verified"] is False
    assert "unknown_cex_deposit_network" in route_payload["warning_reasons"]


def test_spread_detector_blocks_unknown_dex_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    dex_market_id = _seed_dex_market(store, asset_id=asset_id, token_contract_address=POLYGON_FAKE_CA)
    cex_market_id = _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="BINANCE",
        market_symbol="SOL/USDT",
        market_key="BINANCE:SOL-USDT",
        quote_asset="USDT",
        deposit_network="POLYGON",
    )
    observed_ms = 2_000
    store.record_market_tick(
        market_id=dex_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=100.0,
        payload={"baseToken": {"address": POLYGON_FAKE_CA}},
    )
    store.record_market_tick(
        market_id=cex_market_id,
        source="binance_orderbook",
        observed_at_ms=observed_ms,
        price_usd=110.5,
        best_bid=110.0,
        best_ask=111.0,
    )

    result = ArbitrageDetector(store).detect_dex_cex_spreads(now_ms=observed_ms)

    assert result.blocked_identities == 1
    assert _table_rows(store, "arb_opportunities") == []
    assert _table_rows(store, "arb_routes") == []
    assert [row["error_code"] for row in _table_rows(store, "arb_dead_letters")] == [
        "unknown_onchain_token_identity"
    ]


def test_cross_chain_spread_detector_creates_bridge_dex_candidate_for_verified_bridge_group(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    bridge_group = "sol-wormhole"
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
        bridge_group=bridge_group,
    )
    store.ensure_token(
        asset_id=asset_id,
        chain_id="8453",
        chain_code="BASE",
        contract_address=BASE_SOL_CA,
        decimals=18,
        bridge_group=bridge_group,
    )
    polygon_market_id = _seed_dex_market(store, asset_id=asset_id)
    base_market_id = _seed_dex_market(
        store,
        asset_id=asset_id,
        token_contract_address=BASE_SOL_CA,
        venue_code="BASESWAP",
        venue_name="BaseSwap",
        chain_id="8453",
        chain_code="BASE",
        pool_address=BASE_POOL_CA,
    )
    observed_ms = 2_000
    store.record_market_tick(
        market_id=polygon_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=100.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    store.record_market_tick(
        market_id=base_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=112.0,
        payload={"baseToken": {"address": BASE_SOL_CA}},
    )
    _seed_pool_snapshot(store, market_id=polygon_market_id, observed_at_ms=observed_ms)
    _seed_pool_snapshot(
        store,
        market_id=base_market_id,
        observed_at_ms=observed_ms,
        token_contract_address=BASE_SOL_CA,
    )

    detector = ArbitrageDetector(store)
    first = detector.detect_cross_chain_spreads(now_ms=observed_ms)
    second = detector.detect_cross_chain_spreads(now_ms=observed_ms)

    assert first.to_dict() == {
        "opportunities_upserted": 1,
        "routes_upserted": 1,
        "blocked_identities": 0,
        "skipped": 0,
    }
    assert second.opportunities_upserted == 1
    opportunities = _table_rows(store, "arb_opportunities")
    routes = _table_rows(store, "arb_routes")
    assert len(opportunities) == 1
    assert len(routes) == 1

    opportunity = opportunities[0]
    route = routes[0]
    payload = json.loads(opportunity["payload_json"])
    assert opportunity["anomaly_type"] == "cross_chain_spread"
    assert opportunity["buy_market_id"] == polygon_market_id
    assert opportunity["sell_market_id"] == base_market_id
    assert opportunity["spread_bps"] == 1200.0
    assert payload["buy_venue"] == "QUICKSWAP"
    assert payload["sell_venue"] == "BASESWAP"
    assert payload["buy_chain"] == "POLYGON"
    assert payload["sell_chain"] == "BASE"
    assert payload["token_ca"] == POLYGON_SOL_CA
    assert payload["sell_token_ca"] == BASE_SOL_CA
    assert payload["bridge_group"] == bridge_group
    assert payload["identity"]["verification"]["reason"] == "matching_bridge_group"
    assert payload["edge_worst_verified"] is False
    assert payload["bridge_quote_evaluated"] is False

    assert route["route_type"] == "bridge_dex_sell"
    assert route["route_status"] == "WAIT"
    assert route["safety_status"] == "WARN"
    assert route["edge_worst_verified"] == 0
    assert json.loads(route["warning_reasons_json"]) == [
        "candidate_only",
        "edge_worst_unverified",
        "bridge_quote_not_evaluated",
    ]

    snapshot = ArbitrageEngine(store).snapshot(selected_opportunity_id=opportunity["id"])
    card = snapshot["opportunities"][0]
    assert card["buy"]["venue"] == "QUICKSWAP"
    assert card["buy"]["chain"] == "POLYGON"
    assert card["buy"]["token_ca"] == POLYGON_SOL_CA
    assert card["buy"]["pool_ca"] == POLYGON_POOL_CA
    assert card["sell"]["venue"] == "BASESWAP"
    assert card["sell"]["chain"] == "BASE"
    assert card["sell"]["token_ca"] == BASE_SOL_CA
    assert card["sell"]["pool_ca"] == BASE_POOL_CA
    route_node = [node for node in snapshot["flow_nodes"] if node["id"] == "bridgeDexSell"][0]
    route_edge = [edge for edge in snapshot["flow_edges"] if edge["id"] == "buy-bridge-dex"][0]
    assert route_node["route_id"] == route["id"]
    assert route_edge["route_id"] == route["id"]
    assert route_node["status"] == "WAIT"
    assert route_node["duration_ms"] is None
    assert route_edge["status"] == "WAIT"
    assert route_edge["duration_ms"] is None


def test_cross_chain_spread_detector_rejects_symbol_only_cross_chain_match(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    store.ensure_token(
        asset_id=asset_id,
        chain_id="8453",
        chain_code="BASE",
        contract_address=BASE_SOL_CA,
        decimals=18,
    )
    polygon_market_id = _seed_dex_market(store, asset_id=asset_id)
    base_market_id = _seed_dex_market(
        store,
        asset_id=asset_id,
        token_contract_address=BASE_SOL_CA,
        venue_code="BASESWAP",
        venue_name="BaseSwap",
        chain_id="8453",
        chain_code="BASE",
        pool_address=BASE_POOL_CA,
    )
    observed_ms = 2_000
    store.record_market_tick(
        market_id=polygon_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=100.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    store.record_market_tick(
        market_id=base_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=112.0,
        payload={"baseToken": {"address": BASE_SOL_CA}},
    )

    result = ArbitrageDetector(store).detect_cross_chain_spreads(now_ms=observed_ms)

    assert result.opportunities_upserted == 0
    assert result.routes_upserted == 0
    assert result.blocked_identities == 1
    assert _table_rows(store, "arb_opportunities") == []
    assert _table_rows(store, "arb_routes") == []
    dead_letters = _table_rows(store, "arb_dead_letters")
    assert [row["error_code"] for row in dead_letters] == ["symbol_only_cross_chain_identity"]
    dead_letter_payload = json.loads(dead_letters[0]["payload_json"])
    assert dead_letter_payload["verification_evidence"]["reason"] == "missing_bridge_group"


def test_depeg_detector_compares_stable_asset_against_configured_peg(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="USDC", name="USD Coin")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=6,
    )
    market_id = _seed_dex_market(
        store,
        asset_id=asset_id,
        market_symbol="USDC/USDT",
        quote_asset="USDT",
    )
    observed_ms = 2_000
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=0.97,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    _seed_pool_snapshot(store, market_id=market_id, observed_at_ms=observed_ms)

    result = ArbitrageDetector(store, depeg_threshold_bps=100).detect_depegs(now_ms=observed_ms)

    assert result.opportunities_upserted == 1
    opportunity = _table_rows(store, "arb_opportunities")[0]
    payload = json.loads(opportunity["payload_json"])
    assert opportunity["anomaly_type"] == "depeg"
    assert payload["detection_reason"] == "stable_below_peg"
    assert payload["reference_price"] == 1.0
    assert round(payload["deviation_bps"], 6) == 300.0
    assert payload["source_freshness"]["dex_tick"]["status"] == "fresh"
    assert payload["token_ca"] == POLYGON_SOL_CA


def test_price_spike_detector_distinguishes_upside_spike_from_drawdown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    market_id = _seed_dex_market(store, asset_id=asset_id)
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=1_000,
        price_usd=100.0,
    )
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=2_000,
        price_usd=112.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )

    spike = ArbitrageDetector(store, price_spike_threshold_bps=500).detect_price_spikes(now_ms=2_000)
    drawdown = ArbitrageDetector(store, drawdown_threshold_bps=500).detect_dex_drawdowns(now_ms=2_000)

    assert spike.opportunities_upserted == 1
    assert drawdown.opportunities_upserted == 0
    payload = json.loads(_table_rows(store, "arb_opportunities")[0]["payload_json"])
    assert payload["anomaly_type"] == "price_spike"
    assert payload["detection_reason"] == "dex_price_spike_upside"
    assert payload["spike_bps"] == 1200.0


def test_liquidity_collapse_detector_records_reserve_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    market_id = _seed_dex_market(store, asset_id=asset_id)
    store.record_market_tick(
        market_id=market_id,
        source="dexscreener",
        observed_at_ms=2_000,
        price_usd=100.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    _seed_pool_snapshot(
        store,
        market_id=market_id,
        observed_at_ms=1_000,
        reserve0_raw="1000",
        reserve1_raw="100000",
        liquidity_usd=1_000_000.0,
    )
    _seed_pool_snapshot(
        store,
        market_id=market_id,
        observed_at_ms=2_000,
        reserve0_raw="500",
        reserve1_raw="50000",
        liquidity_usd=500_000.0,
    )

    result = ArbitrageDetector(store, liquidity_collapse_threshold_bps=3_000).detect_liquidity_collapses(now_ms=2_000)

    assert result.opportunities_upserted == 1
    payload = json.loads(_table_rows(store, "arb_opportunities")[0]["payload_json"])
    assert payload["anomaly_type"] == "liquidity_collapse"
    assert payload["collapse_bps"] == 5000.0
    assert payload["reserve_collapse_evidence"]["baseline"]["reserve0_raw"] == "1000"
    assert payload["reserve_collapse_evidence"]["current"]["reserve0_raw"] == "500"
    assert payload["depth_collapse_evidence"]["source"] == "pool_reserves"


def test_pool_divergence_detector_requires_verified_same_contract_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    quick_market_id = _seed_dex_market(store, asset_id=asset_id)
    sushi_pool_ca = "0x5555555555555555555555555555555555555555"
    sushi_market_id = _seed_dex_market(
        store,
        asset_id=asset_id,
        token_contract_address=POLYGON_SOL_CA,
        venue_code="SUSHISWAP",
        venue_name="SushiSwap",
        pool_address=sushi_pool_ca,
    )
    observed_ms = 2_000
    store.record_market_tick(
        market_id=quick_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=100.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    store.record_market_tick(
        market_id=sushi_market_id,
        source="dexscreener",
        observed_at_ms=observed_ms,
        price_usd=105.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )

    result = ArbitrageDetector(store, pool_divergence_threshold_bps=100).detect_pool_divergences(now_ms=observed_ms)

    assert result.opportunities_upserted == 1
    opportunity = _table_rows(store, "arb_opportunities")[0]
    payload = json.loads(opportunity["payload_json"])
    assert opportunity["anomaly_type"] == "pool_divergence"
    assert payload["detection_reason"] == "same_asset_pool_price_divergence"
    assert payload["identity"]["verification"]["symbol_only"] is False
    assert payload["token_ca"] == POLYGON_SOL_CA
    assert payload["sell_pool_ca"] == sushi_pool_ca


def test_detector_ttl_blocks_stale_dex_cex_fx_and_rpc_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    dex_market_id = _seed_dex_market(store, asset_id=asset_id)
    cex_market_id = _seed_cex_market(
        store,
        asset_id=asset_id,
        venue_code="BINANCE",
        market_symbol="SOL/USDT",
        market_key="BINANCE:SOL-USDT",
        quote_asset="USDT",
        deposit_network="POLYGON",
    )
    now_ms = 100_000
    ttl_config = {"dex_tick_ttl_ms": 1_000, "cex_orderbook_ttl_ms": 1_000, "rpc_freshness_ttl_ms": 1_000}
    detector = ArbitrageDetector(store, ttl_config=ttl_config)
    store.record_market_tick(
        market_id=dex_market_id,
        source="dexscreener",
        observed_at_ms=now_ms - 5_000,
        price_usd=100.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    store.record_market_tick(
        market_id=cex_market_id,
        source="binance_orderbook",
        observed_at_ms=now_ms - 5_000,
        price_usd=110.0,
        best_bid=110.0,
        best_ask=111.0,
    )
    assert detector.detect_dex_cex_spreads(now_ms=now_ms).opportunities_upserted == 0

    store.record_market_tick(
        market_id=dex_market_id,
        source="dexscreener",
        observed_at_ms=now_ms,
        price_usd=100.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    assert detector.detect_dex_cex_spreads(now_ms=now_ms).opportunities_upserted == 0

    store.record_market_tick(
        market_id=cex_market_id,
        source="binance_orderbook",
        observed_at_ms=now_ms,
        price_usd=110.0,
        best_bid=110.0,
        best_ask=111.0,
    )
    assert detector.detect_dex_cex_spreads(now_ms=now_ms).opportunities_upserted == 1

    store.set_provider_health(
        provider_key="alchemy",
        status="DEGRADED",
        reason="rpc_timeout",
        capability="rpc_freshness",
        scope_key="POLYGON",
    )
    assert detector.detect_dex_cex_spreads(now_ms=now_ms).opportunities_upserted == 0

    krw_store = _store(tmp_path / "krw")
    krw_asset_id = krw_store.ensure_asset(symbol="SOL", name="Solana")
    krw_store.ensure_token(
        asset_id=krw_asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
    )
    krw_dex_market_id = _seed_dex_market(krw_store, asset_id=krw_asset_id)
    krw_cex_market_id = _seed_cex_market(
        krw_store,
        asset_id=krw_asset_id,
        venue_code="UPBIT",
        market_symbol="SOL/KRW",
        market_key="UPBIT:SOL-KRW",
        quote_asset="KRW",
        deposit_network="POLYGON",
    )
    krw_store.record_market_tick(
        market_id=krw_dex_market_id,
        source="dexscreener",
        observed_at_ms=now_ms,
        price_usd=100.0,
        payload={"baseToken": {"address": POLYGON_SOL_CA}},
    )
    krw_store.record_market_tick(
        market_id=krw_cex_market_id,
        source="upbit_orderbook",
        observed_at_ms=now_ms,
        price_krw=130_000.0,
        best_bid=130_000.0,
        best_ask=131_000.0,
    )
    _record_fx_rate(krw_store, pair="USDT/KRW", rate=1_000.0, observed_at_ms=now_ms - 5_000)

    krw_result = ArbitrageDetector(
        krw_store,
        ttl_config={"dex_tick_ttl_ms": 1_000, "krw_orderbook_ttl_ms": 1_000, "fx_ttl_ms": 1_000},
    ).detect_dex_krw_spreads(now_ms=now_ms)

    assert krw_result.opportunities_upserted == 0
