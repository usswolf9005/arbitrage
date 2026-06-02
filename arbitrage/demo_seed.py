from __future__ import annotations

from typing import Any

from .store import ArbitrageStore, now_ms


DEMO_SOL_ID = "demo-sol-same-dex"
DEMO_SOL_TOKEN_CA = "0x1111111111111111111111111111111111111111"
DEMO_SOL_BUY_POOL_CA = "0x2222222222222222222222222222222222222222"
DEMO_SOL_SELL_POOL_CA = "0x3333333333333333333333333333333333333333"
DEMO_SOL_OBSERVED_AT_MS = 1_700_000_000_000


def seed_demo_sol_opportunity(store: ArbitrageStore) -> dict[str, Any]:
    """Create the deterministic paper-only SOL demo opportunity."""
    store.init()
    stamp = now_ms()
    fresh_until_ms = stamp + 5 * 60_000

    store.configure_strategy_profile(
        "default",
        paper_enabled=True,
        one_click_enabled=False,
        auto_small_enabled=False,
        live_full_enabled=False,
        min_edge_worst_bps=100,
        active=True,
    )

    asset_id = store.ensure_asset(symbol="SOL", name="Solana", canonical_source="demo")
    token_id = store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=DEMO_SOL_TOKEN_CA,
        decimals=18,
        wrapped_kind="demo_wrapped",
        bridge_group="SOL",
    )
    buy_venue_id = store.ensure_venue("QUICKSWAP", "DEX", "QuickSwap V2")
    sell_venue_id = store.ensure_venue("UNISWAP", "DEX", "Uniswap V3")
    buy_market_id = store.ensure_market(
        market_key=f"{DEMO_SOL_ID}:POLYGON:QUICKSWAP:SOL-USDC:{DEMO_SOL_BUY_POOL_CA}",
        asset_id=asset_id,
        venue_id=buy_venue_id,
        market_type="DEX_POOL",
        chain_code="POLYGON",
        pool_address=DEMO_SOL_BUY_POOL_CA,
        market_symbol="SOL/USDC",
        quote_asset="USDC",
        payload={"demo": True, "demo_id": DEMO_SOL_ID, "paper_only": True},
    )
    sell_market_id = store.ensure_market(
        market_key=f"{DEMO_SOL_ID}:POLYGON:UNISWAP:SOL-USDC:{DEMO_SOL_SELL_POOL_CA}",
        asset_id=asset_id,
        venue_id=sell_venue_id,
        market_type="DEX_POOL",
        chain_code="POLYGON",
        pool_address=DEMO_SOL_SELL_POOL_CA,
        market_symbol="SOL/USDC",
        quote_asset="USDC",
        payload={"demo": True, "demo_id": DEMO_SOL_ID, "paper_only": True},
    )

    store.record_market_tick(
        market_id=buy_market_id,
        source="demo_seed",
        observed_at_ms=DEMO_SOL_OBSERVED_AT_MS,
        raw_price=71.33,
        price_usd=71.33,
        price_krw=99_862.0,
        best_ask=71.33,
        liquidity_usd=1_200_000,
        payload={"demo": True, "side": "buy", "demo_id": DEMO_SOL_ID},
    )
    store.record_market_tick(
        market_id=sell_market_id,
        source="demo_seed",
        observed_at_ms=DEMO_SOL_OBSERVED_AT_MS,
        raw_price=86.29,
        price_usd=86.29,
        price_krw=120_806.0,
        best_bid=86.29,
        liquidity_usd=1_500_000,
        payload={"demo": True, "side": "sell", "demo_id": DEMO_SOL_ID},
    )

    opportunity_id = store.upsert_opportunity(
        opportunity_key=f"{DEMO_SOL_ID}:opportunity",
        asset_id=asset_id,
        anomaly_type="demo_same_dex_spread",
        lifecycle_status="PRECHECK_PASS",
        safety_status="PASS",
        buy_market_id=buy_market_id,
        sell_market_id=sell_market_id,
        spread_bps=2_098,
        edge_expected_bps=1_560,
        edge_worst_bps=1_000,
        first_seen_at_ms=DEMO_SOL_OBSERVED_AT_MS,
        last_seen_at_ms=stamp,
        source_signalhub_event_id=f"{DEMO_SOL_ID}:signal",
        payload={
            "demo": True,
            "demo_id": DEMO_SOL_ID,
            "paper_only": True,
            "no_live_modes": True,
        },
        emit_event=False,
    )
    route_id = store.upsert_route(
        route_key=f"{DEMO_SOL_ID}:same_dex_sell",
        opportunity_id=opportunity_id,
        route_type="same_dex_sell",
        buy_market_id=buy_market_id,
        sell_market_id=sell_market_id,
        safety_status="PASS",
        route_status="OPEN",
        edge_expected_bps=1_560,
        edge_worst_bps=1_000,
        selected=True,
        quote_fresh_until_ms=fresh_until_ms,
        edge_worst_verified=True,
        payload={
            "demo": True,
            "demo_id": DEMO_SOL_ID,
            "paper_only": True,
            "no_external_submission": True,
            "edge_evaluation": {
                "edge_worst_verified": True,
                "missing_components": [],
                "stale_components": [],
                "freshness": {
                    "buy_quote": {"component": "buy_quote", "status": "fresh", "fresh_until_ms": fresh_until_ms},
                    "sell_quote": {"component": "sell_quote_or_orderbook", "status": "fresh", "fresh_until_ms": fresh_until_ms},
                    "rpc_block": {"component": "rpc_freshness", "status": "fresh", "fresh_until_ms": fresh_until_ms},
                },
            },
        },
    )
    store.set_route_freshness(
        route_id,
        {
            "buy_quote": fresh_until_ms,
            "sell_quote": fresh_until_ms,
            "rpc_block": fresh_until_ms,
        },
    )
    _ensure_route_quote(store, route_id=route_id)
    precheck_run_id = _ensure_precheck_pass_evidence(store, opportunity_id=opportunity_id, route_id=route_id)

    return {
        "ok": True,
        "demo_id": DEMO_SOL_ID,
        "asset_id": asset_id,
        "token_id": token_id,
        "opportunity_id": opportunity_id,
        "route_id": route_id,
        "buy_market_id": buy_market_id,
        "sell_market_id": sell_market_id,
        "precheck_run_id": precheck_run_id,
        "token_ca": DEMO_SOL_TOKEN_CA,
        "buy_pool_ca": DEMO_SOL_BUY_POOL_CA,
        "sell_pool_ca": DEMO_SOL_SELL_POOL_CA,
        "quote_fresh_until_ms": fresh_until_ms,
        "mode": "paper_demo_only",
    }


def _ensure_route_quote(store: ArbitrageStore, *, route_id: int) -> int:
    with store.conn() as conn:
        existing = conn.execute(
            """
            SELECT id FROM arb_route_quotes
            WHERE route_id = ?
              AND leg_type = 'exit'
              AND source = 'demo_seed'
              AND destination = 'UNISWAP'
              AND observed_at_ms = ?
            LIMIT 1
            """,
            (int(route_id), DEMO_SOL_OBSERVED_AT_MS),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO arb_route_quotes(
                route_id, leg_type, source, destination, amount_in_raw, amount_in_value_krw,
                amount_out_expected_krw, amount_out_min_krw, gas_krw, fee_krw, price_impact_bps,
                eta_seconds, observed_at_ms, expires_at_ms, stale, payload_json
            ) VALUES (?, 'exit', 'demo_seed', 'UNISWAP', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                int(route_id),
                "1000000000000000000",
                99_862.0,
                120_806.0,
                109_848.2,
                2_200.0,
                650.0,
                18.0,
                4,
                DEMO_SOL_OBSERVED_AT_MS,
                DEMO_SOL_OBSERVED_AT_MS + 300_000,
                '{"demo":true,"demo_id":"demo-sol-same-dex","paper_only":true}',
            ),
        )
        return int(cur.lastrowid)


def _ensure_precheck_pass_evidence(store: ArbitrageStore, *, opportunity_id: int, route_id: int) -> int:
    precheck_run_id = store.insert_precheck_run(
        run_key=f"{DEMO_SOL_ID}:precheck",
        opportunity_id=opportunity_id,
        route_id=route_id,
        status="PASS",
    )
    checks = (
        ("sell_quote", {"sell_venue": "UNISWAP", "pool_ca": DEMO_SOL_SELL_POOL_CA}),
        ("small_sell_simulation", {"max_slippage_bps": 180}),
        ("transfer_simulation", {"route_type": "same_dex_sell"}),
        ("tax_blacklist", {"token_ca": DEMO_SOL_TOKEN_CA}),
        ("pool_reserve", {"buy_pool_ca": DEMO_SOL_BUY_POOL_CA, "sell_pool_ca": DEMO_SOL_SELL_POOL_CA}),
        ("route_edge", {"edge_worst_bps": 1_000, "edge_worst_verified": True}),
        ("wallet_permission", {"mode": "paper", "paper_only": True}),
        ("stale_data", {"freshness_sources": ["buy_quote", "sell_quote", "rpc_block"]}),
    )
    for check_name, details in checks:
        store.insert_precheck_result(
            precheck_run_id=precheck_run_id,
            check_name=check_name,
            status="PASS",
            details={"demo": True, "demo_id": DEMO_SOL_ID, **details},
        )
    return precheck_run_id
