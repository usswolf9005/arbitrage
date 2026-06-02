from __future__ import annotations

from pathlib import Path
from typing import Any

from arbitrage.engine import ArbitrageEngine, SUPPORTED_PRECHECK_CHECK_NAMES
from arbitrage.route_evaluator import (
    COMPONENT_NAMES,
    EdgeComponentEvidence,
    RouteEvaluator,
    evaluate_stored_route,
    evaluate_route_components,
    required_components_for_route,
)
from arbitrage.store import ArbitrageStore


AS_OF_MS = 10_000
FRESH_UNTIL_MS = 40_000
POLYGON_SOL_CA = "0x1111111111111111111111111111111111111111"
BASE_SOL_CA = "0x3333333333333333333333333333333333333333"
POLYGON_POOL_CA = "0x2222222222222222222222222222222222222222"
BASE_POOL_CA = "0x4444444444444444444444444444444444444444"


def _component(
    name: str,
    *,
    cost_bps: float = 0.0,
    fresh_until_ms: int = FRESH_UNTIL_MS,
    stale: bool = False,
) -> EdgeComponentEvidence:
    return EdgeComponentEvidence(
        name=name,
        cost_bps=cost_bps,
        observed_at_ms=AS_OF_MS - 250,
        fresh_until_ms=fresh_until_ms,
        stale=stale,
        details={"source": "fixture"},
    )


def _components(
    names: tuple[str, ...] | list[str],
    *,
    costs: dict[str, float] | None = None,
) -> dict[str, EdgeComponentEvidence]:
    costs = costs or {}
    return {name: _component(name, cost_bps=costs.get(name, 0.0)) for name in names}


def _table_count(store: ArbitrageStore, table: str) -> int:
    with store.conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])


def _seed_eval_route(
    store: ArbitrageStore,
    *,
    route_type: str,
    edge_expected_bps: float,
    sell_kind: str = "DEX",
    sell_quote_asset: str = "USDC",
    sell_chain_code: str = "POLYGON",
    sell_deposit_network: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, int]:
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address=POLYGON_SOL_CA,
        decimals=18,
        bridge_group="sol-wormhole",
    )
    if sell_kind == "DEX" and sell_chain_code == "BASE":
        store.ensure_token(
            asset_id=asset_id,
            chain_id="8453",
            chain_code="BASE",
            contract_address=BASE_SOL_CA,
            decimals=18,
            bridge_group="sol-wormhole",
        )

    buy_venue_id = store.ensure_venue("QUICKSWAP", "DEX", "QuickSwap")
    buy_market_id = store.ensure_market(
        market_key="POLYGON:QUICKSWAP:SOL-USDC:0xpoolbuy",
        asset_id=asset_id,
        venue_id=buy_venue_id,
        market_type="DEX_POOL",
        chain_code="POLYGON",
        pool_address=POLYGON_POOL_CA,
        market_symbol="SOL/USDC",
        quote_asset="USDC",
        payload={"chain_id": "137", "token_contract_address": POLYGON_SOL_CA},
    )
    if sell_kind == "CEX":
        sell_venue_id = store.ensure_venue("UPBIT", "CEX", "Upbit")
        sell_market_id = store.ensure_market(
            market_key=f"UPBIT:SOL-{sell_quote_asset}",
            asset_id=asset_id,
            venue_id=sell_venue_id,
            market_type="CEX_ORDERBOOK",
            chain_code=sell_quote_asset,
            market_symbol=f"SOL/{sell_quote_asset}",
            quote_asset=sell_quote_asset,
            deposit_network=sell_deposit_network,
        )
    else:
        sell_venue_id = store.ensure_venue("BASESWAP" if sell_chain_code == "BASE" else "QUICKSWAP", "DEX", "DEX")
        sell_market_id = (
            buy_market_id
            if sell_chain_code == "POLYGON"
            else store.ensure_market(
                market_key="BASE:BASESWAP:SOL-USDC:0xpoolbase",
                asset_id=asset_id,
                venue_id=sell_venue_id,
                market_type="DEX_POOL",
                chain_code="BASE",
                pool_address=BASE_POOL_CA,
                market_symbol="SOL/USDC",
                quote_asset=sell_quote_asset,
                payload={"chain_id": "8453", "token_contract_address": BASE_SOL_CA},
            )
        )

    opportunity_id = store.upsert_opportunity(
        opportunity_key=f"SOL:{route_type}:eval",
        asset_id=asset_id,
        anomaly_type="route_eval_fixture",
        lifecycle_status="DETECTED",
        safety_status="WARN",
        buy_market_id=buy_market_id,
        sell_market_id=sell_market_id,
        spread_bps=edge_expected_bps,
        edge_expected_bps=edge_expected_bps,
        edge_worst_bps=0.0,
        first_seen_at_ms=AS_OF_MS - 1_000,
        last_seen_at_ms=AS_OF_MS,
    )
    route_id = store.upsert_route(
        route_key=f"SOL:{route_type}:eval",
        opportunity_id=opportunity_id,
        route_type=route_type,
        buy_market_id=buy_market_id,
        sell_market_id=sell_market_id,
        safety_status="WARN",
        route_status="WAIT",
        edge_expected_bps=edge_expected_bps,
        edge_worst_bps=0.0,
        selected=True,
        edge_worst_verified=False,
        warning_reasons=["candidate_only", "edge_worst_unverified"],
        payload={"latency_haircut_bps": 15.0, "notional_krw": 100_000.0, **(payload or {})},
    )
    return {
        "asset_id": asset_id,
        "buy_market_id": buy_market_id,
        "sell_market_id": sell_market_id,
        "opportunity_id": opportunity_id,
        "route_id": route_id,
    }


def _record_fx_rate(store: ArbitrageStore, *, observed_at_ms: int = AS_OF_MS - 100, stale: bool = False) -> None:
    with store.conn() as conn:
        conn.execute(
            """
            INSERT INTO arb_fx_rates(pair, source, observed_at_ms, rate, stale, payload_json)
            VALUES ('USDT/KRW', 'upbit_public', ?, 1400.0, ?, '{}')
            """,
            (int(observed_at_ms), 1 if stale else 0),
        )


def _precheck_rows(store: ArbitrageStore, precheck_run_id: int) -> list[dict[str, Any]]:
    with store.conn() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT check_name, status, error_code, error_msg, details_json
                FROM arb_precheck_results
                WHERE precheck_run_id = ?
                ORDER BY check_name
                """,
                (int(precheck_run_id),),
            ).fetchall()
        ]


def test_evaluator_result_contract_supports_all_component_names() -> None:
    costs = {"gas": 12.0, "swap_fee": 8.0, "bridge_fee": 15.0, "slippage": 30.0}
    result = evaluate_route_components(
        route_id=101,
        route_type="bridge_cex_sell",
        edge_expected_bps=500.0,
        required_components=COMPONENT_NAMES,
        components=_components(COMPONENT_NAMES, costs=costs),
        as_of_ms=AS_OF_MS,
    )

    payload = result.to_dict()
    assert payload["route_id"] == 101
    assert payload["route_type"] == "bridge_cex_sell"
    assert payload["edge_expected_bps"] == 500.0
    assert payload["edge_worst_bps"] == 435.0
    assert payload["edge_worst_verified"] is True
    assert payload["missing_components"] == []
    assert payload["warning_reasons"] == []
    assert payload["blocker_reasons"] == []
    assert set(payload["freshness"]) == set(COMPONENT_NAMES)
    assert set(payload["component_evidence"]) == set(COMPONENT_NAMES)
    assert all(record["status"] == "fresh" for record in payload["freshness"].values())


def test_missing_required_component_keeps_edge_verification_closed() -> None:
    required = ("buy_quote", "sell_quote_or_orderbook", "gas", "rpc_freshness")
    components = _components(("buy_quote", "sell_quote_or_orderbook", "rpc_freshness"))

    result = RouteEvaluator().evaluate(
        route_id=202,
        route_type="same_dex_sell",
        edge_expected_bps=300.0,
        required_components=required,
        components=components,
        as_of_ms=AS_OF_MS,
    )

    assert result.edge_worst_verified is False
    assert result.missing_components == ["gas"]
    assert result.stale_components == []
    assert "edge_worst_unverified" in result.warning_reasons
    assert result.blocker_reasons == ["edge_component_missing:gas"]
    assert result.freshness["gas"]["status"] == "missing"


def test_stale_required_component_keeps_edge_verification_closed() -> None:
    required = ("buy_quote", "sell_quote_or_orderbook", "gas", "rpc_freshness")
    components = _components(required)
    components["rpc_freshness"] = _component("rpc_freshness", fresh_until_ms=AS_OF_MS)

    result = evaluate_route_components(
        route_id=303,
        route_type="same_dex_sell",
        edge_expected_bps=300.0,
        required_components=required,
        components=components,
        as_of_ms=AS_OF_MS,
    )

    assert result.edge_worst_verified is False
    assert result.missing_components == []
    assert result.stale_components == ["rpc_freshness"]
    assert result.blocker_reasons == ["edge_component_stale:rpc_freshness"]
    assert result.freshness["rpc_freshness"]["status"] == "stale"


def test_route_defaults_include_krw_fx_only_for_cex_quote_asset() -> None:
    direct_krw = required_components_for_route("direct_cex_sell", quote_asset="KRW")
    direct_usd = required_components_for_route("direct_cex_sell", quote_asset="USDT")

    assert "fx" in direct_krw
    assert "fx" not in direct_usd
    assert "deposit_or_bridge_status" in direct_krw
    assert "bridge_fee" in required_components_for_route("bridge_dex_sell")


def test_mapping_component_input_preserves_details() -> None:
    result = evaluate_route_components(
        route_id=404,
        route_type="same_dex_sell",
        edge_expected_bps=100.0,
        required_components=("buy_quote",),
        components={
            "buy_quote": {
                "cost_bps": 0.0,
                "observed_at_ms": AS_OF_MS - 100,
                "fresh_until_ms": FRESH_UNTIL_MS,
                "source": "unit_fixture",
                "quote_id": "quote-1",
            }
        },
        as_of_ms=AS_OF_MS,
    )

    evidence: dict[str, Any] = result.component_evidence["buy_quote"]
    assert result.edge_worst_verified is True
    assert evidence["details"] == {"source": "unit_fixture", "quote_id": "quote-1"}


def test_stored_same_dex_route_evaluation_updates_verified_route_evidence(tmp_path: Path) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(store, route_type="same_dex_sell", edge_expected_bps=500.0)
    before_execution_counts = {
        table: _table_count(store, table)
        for table in (
            "arb_execution_runs",
            "arb_execution_steps",
            "arb_orders",
            "arb_transactions",
            "arb_transfers",
        )
    }
    store.record_market_tick(
        market_id=seeded["buy_market_id"],
        source="dexscreener",
        observed_at_ms=AS_OF_MS - 500,
        price_usd=90.0,
        liquidity_usd=1_000_000,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="buy",
        source="swap_quote",
        destination="QUICKSWAP",
        amount_in_value_krw=100_000.0,
        amount_out_expected_krw=100_000.0,
        gas_krw=100.0,
        fee_krw=50.0,
        price_impact_bps=20.0,
        observed_at_ms=AS_OF_MS - 100,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="sell",
        source="swap_quote",
        destination="QUICKSWAP",
        amount_in_value_krw=100_000.0,
        amount_out_expected_krw=110_000.0,
        amount_out_min_krw=109_500.0,
        gas_krw=150.0,
        fee_krw=50.0,
        price_impact_bps=25.0,
        observed_at_ms=AS_OF_MS - 90,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.set_route_freshness(seeded["route_id"], {"rpc_block": FRESH_UNTIL_MS})

    result = evaluate_stored_route(store, seeded["route_id"], as_of_ms=AS_OF_MS)

    assert result.edge_worst_verified is True
    assert result.missing_components == []
    assert result.stale_components == []
    assert result.edge_worst_bps == 405.0

    route = store.get_route(seeded["route_id"])
    assert route["edge_worst_verified"] == 1
    assert route["edge_worst_bps"] == 405.0
    assert route["quote_fresh_until_ms"] == FRESH_UNTIL_MS
    assert route["route_status"] == "WARN"
    assert route["blocker_reasons"] == []
    assert route["warning_reasons"] == []
    assert route["payload"]["edge_evaluation"]["component_evidence"]["sell_quote_or_orderbook"]["details"]["evidence_type"] == "sell_quote"
    freshness = store.fetch_route_freshness(seeded["route_id"])
    assert freshness["buy_quote"] == FRESH_UNTIL_MS
    assert freshness["sell_quote"] == FRESH_UNTIL_MS
    assert freshness["rpc_block"] == FRESH_UNTIL_MS
    after_execution_counts = {table: _table_count(store, table) for table in before_execution_counts}
    assert after_execution_counts == before_execution_counts


def test_stored_direct_cex_krw_route_evaluation_uses_fx_orderbook_and_deposit_status(
    tmp_path: Path,
) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(
        store,
        route_type="direct_cex_sell",
        edge_expected_bps=800.0,
        sell_kind="CEX",
        sell_quote_asset="KRW",
        sell_deposit_network="POLYGON",
        payload={"latency_haircut_bps": 10.0},
    )
    store.record_market_tick(
        market_id=seeded["buy_market_id"],
        source="dexscreener",
        observed_at_ms=AS_OF_MS - 300,
        price_usd=70.0,
        liquidity_usd=1_000_000,
    )
    store.record_orderbook_snapshot(
        market_id=seeded["sell_market_id"],
        source="upbit_ws",
        observed_at_ms=AS_OF_MS - 200,
        best_bid=115_000.0,
        best_ask=116_000.0,
        depth=[{"price": 115_000.0, "quantity": 10.0}],
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="buy",
        source="swap_quote",
        destination="QUICKSWAP",
        amount_in_value_krw=100_000.0,
        gas_krw=100.0,
        fee_krw=0.0,
        price_impact_bps=10.0,
        observed_at_ms=AS_OF_MS - 100,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="cex_sell",
        source="upbit_orderbook",
        destination="UPBIT",
        amount_in_value_krw=100_000.0,
        fee_krw=200.0,
        price_impact_bps=30.0,
        observed_at_ms=AS_OF_MS - 90,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    _record_fx_rate(store)
    store.set_route_freshness(
        seeded["route_id"],
        {"rpc_block": FRESH_UNTIL_MS, "deposit_status": FRESH_UNTIL_MS},
    )

    result = evaluate_stored_route(store, seeded["route_id"], as_of_ms=AS_OF_MS)

    assert result.edge_worst_verified is True
    assert result.edge_worst_bps == 720.0
    assert "fx" in result.component_evidence
    assert result.component_evidence["sell_quote_or_orderbook"]["details"]["evidence_type"] == "orderbook"

    route = store.get_route(seeded["route_id"])
    assert route["edge_worst_verified"] == 1
    assert route["edge_worst_bps"] == 720.0
    assert route["payload"]["edge_evaluation"]["component_evidence"]["fx"]["details"]["effective_rate"] == 1400.0
    freshness = store.fetch_route_freshness(seeded["route_id"])
    assert freshness["orderbook"] > AS_OF_MS
    assert freshness["fx"] > AS_OF_MS
    assert freshness["deposit_status"] == FRESH_UNTIL_MS


def test_stored_bridge_route_missing_bridge_fee_and_status_stays_unverified(
    tmp_path: Path,
) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(
        store,
        route_type="bridge_dex_sell",
        edge_expected_bps=650.0,
        sell_kind="DEX",
        sell_chain_code="BASE",
    )
    store.record_market_tick(
        market_id=seeded["buy_market_id"],
        source="dexscreener",
        observed_at_ms=AS_OF_MS - 300,
        price_usd=70.0,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="sell",
        source="swap_quote",
        destination="BASESWAP",
        amount_in_value_krw=100_000.0,
        amount_out_expected_krw=108_000.0,
        gas_krw=120.0,
        fee_krw=70.0,
        price_impact_bps=35.0,
        observed_at_ms=AS_OF_MS - 100,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.set_route_freshness(seeded["route_id"], {"rpc_block": FRESH_UNTIL_MS})

    result = evaluate_stored_route(store, seeded["route_id"], as_of_ms=AS_OF_MS)

    assert result.edge_worst_verified is False
    assert result.missing_components == ["bridge_fee", "deposit_or_bridge_status"]
    assert "edge_component_missing:bridge_fee" in result.blocker_reasons
    assert "edge_component_missing:deposit_or_bridge_status" in result.blocker_reasons
    route = store.get_route(seeded["route_id"])
    assert route["edge_worst_verified"] == 0
    assert route["route_status"] == "WARN"
    assert route["blocker_reasons"] == result.blocker_reasons


def test_stored_bridge_route_requires_eta_with_bridge_fee_evidence(
    tmp_path: Path,
) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(
        store,
        route_type="bridge_dex_sell",
        edge_expected_bps=650.0,
        sell_kind="DEX",
        sell_chain_code="BASE",
    )
    store.record_market_tick(
        market_id=seeded["buy_market_id"],
        source="dexscreener",
        observed_at_ms=AS_OF_MS - 300,
        price_usd=70.0,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="buy",
        source="swap_quote",
        destination="QUICKSWAP",
        amount_in_value_krw=100_000.0,
        gas_krw=100.0,
        fee_krw=50.0,
        price_impact_bps=10.0,
        observed_at_ms=AS_OF_MS - 100,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="sell",
        source="swap_quote",
        destination="BASESWAP",
        amount_in_value_krw=100_000.0,
        amount_out_expected_krw=108_000.0,
        gas_krw=120.0,
        fee_krw=70.0,
        price_impact_bps=20.0,
        observed_at_ms=AS_OF_MS - 95,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="bridge",
        source="bridge_quote",
        destination="BASE",
        amount_in_value_krw=100_000.0,
        fee_krw=300.0,
        observed_at_ms=AS_OF_MS - 90,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.set_route_freshness(
        seeded["route_id"],
        {"rpc_block": FRESH_UNTIL_MS, "bridge_status": FRESH_UNTIL_MS},
    )

    result = evaluate_stored_route(store, seeded["route_id"], as_of_ms=AS_OF_MS)

    assert result.edge_worst_verified is False
    assert result.missing_components == ["bridge_fee"]
    assert result.blocker_reasons == ["edge_component_missing:bridge_fee"]


def test_stored_bridge_cex_route_requires_bridge_and_deposit_status_evidence(
    tmp_path: Path,
) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(
        store,
        route_type="bridge_cex_sell",
        edge_expected_bps=900.0,
        sell_kind="CEX",
        sell_quote_asset="KRW",
        sell_deposit_network="BASE",
        payload={"latency_haircut_bps": 10.0},
    )
    store.record_market_tick(
        market_id=seeded["buy_market_id"],
        source="dexscreener",
        observed_at_ms=AS_OF_MS - 300,
        price_usd=70.0,
    )
    store.record_orderbook_snapshot(
        market_id=seeded["sell_market_id"],
        source="upbit_ws",
        observed_at_ms=AS_OF_MS - 200,
        best_bid=115_000.0,
        best_ask=116_000.0,
        depth=[{"price": 115_000.0, "quantity": 10.0}],
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="buy",
        source="swap_quote",
        destination="QUICKSWAP",
        amount_in_value_krw=100_000.0,
        gas_krw=100.0,
        fee_krw=50.0,
        price_impact_bps=10.0,
        observed_at_ms=AS_OF_MS - 100,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="bridge",
        source="bridge_quote",
        destination="BASE",
        amount_in_value_krw=100_000.0,
        fee_krw=300.0,
        eta_seconds=600,
        observed_at_ms=AS_OF_MS - 95,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="cex_sell",
        source="upbit_orderbook",
        destination="UPBIT",
        amount_in_value_krw=100_000.0,
        fee_krw=200.0,
        price_impact_bps=30.0,
        observed_at_ms=AS_OF_MS - 90,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    _record_fx_rate(store)
    store.set_route_freshness(
        seeded["route_id"],
        {"rpc_block": FRESH_UNTIL_MS, "deposit_status": FRESH_UNTIL_MS},
    )

    result = evaluate_stored_route(store, seeded["route_id"], as_of_ms=AS_OF_MS)

    assert result.edge_worst_verified is False
    assert result.missing_components == ["deposit_or_bridge_status"]
    assert "edge_component_missing:deposit_or_bridge_status" in result.blocker_reasons

    store.set_route_freshness(seeded["route_id"], {"bridge_status": FRESH_UNTIL_MS})
    result = evaluate_stored_route(store, seeded["route_id"], as_of_ms=AS_OF_MS)

    assert result.edge_worst_verified is True
    status_details = result.component_evidence["deposit_or_bridge_status"]["details"]
    assert status_details["source_keys"] == ["bridge_status", "deposit_status"]
    assert status_details["source_fresh_until_ms"] == {
        "bridge_status": FRESH_UNTIL_MS,
        "deposit_status": FRESH_UNTIL_MS,
    }
    assert result.component_evidence["bridge_fee"]["details"]["eta_seconds"] == 600


def test_stored_direct_cex_route_blocks_on_stale_rpc_and_orderbook(
    tmp_path: Path,
) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(
        store,
        route_type="direct_cex_sell",
        edge_expected_bps=800.0,
        sell_kind="CEX",
        sell_quote_asset="KRW",
        sell_deposit_network="POLYGON",
        payload={"latency_haircut_bps": 10.0},
    )
    store.record_market_tick(
        market_id=seeded["buy_market_id"],
        source="dexscreener",
        observed_at_ms=AS_OF_MS - 300,
        price_usd=70.0,
    )
    store.record_orderbook_snapshot(
        market_id=seeded["sell_market_id"],
        source="upbit_ws",
        observed_at_ms=AS_OF_MS - 200,
        best_bid=115_000.0,
        best_ask=116_000.0,
        depth=[{"price": 115_000.0, "quantity": 10.0}],
        stale=True,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="buy",
        source="swap_quote",
        destination="QUICKSWAP",
        amount_in_value_krw=100_000.0,
        gas_krw=100.0,
        fee_krw=0.0,
        price_impact_bps=10.0,
        observed_at_ms=AS_OF_MS - 100,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    store.record_route_quote(
        route_id=seeded["route_id"],
        leg_type="cex_sell",
        source="upbit_orderbook",
        destination="UPBIT",
        amount_in_value_krw=100_000.0,
        fee_krw=200.0,
        price_impact_bps=30.0,
        observed_at_ms=AS_OF_MS - 90,
        expires_at_ms=FRESH_UNTIL_MS,
    )
    _record_fx_rate(store)
    store.set_route_freshness(
        seeded["route_id"],
        {"rpc_block": AS_OF_MS - 1, "deposit_status": FRESH_UNTIL_MS},
    )

    result = evaluate_stored_route(store, seeded["route_id"], as_of_ms=AS_OF_MS)

    assert result.edge_worst_verified is False
    assert result.stale_components == ["sell_quote_or_orderbook", "rpc_freshness"]
    assert "edge_component_stale:sell_quote_or_orderbook" in result.blocker_reasons
    assert "edge_component_stale:rpc_freshness" in result.blocker_reasons
    route = store.get_route(seeded["route_id"])
    assert route["edge_worst_verified"] == 0
    assert route["route_status"] == "BLOCKED"
    assert route["safety_status"] == "BLOCK"


def test_evaluator_writes_no_execution_order_transaction_or_transfer_rows(tmp_path: Path) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    before = {
        table: _table_count(store, table)
        for table in (
            "arb_execution_runs",
            "arb_execution_steps",
            "arb_orders",
            "arb_transactions",
            "arb_transfers",
        )
    }

    result = evaluate_route_components(
        route_id=505,
        route_type="same_dex_sell",
        edge_expected_bps=250.0,
        components=_components(required_components_for_route("same_dex_sell")),
        as_of_ms=AS_OF_MS,
    )

    after = {table: _table_count(store, table) for table in before}
    assert result.edge_worst_verified is True
    assert after == before


def test_precheck_supports_expanded_check_names_and_idempotent_rows(tmp_path: Path) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(store, route_type="same_dex_sell", edge_expected_bps=500.0)
    store.set_route_edge_verification(seeded["route_id"], verified=True)

    checks = [{"check_name": name, "status": "PASS"} for name in SUPPORTED_PRECHECK_CHECK_NAMES]
    checks.append({"check_name": "sell_quote", "status": "PASS", "details": {"duplicate": True}})

    result = ArbitrageEngine(store).run_precheck(
        opportunity_id=seeded["opportunity_id"],
        route_id=seeded["route_id"],
        checks=checks,
    )

    assert result["status"] == "PASS"
    route = store.get_route(seeded["route_id"])
    assert route["safety_status"] == "PASS"
    assert route["route_status"] == "OPEN"
    rows = _precheck_rows(store, result["precheck_run_id"])
    assert len(rows) == len(SUPPORTED_PRECHECK_CHECK_NAMES)
    assert {row["check_name"] for row in rows} == set(SUPPORTED_PRECHECK_CHECK_NAMES)
    assert {row["status"] for row in rows} == {"PASS"}


def test_precheck_all_pass_requires_verified_edge_before_opening_route(tmp_path: Path) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(store, route_type="same_dex_sell", edge_expected_bps=500.0)

    result = ArbitrageEngine(store).run_precheck(
        opportunity_id=seeded["opportunity_id"],
        route_id=seeded["route_id"],
        checks=[
            {"check_name": "sell_quote", "status": "PASS"},
            {"check_name": "small_sell_simulation", "status": "PASS"},
            {"check_name": "route_edge", "status": "PASS"},
        ],
    )

    assert result["status"] == "WARN"
    assert result["route_status"] == "WARN"
    route = store.get_route(seeded["route_id"])
    assert route["safety_status"] == "WARN"
    assert route["route_status"] == "WARN"
    assert route["warning_reasons"] == ["edge_worst_unverified"]


def test_precheck_warn_without_block_keeps_route_warn(tmp_path: Path) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(store, route_type="same_dex_sell", edge_expected_bps=500.0)
    store.set_route_edge_verification(seeded["route_id"], verified=True)

    result = ArbitrageEngine(store).run_precheck(
        opportunity_id=seeded["opportunity_id"],
        route_id=seeded["route_id"],
        checks=[
            {"check_name": "sell_quote", "status": "PASS"},
            {"check_name": "pool_reserve", "status": "WARN", "error_code": "thin_pool_reserve"},
        ],
    )

    route = store.get_route(seeded["route_id"])
    assert result["status"] == "WARN"
    assert result["route_status"] == "WARN"
    assert route["safety_status"] == "WARN"
    assert route["route_status"] == "WARN"
    assert route["warning_reasons"] == ["thin_pool_reserve"]


def test_blocked_cex_precheck_does_not_block_pass_same_chain_route(tmp_path: Path) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(store, route_type="same_dex_sell", edge_expected_bps=500.0)
    same_route_id = store.upsert_route(
        route_key="SOL:same_dex_sell:eval",
        opportunity_id=seeded["opportunity_id"],
        route_type="same_dex_sell",
        buy_market_id=seeded["buy_market_id"],
        sell_market_id=seeded["sell_market_id"],
        safety_status="PASS",
        route_status="OPEN",
        edge_expected_bps=500.0,
        edge_worst_bps=420.0,
        selected=True,
        quote_fresh_until_ms=FRESH_UNTIL_MS,
        edge_worst_verified=True,
    )
    cex_route_id = store.upsert_route(
        route_key="SOL:direct_cex_sell:eval",
        opportunity_id=seeded["opportunity_id"],
        route_type="direct_cex_sell",
        buy_market_id=seeded["buy_market_id"],
        sell_market_id=seeded["sell_market_id"],
        safety_status="WARN",
        route_status="CHECKING",
        edge_expected_bps=520.0,
        edge_worst_bps=450.0,
        selected=False,
        quote_fresh_until_ms=FRESH_UNTIL_MS,
        edge_worst_verified=True,
    )

    ArbitrageEngine(store).run_precheck(
        opportunity_id=seeded["opportunity_id"],
        route_id=cex_route_id,
        checks=[
            {"check_name": "sell_quote", "status": "PASS"},
            {"check_name": "cex_deposit", "status": "BLOCK", "error_code": "deposit_disabled"},
        ],
    )

    same_route = store.get_route(same_route_id)
    cex_route = store.get_route(cex_route_id)
    opportunity = store.get_opportunity(seeded["opportunity_id"])
    assert same_route["safety_status"] == "PASS"
    assert same_route["route_status"] == "OPEN"
    assert cex_route["safety_status"] == "BLOCK"
    assert cex_route["route_status"] == "BLOCKED"
    assert opportunity["safety_status"] == "PASS"
    assert opportunity["lifecycle_status"] == "PRECHECK_PASS"
    assert opportunity["selected_route_id"] == same_route_id


def test_blocked_selected_route_reselects_warn_route_or_clears_when_none_exist(tmp_path: Path) -> None:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    seeded = _seed_eval_route(store, route_type="direct_cex_sell", edge_expected_bps=500.0)
    warn_route_id = store.upsert_route(
        route_key="SOL:same_dex_sell:warn-fallback",
        opportunity_id=seeded["opportunity_id"],
        route_type="same_dex_sell",
        buy_market_id=seeded["buy_market_id"],
        sell_market_id=seeded["sell_market_id"],
        safety_status="WARN",
        route_status="WARN",
        edge_expected_bps=450.0,
        edge_worst_bps=375.0,
        selected=False,
        quote_fresh_until_ms=FRESH_UNTIL_MS,
        edge_worst_verified=False,
        warning_reasons=["edge_worst_unverified"],
    )

    ArbitrageEngine(store).run_precheck(
        opportunity_id=seeded["opportunity_id"],
        route_id=seeded["route_id"],
        checks=[{"check_name": "cex_deposit", "status": "BLOCK", "error_code": "deposit_disabled"}],
    )

    opportunity = store.get_opportunity(seeded["opportunity_id"])
    assert opportunity["selected_route_id"] == warn_route_id
    assert store.get_route(warn_route_id)["selected"] == 1

    ArbitrageEngine(store).run_precheck(
        opportunity_id=seeded["opportunity_id"],
        route_id=warn_route_id,
        checks=[{"check_name": "stale_data", "status": "ERROR", "error_code": "stale_route_data"}],
    )

    opportunity = store.get_opportunity(seeded["opportunity_id"])
    assert opportunity["selected_route_id"] is None
    assert store.get_route(warn_route_id)["selected"] == 0
    assert opportunity["safety_status"] == "ERROR"
    assert opportunity["lifecycle_status"] == "BLOCKED"
