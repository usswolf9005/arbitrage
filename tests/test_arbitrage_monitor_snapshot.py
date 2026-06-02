import http.client
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from arbitrage.api_server import create_server
from arbitrage.store import ArbitrageStore, now_ms


def _store(path: str) -> ArbitrageStore:
    store = ArbitrageStore(path)
    store.init()
    return store


def _seed_opportunity(store: ArbitrageStore, key: str) -> dict:
    asset_id = store.ensure_asset(symbol=key, name=key)
    buy_venue_id = store.ensure_venue(f"{key}_DEX", "DEX", f"{key} DEX")
    sell_venue_id = store.ensure_venue(f"{key}_CEX", "CEX", f"{key} CEX")
    buy_market_id = store.ensure_market(
        market_key=f"{key}:POLYGON:DEX:POOL",
        asset_id=asset_id,
        venue_id=buy_venue_id,
        market_type="DEX_POOL",
        chain_code="POLYGON",
        pool_address=f"0x{key.lower():0<40}"[:42],
        quote_asset="USDC",
    )
    sell_market_id = store.ensure_market(
        market_key=f"{key}:UPBIT:KRW",
        asset_id=asset_id,
        venue_id=sell_venue_id,
        market_type="CEX_ORDERBOOK",
        chain_code="KRW",
        market_symbol=f"{key}/KRW",
        quote_asset="KRW",
        deposit_network="POLYGON",
    )
    stamp = now_ms()
    opportunity_id = store.upsert_opportunity(
        opportunity_key=f"{key}:MONITOR",
        asset_id=asset_id,
        anomaly_type="dex_cex_spread",
        lifecycle_status="PRECHECK_PASS",
        safety_status="PASS",
        buy_market_id=buy_market_id,
        sell_market_id=sell_market_id,
        spread_bps=1000,
        edge_expected_bps=800,
        edge_worst_bps=600,
        first_seen_at_ms=stamp,
        last_seen_at_ms=stamp,
    )
    route_id = store.upsert_route(
        route_key=f"{key}:MONITOR:same_dex_sell",
        opportunity_id=opportunity_id,
        route_type="same_dex_sell",
        buy_market_id=buy_market_id,
        sell_market_id=sell_market_id,
        safety_status="PASS",
        route_status="OPEN",
        edge_expected_bps=800,
        edge_worst_bps=600,
        selected=True,
        quote_fresh_until_ms=stamp + 30_000,
        edge_worst_verified=True,
    )
    return {"opportunity_id": opportunity_id, "route_id": route_id}


def test_snapshot_api_includes_provider_jobs_and_scoped_simulation_runs() -> None:
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        selected = _seed_opportunity(store, "AAA")
        other = _seed_opportunity(store, "BBB")
        first_run = store.insert_simulation_run(
            simulation_key="selected-sim",
            status="RUNNING",
            requested_by="test",
            payload={"blockers": ["selected_blocker"]},
        )
        store.update_simulation_run(
            first_run["id"],
            status="COMPLETED",
            opportunity_id=selected["opportunity_id"],
            route_id=selected["route_id"],
            payload={"blockers": ["selected_blocker"], "simulated_pnl": {"net_krw": 1200}},
        )
        other_run = store.insert_simulation_run(
            simulation_key="other-sim",
            status="RUNNING",
            requested_by="test",
            payload={"blockers": ["other_blocker"]},
        )
        store.update_simulation_run(
            other_run["id"],
            status="FAILED",
            opportunity_id=other["opportunity_id"],
            route_id=other["route_id"],
            error_code="other_failed",
        )

        server = create_server("127.0.0.1", 0, store=store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn.request("GET", f"/api/arbitrage/snapshot?selected_opportunity_id={selected['opportunity_id']}")
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()
            server.shutdown()
            server.server_close()

        assert response.status == 200
        assert "provider_jobs" in payload
        assert payload["provider_jobs"]
        assert [run["id"] for run in payload["simulation_runs"]] == [first_run["id"]]
        assert payload["simulation_runs"][0]["payload"]["blockers"] == ["selected_blocker"]
