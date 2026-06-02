from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any

import arbitrage.api_server as api_server
from arbitrage.api_server import create_server
from arbitrage.demo_seed import seed_demo_sol_opportunity
from arbitrage.engine import ArbitrageEngine
from arbitrage.live_collectors import LiveProviderJobRunner
from arbitrage.simulation import SimulationRunner
from arbitrage.store import ArbitrageStore


NOW_MS = 1_779_539_700_000
TOKEN_CA = "0x1111111111111111111111111111111111111111"
POOL_CA = "0x2222222222222222222222222222222222222222"


def _store(tmp_path: Path) -> ArbitrageStore:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    return store


def _dex_payload(*, observed_at_ms: int = NOW_MS) -> dict[str, Any]:
    return {
        "pairs": [
            {
                "chainId": "polygon",
                "dexId": "quickswap",
                "pairAddress": POOL_CA,
                "baseToken": {
                    "address": TOKEN_CA,
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
                "priceNative": "70.0",
                "priceUsd": "70.0",
                "priceKrw": "98000",
                "liquidity": {"usd": "1000000", "base": "1000", "quote": "70000"},
                "volume": {"h24": "500000"},
                "blockNumber": 12345678,
                "observed_at_ms": observed_at_ms,
            }
        ],
    }


def _cex_payload(*, observed_at_ms: int = NOW_MS + 100, bid: str = "85.0") -> dict[str, Any]:
    return {
        "symbol": "SOLUSDT",
        "lastUpdateId": 987654321,
        "deposit_network": "POLYGON",
        "observed_at_ms": observed_at_ms,
        "bids": [[bid, "12.5"]],
        "asks": [["85.5", "8.1"]],
    }


def _rpc_payload(*, observed_at_ms: int = NOW_MS + 200) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "observed_at_ms": observed_at_ms,
        "result": {"number": hex(12345678), "hash": "0xabc"},
    }


def _table_rows(store: ArbitrageStore, table: str) -> list[dict[str, Any]]:
    order_by = "id"
    if table == "arb_provider_health":
        order_by = "provider_key"
    with store.conn() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()]


def test_live_provider_runner_stores_read_only_observations_and_rpc_freshness(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = LiveProviderJobRunner(
        store,
        fetchers={
            "dexscreener": lambda _job: _dex_payload(),
            "binance_public": lambda _job: _cex_payload(),
            "alchemy": lambda _job: _rpc_payload(),
        },
    )

    results = runner.run_once(
        [
            {"provider_key": "dexscreener", "capability": "dex_pool_price", "scope_key": "polygon:sol-usdc"},
            {"provider_key": "binance_public", "capability": "cex_orderbook", "scope_key": "binance:solusdt"},
            {
                "provider_key": "alchemy",
                "capability": "rpc_block_freshness",
                "scope_key": "polygon",
                "route_freshness_ttl_ms": 30_000,
            },
        ],
        now_ms=NOW_MS,
    )

    assert [result.status for result in results] == ["OK", "OK", "OK"]
    assert _table_rows(store, "arb_market_ticks")
    assert _table_rows(store, "arb_pool_snapshots")
    assert _table_rows(store, "arb_orderbook_snapshots")
    assert _table_rows(store, "arb_provider_health")[0]["status"] == "OK"
    assert _table_rows(store, "arb_orders") == []
    assert _table_rows(store, "arb_transactions") == []
    assert _table_rows(store, "arb_transfers") == []


def test_live_provider_runner_timeout_does_not_advance_cursor_and_deadletters(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = LiveProviderJobRunner(
        store,
        fetchers={"dexscreener": lambda _job: (_ for _ in ()).throw(TimeoutError("provider timed out"))},
    )

    before = store.get_collect_cursor("dexscreener", "polygon:sol-usdc")
    [result] = runner.run_once(
        [{"provider_key": "dexscreener", "capability": "dex_pool_price", "scope_key": "polygon:sol-usdc"}],
        now_ms=NOW_MS,
    )

    assert result.status == "DEGRADED"
    assert store.get_collect_cursor("dexscreener", "polygon:sol-usdc") == before
    assert _table_rows(store, "arb_dead_letters")[0]["error_code"] == "provider_timeout"


def test_simulation_run_collects_detects_evaluates_prechecks_and_paper_executes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = SimulationRunner(store).start(
        {
            "now_ms": NOW_MS + 1_000,
            "lookback_ms": 60_000,
            "spread_threshold_bps": 100.0,
            "jobs": [
                {
                    "provider_key": "dexscreener",
                    "capability": "dex_pool_price",
                    "scope_key": "polygon:sol-usdc",
                    "payload": _dex_payload(),
                },
                {
                    "provider_key": "binance_public",
                    "capability": "cex_orderbook",
                    "scope_key": "binance:solusdt",
                    "payload": _cex_payload(),
                },
            ],
        }
    )

    assert result["ok"] is True
    assert result["status"] == "COMPLETED"
    assert result["opportunity_id"] > 0
    assert result["run_id"] > 0
    assert result["paper_result"]["run"]["status"] == "SETTLED"

    opportunity = store.get_opportunity(result["opportunity_id"])
    route = store.get_route(result["route_id"])
    assert opportunity["anomaly_type"] == "dex_cex_spread"
    assert route["route_type"] == "direct_cex_sell"
    assert route["edge_worst_verified"] == 1
    assert result["stage_status"] == {
        "collect": "COMPLETED",
        "detect": "COMPLETED",
        "evaluate": "COMPLETED",
        "precheck": "COMPLETED",
        "paper_execution": "COMPLETED",
    }
    assert result["no_real_funds"] is True
    assert result["no_real_submit"] is True
    assert result["selected_opportunity"]["id"] == result["opportunity_id"]
    assert result["selected_route"]["id"] == result["route_id"]
    assert set(result["step_durations_ms"]).issuperset({"precheck", "dex_buy", "settle"})
    assert result["simulated_pnl"]["paper_only"] is True
    assert _table_rows(store, "arb_transactions") == []
    assert _table_rows(store, "arb_orders") == []
    assert _table_rows(store, "arb_transfers") == []
    assert any(row["event_type"] == "simulation.run.stage" for row in store.fetch_event_log(limit=50))
    assert any(row["event_type"] == "simulation.run.completed" for row in store.fetch_event_log(limit=50))


def test_duplicate_simulation_key_returns_conflict_without_replaying_pipeline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = SimulationRunner(store)
    payload = {
        "simulation_key": "sim:key:one",
        "now_ms": NOW_MS + 1_000,
        "lookback_ms": 60_000,
        "spread_threshold_bps": 100.0,
        "jobs": [
            {
                "provider_key": "dexscreener",
                "capability": "dex_pool_price",
                "scope_key": "polygon:sol-usdc",
                "payload": _dex_payload(),
            },
            {
                "provider_key": "binance_public",
                "capability": "cex_orderbook",
                "scope_key": "binance:solusdt",
                "payload": _cex_payload(),
            },
        ],
    }

    first = runner.start(payload)
    events_after_first = store.fetch_event_log(limit=100)
    second = runner.start(payload)
    events_after_second = store.fetch_event_log(limit=100)

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error_code"] == "simulation_key_conflict"
    assert second["simulation_run_id"] == first["simulation_run_id"]
    assert _table_rows(store, "arb_simulation_runs")[0]["simulation_key"] == "sim:key:one"
    assert len(events_after_second) == len(events_after_first)
    assert sum(row["event_type"] == "simulation.run.started" for row in events_after_second) == 1
    assert sum(row["event_type"] == "simulation.run.completed" for row in events_after_second) == 1


def test_api_simulation_live_collect_uses_http_adapter_catalog_and_configured_jobs(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    store = _store(tmp_path)
    calls: list[dict[str, Any]] = []

    class FakeHttpCatalog:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def fetch_payload(self, job: dict[str, Any]) -> dict[str, Any]:
            calls.append(dict(job))
            return _cex_payload(observed_at_ms=NOW_MS + 1_000)

    monkeypatch.setattr(api_server, "ReadOnlyHttpAdapterCatalog", FakeHttpCatalog)
    monkeypatch.setenv(
        "ARBITRAGE_PROVIDER_JOBS_JSON",
        json.dumps(
            [
                {
                    "provider_key": "binance_public",
                    "capability": "cex_orderbook",
                    "scope_key": "binance:SOLUSDT",
                    "symbol": "SOLUSDT",
                    "limit": 5,
                }
            ]
        ),
    )

    server = create_server("127.0.0.1", 0, store=store)
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        body = json.dumps(
            {
                "now_ms": NOW_MS + 1_000,
                "bounded_live_collect": True,
                "continue_on_provider_failure": True,
                "jobs": [],
            }
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/arbitrage/simulation-runs", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        created = json.loads(response.read().decode("utf-8"))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=2)

    assert response.status == 202
    assert created["ok"] is True
    assert calls and calls[0]["symbol"] == "SOLUSDT"
    provider_results = created["simulation_run"]["payload"]["provider_results"]
    assert provider_results[0]["status"] == "OK"
    assert "provider_fetcher_missing" not in json.dumps(created)


def test_api_simulation_live_collect_requires_configured_target_jobs(tmp_path: Path, monkeypatch: Any) -> None:
    store = _store(tmp_path)
    monkeypatch.delenv("ARBITRAGE_PROVIDER_JOBS_JSON", raising=False)
    server = create_server("127.0.0.1", 0, store=store)
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        body = json.dumps({"bounded_live_collect": True})
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/arbitrage/simulation-runs", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=2)

    assert response.status == 400
    assert payload == {"error": "provider_jobs_required"}
    assert "provider_fetcher_missing" not in json.dumps(payload)
    assert _table_rows(store, "arb_dead_letters") == []


def test_provider_job_api_and_snapshot_redact_configured_secrets(tmp_path: Path, monkeypatch: Any) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv(
        "ARBITRAGE_PROVIDER_JOBS_JSON",
        json.dumps(
            [
                {
                    "provider_key": "binance_public",
                    "capability": "cex_orderbook",
                    "scope_key": "binance:SOLUSDT",
                    "symbol": "SOLUSDT",
                    "headers": {"Authorization": "Bearer plain-secret-12345"},
                    "params": {"api_key": "plain-api-key-12345"},
                    "token": "plain-token-12345",
                    "headerValue": "plain-unpatterned-secret-12345",
                }
            ]
        ),
    )
    server = create_server("127.0.0.1", 0, store=store)
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/arbitrage/provider-jobs")
        jobs_response = conn.getresponse()
        jobs_payload = json.loads(jobs_response.read().decode("utf-8"))
        conn.close()

        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/arbitrage/snapshot")
        snapshot_response = conn.getresponse()
        snapshot_payload = json.loads(snapshot_response.read().decode("utf-8"))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=2)

    assert jobs_response.status == 200
    assert snapshot_response.status == 200
    serialized = json.dumps({"jobs": jobs_payload, "snapshot": snapshot_payload}, ensure_ascii=False)
    assert "plain-secret-12345" not in serialized
    assert "plain-api-key-12345" not in serialized
    assert "plain-token-12345" not in serialized
    assert "plain-unpatterned-secret-12345" not in serialized
    assert "headerValue" not in serialized
    assert "headers" not in serialized
    assert "params" not in serialized


def test_simulation_response_redacts_inline_provider_job_secrets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = SimulationRunner(store).start(
        {
            "now_ms": NOW_MS,
            "jobs": [
                {
                    "provider_key": "dexscreener",
                    "capability": "dex_pool_price",
                    "scope_key": "polygon:sol-usdc",
                    "payload": _dex_payload(observed_at_ms=NOW_MS),
                    "headers": {"Authorization": "Bearer inline-secret-12345"},
                    "params": {"api_key": "inline-api-key-12345"},
                    "token": "inline-token-12345",
                    "headerValue": "plain-unpatterned-inline-secret-12345",
                }
            ],
        }
    )

    serialized = json.dumps(
        {"result": result, "stored": store.get_simulation_run(result["simulation_run_id"])},
        ensure_ascii=False,
    )
    assert "inline-secret-12345" not in serialized
    assert "inline-api-key-12345" not in serialized
    assert "inline-token-12345" not in serialized
    assert "plain-unpatterned-inline-secret-12345" not in serialized
    assert "headerValue" not in serialized
    assert "<redacted>" in serialized or "[REDACTED]" in serialized


def test_api_simulation_live_collect_skips_disabled_configured_jobs(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    store = _store(tmp_path)
    calls: list[dict[str, Any]] = []

    class FakeHttpCatalog:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def fetch_payload(self, job: dict[str, Any]) -> dict[str, Any]:
            calls.append(dict(job))
            return _cex_payload(observed_at_ms=NOW_MS + 1_000)

    monkeypatch.setattr(api_server, "ReadOnlyHttpAdapterCatalog", FakeHttpCatalog)
    monkeypatch.setenv(
        "ARBITRAGE_PROVIDER_JOBS_JSON",
        json.dumps(
            [
                {
                    "provider_key": "binance_public",
                    "capability": "cex_orderbook",
                    "scope_key": "binance:SOLUSDT",
                    "symbol": "SOLUSDT",
                    "enabled": False,
                }
            ]
        ),
    )

    server = create_server("127.0.0.1", 0, store=store)
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        body = json.dumps({"now_ms": NOW_MS + 1_000, "bounded_live_collect": True})
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/arbitrage/simulation-runs", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        created = json.loads(response.read().decode("utf-8"))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=2)

    assert response.status == 202
    assert created["ok"] is True
    assert created["status"] == "NO_OPPORTUNITY"
    assert calls == []
    assert created["provider_results"] == []


def test_api_simulation_rejects_private_provider_job_config(tmp_path: Path, monkeypatch: Any) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv(
        "ARBITRAGE_PROVIDER_JOBS_JSON",
        json.dumps([{"provider_key": "zerox", "capability": "swap_build_tx", "scope_key": "private"}]),
    )
    server = create_server("127.0.0.1", 0, store=store)
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        body = json.dumps({"bounded_live_collect": True})
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/arbitrage/simulation-runs", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=2)

    assert response.status == 400
    assert payload == {"error": "invalid_provider_jobs_config"}
    assert _table_rows(store, "arb_simulation_runs") == []


def test_api_simulation_rejects_live_collect_job_without_http_adapter(tmp_path: Path, monkeypatch: Any) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv(
        "ARBITRAGE_PROVIDER_JOBS_JSON",
        json.dumps([{"provider_key": "coingecko", "capability": "coin_price", "scope_key": "SOL"}]),
    )
    server = create_server("127.0.0.1", 0, store=store)
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        body = json.dumps({"bounded_live_collect": True})
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/arbitrage/simulation-runs", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=2)

    assert response.status == 400
    assert payload == {"error": "invalid_provider_jobs_config"}
    assert _table_rows(store, "arb_simulation_runs") == []


def test_demo_sol_simulation_reaches_paper_execution_with_fresh_simulation_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seeded = seed_demo_sol_opportunity(store)

    result = SimulationRunner(store).start(
        {
            "opportunity_id": seeded["opportunity_id"],
            "route_id": seeded["route_id"],
            "requested_by": "demo-smoke",
            "trade_amount_krw": 99_862,
        }
    )

    assert result["ok"] is True
    assert result["status"] == "COMPLETED"
    assert result["run_id"] > 0
    assert result["stage_status"]["paper_execution"] == "COMPLETED"
    assert result["paper_result"]["run"]["status"] == "SETTLED"
    assert result["blockers"] == []
    assert result["no_real_funds"] is True
    assert result["no_real_submit"] is True


def test_simulation_preserves_original_stale_exit_quote_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seeded = seed_demo_sol_opportunity(store)
    route_id = seeded["route_id"]
    future_stamp = seeded["quote_fresh_until_ms"] + 120_000
    with store.conn() as conn:
        before = dict(
            conn.execute(
                "SELECT * FROM arb_route_quotes WHERE route_id = ? AND source = 'demo_seed' ORDER BY id LIMIT 1",
                (route_id,),
            ).fetchone()
        )

    result = SimulationRunner(store).start(
        {
            "now_ms": future_stamp,
            "opportunity_id": seeded["opportunity_id"],
            "route_id": route_id,
            "requested_by": "demo-stale-quote",
            "trade_amount_krw": 99_862,
        }
    )

    assert result["ok"] is True
    with store.conn() as conn:
        after = dict(conn.execute("SELECT * FROM arb_route_quotes WHERE id = ?", (before["id"],)).fetchone())
        synthetic = conn.execute(
            "SELECT * FROM arb_route_quotes WHERE route_id = ? AND source = 'no_funds_simulation'",
            (route_id,),
        ).fetchall()
    assert after["observed_at_ms"] == before["observed_at_ms"]
    assert after["expires_at_ms"] == before["expires_at_ms"]
    assert after["stale"] == before["stale"]
    assert synthetic


def test_simulation_promoted_route_cannot_be_used_for_edge_gated_execution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seeded = seed_demo_sol_opportunity(store)
    simulation = SimulationRunner(store).start(
        {
            "opportunity_id": seeded["opportunity_id"],
            "route_id": seeded["route_id"],
            "requested_by": "demo-edge-gate",
            "trade_amount_krw": 99_862,
        }
    )
    assert simulation["ok"] is True
    with store.conn() as conn:
        conn.execute("UPDATE arb_routes SET payload_json = '{}' WHERE id = ?", (seeded["route_id"],))
    store.configure_strategy_profile(
        "default",
        auto_small_enabled=True,
        max_trade_krw=1_000_000,
        max_daily_loss_krw=200_000,
        active=True,
    )
    store.ensure_wallet(
        wallet_key="auto-small-hot-polygon",
        chain_code="POLYGON",
        address="0x9999999999999999999999999999999999999999",
        wallet_type="HOT",
        mode="auto_small",
        enabled=True,
        withdrawal_enabled=False,
    )

    result = ArbitrageEngine(store).start_execution(
        opportunity_id=seeded["opportunity_id"],
        route_id=seeded["route_id"],
        mode="auto_small",
        idempotency_key="simulation-route-auto-small-blocked",
        requested_by="test",
        trade_amount_krw=99_862,
    )

    assert result["ok"] is False
    assert result["run"]["status"] == "BLOCKED"
    assert "simulation_evidence_not_executable" in result["run"]["error_code"]


def test_simulation_run_response_exposes_exact_error_code_and_blockers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = SimulationRunner(store).start(
        {
            "now_ms": NOW_MS,
            "jobs": [
                {
                    "provider_key": "dexscreener",
                    "capability": "dex_pool_price",
                    "scope_key": "polygon:sol-usdc",
                    "payload": _dex_payload(observed_at_ms=NOW_MS - 120_000),
                }
            ],
        }
    )

    assert result["ok"] is True
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "stale_observations_blocked"
    assert result["simulation_run"]["error_code"] == "stale_observations_blocked"
    assert result["blockers"] == ["stale_observations_blocked"]
    api_payload = api_server._simulation_response(store.get_simulation_run(result["simulation_run_id"]) or {})
    assert api_payload["error_code"] == "stale_observations_blocked"
    assert api_payload["blockers"] == ["stale_observations_blocked"]


def test_api_server_serves_built_monitor_and_api_on_one_port(tmp_path: Path) -> None:
    store = _store(tmp_path)
    static_dir = tmp_path / "dist"
    asset_dir = static_dir / "assets"
    asset_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><title>monitor</title><script src=\"/assets/app.js\"></script>", encoding="utf-8")
    (asset_dir / "app.js").write_text("window.__monitor = true;", encoding="utf-8")

    server = create_server("127.0.0.1", 0, store=store, static_dir=str(static_dir))
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/")
        html_response = conn.getresponse()
        html = html_response.read().decode("utf-8")
        conn.close()

        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/assets/app.js")
        asset_response = conn.getresponse()
        asset = asset_response.read().decode("utf-8")
        conn.close()

        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/arbitrage/health")
        api_response = conn.getresponse()
        api_payload = json.loads(api_response.read().decode("utf-8"))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=2)

    assert html_response.status == 200
    assert "text/html" in html_response.getheader("Content-Type")
    assert "no-store" in (html_response.getheader("Cache-Control") or "")
    assert html_response.getheader("Pragma") == "no-cache"
    assert "monitor" in html
    assert asset_response.status == 200
    assert "javascript" in asset_response.getheader("Content-Type")
    assert "no-store" in (asset_response.getheader("Cache-Control") or "")
    assert asset_response.getheader("Pragma") == "no-cache"
    assert "__monitor" in asset
    assert api_response.status == 200
    assert api_payload["service"] == "arbitrage"


def test_api_namespace_does_not_fall_through_to_static_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    static_dir = tmp_path / "dist"
    shadow_dir = static_dir / "api" / "arbitrage"
    shadow_dir.mkdir(parents=True)
    (shadow_dir / "not-real").write_text("shadowed", encoding="utf-8")

    server = create_server("127.0.0.1", 0, store=store, static_dir=str(static_dir))
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/arbitrage/not-real")
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api")
        root_response = conn.getresponse()
        root_payload = json.loads(root_response.read().decode("utf-8"))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=2)

    assert response.status == 404
    assert payload == {"error": "not_found"}
    assert root_response.status == 404
    assert root_payload == {"error": "not_found"}


def test_simulation_stale_observations_block_before_paper_execution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = SimulationRunner(store).start(
        {
            "now_ms": NOW_MS + 120_000,
            "jobs": [
                {
                    "provider_key": "dexscreener",
                    "capability": "dex_pool_price",
                    "scope_key": "polygon:sol-usdc",
                    "payload": _dex_payload(observed_at_ms=NOW_MS),
                },
                {
                    "provider_key": "binance_public",
                    "capability": "cex_orderbook",
                    "scope_key": "binance:solusdt",
                    "payload": _cex_payload(observed_at_ms=NOW_MS),
                },
            ],
        }
    )

    assert result["ok"] is True
    assert result["status"] == "BLOCKED"
    assert result["blockers"] == ["stale_observations_blocked"]
    assert result["run_id"] is None
    assert _table_rows(store, "arb_transactions") == []


def test_simulation_no_opportunity_is_terminal_without_paper_execution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = SimulationRunner(store).start(
        {
            "now_ms": NOW_MS + 1_000,
            "jobs": [
                {
                    "provider_key": "dexscreener",
                    "capability": "dex_pool_price",
                    "scope_key": "polygon:sol-usdc",
                    "payload": _dex_payload(),
                },
            ],
        }
    )

    assert result["ok"] is True
    assert result["status"] == "NO_OPPORTUNITY"
    assert result["run_id"] is None
    assert result["stage_status"]["detect"] == "COMPLETED"


def test_simulation_provider_failure_returns_stage_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = SimulationRunner(store).start(
        {
            "now_ms": NOW_MS + 1_000,
            "jobs": [
                {
                    "provider_key": "dexscreener",
                    "capability": "dex_pool_price",
                    "scope_key": "polygon:sol-usdc",
                    "payload": {"pairs": []},
                },
            ],
        }
    )

    assert result["ok"] is False
    assert result["status"] == "FAILED"
    assert result["simulation_run"]["error_code"] == "provider_collect_failed"
    assert result["stage_status"]["collect"] == "BLOCKED"
    assert result["run_id"] is None


def test_simulation_does_not_treat_unknown_provider_status_as_pass(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = SimulationRunner(store).start(
        {
            "now_ms": NOW_MS + 1_000,
            "deposit_status": "UNKNOWN",
            "jobs": [
                {
                    "provider_key": "dexscreener",
                    "capability": "dex_pool_price",
                    "scope_key": "polygon:sol-usdc",
                    "payload": _dex_payload(),
                },
                {
                    "provider_key": "binance_public",
                    "capability": "cex_orderbook",
                    "scope_key": "binance:solusdt",
                    "payload": _cex_payload(),
                },
            ],
        }
    )

    assert result["ok"] is True
    assert result["status"] == "BLOCKED"
    assert "edge_component_missing:deposit_or_bridge_status" in result["blockers"]
    assert result["run_id"] is None


def test_simulation_api_exposes_provider_jobs_and_run_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = create_server("127.0.0.1", 0, store=store)
    thread = None
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/arbitrage/provider-jobs")
        response = conn.getresponse()
        jobs_payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert "provider_jobs" in jobs_payload
        conn.close()

        body = json.dumps(
            {
                "now_ms": NOW_MS + 1_000,
                "jobs": [
                    {
                        "provider_key": "dexscreener",
                        "capability": "dex_pool_price",
                        "scope_key": "polygon:sol-usdc",
                        "payload": _dex_payload(),
                    },
                    {
                        "provider_key": "binance_public",
                        "capability": "cex_orderbook",
                        "scope_key": "binance:solusdt",
                        "payload": _cex_payload(),
                    },
                ],
            }
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/arbitrage/simulation-runs", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        created = json.loads(response.read().decode("utf-8"))
        assert response.status == 202
        assert created["ok"] is True
        simulation_id = created["simulation_run"]["id"]
        conn.close()

        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", f"/api/arbitrage/simulation-runs/{simulation_id}")
        response = conn.getresponse()
        fetched = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert fetched["simulation_run"]["status"] == "COMPLETED"
        assert fetched["stage_status"]["paper_execution"] == "COMPLETED"
        assert fetched["no_real_funds"] is True
        assert fetched["no_real_submit"] is True
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=2)
