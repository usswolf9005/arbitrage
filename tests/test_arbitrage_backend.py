import http.client
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from arbitrage.api_server import create_server, encode_sse_event
from arbitrage.bridge_submit import (
    BRIDGE_SUBMIT_CAPABILITIES,
    CAPABILITY_BRIDGE_STATUS,
    BridgeSubmitRequest,
    DeterministicBridgeSubmitAdapter,
)
from arbitrage.cex_trade import (
    CAPABILITY_CEX_DEPOSIT_STATUS,
    CAPABILITY_CEX_ORDER_RECONCILE,
    CAPABILITY_CEX_ORDER_SUBMIT,
    CEX_TRADE_CAPABILITIES,
    CexTradeRequest,
    DeterministicCexTradeAdapter,
)
from arbitrage.demo_seed import DEMO_SOL_ID, seed_demo_sol_opportunity
from arbitrage.dex_submit import DEX_SWAP_SUBMIT_CAPABILITIES, DexSwapRequest, DexSwapSubmitResult, DryRunDexSwapAdapter
from arbitrage.engine import ArbitrageEngine
from arbitrage.live_full_execution import LiveFullBridgeCexRunner
from arbitrage.paper_execution import PaperExecutionRunner, ROUTE_STEPS
from arbitrage.providers.base import CAPABILITY_SWAP_BUILD_TX, CAPABILITY_SWAP_QUOTE
from arbitrage.store import ArbitrageStore


LIVE_TRADE_KRW = 100_000


def _store(path: str) -> ArbitrageStore:
    store = ArbitrageStore(path)
    store.init()
    return store


def _seed_live_route(store: ArbitrageStore, *, route_type: str = "same_dex_sell") -> dict:
    asset_id = store.ensure_asset(symbol="SOL", name="Solana")
    token_id = store.ensure_token(
        asset_id=asset_id,
        chain_id="137",
        chain_code="POLYGON",
        contract_address="0x1111111111111111111111111111111111111111",
        decimals=18,
    )
    assert token_id > 0
    buy_venue_id = store.ensure_venue("QUICKSWAP", "DEX", "QuickSwap")
    sell_venue_id = store.ensure_venue("UPBIT", "CEX", "Upbit")
    buy_market_id = store.ensure_market(
        market_key="POLYGON:QUICKSWAP:SOL-USDC:0xpoolbuy",
        asset_id=asset_id,
        venue_id=buy_venue_id,
        market_type="DEX_POOL",
        chain_code="POLYGON",
        pool_address="0x2222222222222222222222222222222222222222",
        quote_asset="USDC",
    )
    sell_market_id = store.ensure_market(
        market_key="UPBIT:SOL-KRW",
        asset_id=asset_id,
        venue_id=sell_venue_id,
        market_type="CEX_ORDERBOOK",
        chain_code="KRW",
        market_symbol="SOL/KRW",
        quote_asset="KRW",
        deposit_network="POLYGON",
    )
    now_ms = int(time.time() * 1000)
    store.record_market_tick(
        market_id=buy_market_id,
        source="dexscreener",
        observed_at_ms=now_ms,
        raw_price=70,
        price_usd=70,
        price_krw=98000,
        best_ask=70,
        liquidity_usd=1_000_000,
    )
    store.record_orderbook_snapshot(
        market_id=sell_market_id,
        source="upbit_ws",
        observed_at_ms=now_ms,
        best_bid=115000,
        best_ask=116000,
        depth=[{"price": 115000, "quantity": 30}],
    )
    opportunity_id = store.upsert_opportunity(
        opportunity_key="SOL:POLYGON:QUICKSWAP:UPBIT:bucket1",
        asset_id=asset_id,
        anomaly_type="dex_cex_spread",
        lifecycle_status="PRECHECK_PASS",
        safety_status="PASS",
        buy_market_id=buy_market_id,
        sell_market_id=sell_market_id,
        spread_bps=1700,
        edge_expected_bps=1200,
        edge_worst_bps=900,
        first_seen_at_ms=now_ms,
        last_seen_at_ms=now_ms,
    )
    route_id = store.upsert_route(
        route_key=f"SOL:{route_type}:bucket1",
        opportunity_id=opportunity_id,
        route_type=route_type,
        buy_market_id=buy_market_id,
        sell_market_id=sell_market_id,
        safety_status="PASS",
        route_status="OPEN",
        edge_expected_bps=1200,
        edge_worst_bps=900,
        selected=True,
        quote_fresh_until_ms=now_ms + 30_000,
        edge_worst_verified=True,
    )
    store.record_route_quote(
        route_id=route_id,
        leg_type="exit",
        source="upbit_orderbook",
        destination="UPBIT",
        amount_in_raw="1000000000000000000",
        amount_out_expected_krw=115000,
        amount_out_min_krw=109000,
        observed_at_ms=now_ms,
        expires_at_ms=now_ms + 30_000,
    )
    return {
        "asset_id": asset_id,
        "buy_market_id": buy_market_id,
        "sell_market_id": sell_market_id,
        "opportunity_id": opportunity_id,
        "route_id": route_id,
        "now_ms": now_ms,
    }


def _arm_live_execution(store: ArbitrageStore, route_id: int, *, mode: str = "live_full") -> None:
    store.configure_strategy_profile(
        "default",
        live_full_enabled=True,
        max_trade_krw=1_000_000,
        max_daily_loss_krw=200_000,
    )
    store.ensure_wallet(
        wallet_key="paper-hot-polygon",
        chain_code="POLYGON",
        address="0x9999999999999999999999999999999999999999",
        wallet_type="HOT",
        mode=mode,
        enabled=True,
        withdrawal_enabled=False,
    )
    fresh_until = int(time.time() * 1000) + 30_000
    freshness = {
        "buy_quote": fresh_until,
        "sell_quote": fresh_until,
        "orderbook": fresh_until,
        "fx": fresh_until,
        "rpc_block": fresh_until,
    }
    route = store.get_route(route_id) or {}
    if route.get("route_type") in {"direct_cex_sell", "bridge_cex_sell"}:
        freshness["deposit_status"] = fresh_until
    if route.get("route_type") in {"bridge_dex_sell", "bridge_cex_sell"}:
        freshness["bridge_quote"] = fresh_until
        freshness["bridge_status"] = fresh_until
    store.set_route_freshness(route_id, freshness)


def _approve_live_full(
    store: ArbitrageStore,
    *,
    opportunity_id: int,
    route_id: int,
    amount_krw: float = LIVE_TRADE_KRW,
    expires_delta_ms: int = 30_000,
    approval_key: str = "live-full-approval",
) -> dict:
    approval = store.request_operator_approval(
        approval_key=approval_key,
        opportunity_id=opportunity_id,
        route_id=route_id,
        mode="live_full",
        requested_by="ops",
        reason="live_full route approval",
        payload={
            "trade_amount_krw": amount_krw,
            "expires_at_ms": int(time.time() * 1000) + expires_delta_ms,
        },
    )
    return store.decide_operator_approval(approval["id"], status="APPROVED", operator="ops")


def _arm_one_click_execution(store: ArbitrageStore, route_id: int) -> None:
    store.configure_strategy_profile(
        "default",
        one_click_enabled=True,
        max_trade_krw=1_000_000,
        max_daily_loss_krw=200_000,
    )
    store.ensure_wallet(
        wallet_key="one-click-hot-polygon",
        chain_code="POLYGON",
        address="0x9999999999999999999999999999999999999999",
        wallet_type="HOT",
        mode="one_click",
        enabled=True,
        withdrawal_enabled=False,
    )
    fresh_until = int(time.time() * 1000) + 30_000
    store.set_route_freshness(
        route_id,
        {
            "buy_quote": fresh_until,
            "sell_quote": fresh_until,
            "orderbook": fresh_until,
            "fx": fresh_until,
            "rpc_block": fresh_until,
            "bridge_quote": fresh_until,
            "bridge_status": fresh_until,
            "deposit_status": fresh_until,
        },
    )


def _arm_auto_small_execution(store: ArbitrageStore, route_id: int) -> None:
    store.configure_strategy_profile(
        "default",
        auto_small_enabled=True,
        max_trade_krw=1_000_000,
        max_daily_loss_krw=200_000,
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
    fresh_until = int(time.time() * 1000) + 30_000
    store.set_route_freshness(
        route_id,
        {
            "buy_quote": fresh_until,
            "sell_quote": fresh_until,
            "rpc_block": fresh_until,
        },
    )


def _set_route_payload(store: ArbitrageStore, route_id: int, payload: dict) -> None:
    with store.conn() as conn:
        conn.execute(
            "UPDATE arb_routes SET payload_json = ?, updated_at_ms = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), int(time.time() * 1000), int(route_id)),
        )


def _start_server(store: ArbitrageStore):
    server = create_server("127.0.0.1", 0, store=store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _table_count(store: ArbitrageStore, table: str) -> int:
    with store.conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        return int(row["count"])


def test_schema_init_is_idempotent_and_declares_core_constraints():
    with TemporaryDirectory() as td:
        db_path = str(Path(td) / "arbitrage.db")
        store = _store(db_path)
        asset_id = store.ensure_asset(symbol="SOL", name="Solana")
        first_token = store.ensure_token(
            asset_id=asset_id,
            chain_id="137",
            chain_code="POLYGON",
            contract_address="0x1111111111111111111111111111111111111111",
            decimals=18,
        )

        store.init()
        second_token = store.ensure_token(
            asset_id=asset_id,
            chain_id="137",
            chain_code="POLYGON",
            contract_address="0x1111111111111111111111111111111111111111",
            decimals=18,
        )

        assert second_token == first_token
        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'arb_%'"
                )
            }
            assert {
                "arb_assets",
                "arb_tokens",
                "arb_markets",
                "arb_market_ticks",
                "arb_opportunities",
                "arb_routes",
                "arb_precheck_results",
                "arb_execution_runs",
                "arb_execution_steps",
                "arb_orders",
                "arb_transactions",
                "arb_event_log",
                "arb_dead_letters",
            }.issubset(tables)
            indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(arb_execution_runs)").fetchall()
            }
            assert "sqlite_autoindex_arb_execution_runs_2" in indexes
            tx_indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(arb_transactions)").fetchall()
            }
            assert "sqlite_autoindex_arb_transactions_1" in tx_indexes


def test_duplicate_opportunity_and_execution_requests_are_idempotent():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        duplicate_id = store.upsert_opportunity(
            opportunity_key="SOL:POLYGON:QUICKSWAP:UPBIT:bucket1",
            asset_id=seeded["asset_id"],
            anomaly_type="dex_cex_spread",
            lifecycle_status="PRECHECK_PASS",
            safety_status="PASS",
            buy_market_id=seeded["buy_market_id"],
            sell_market_id=seeded["sell_market_id"],
            spread_bps=1800,
            edge_expected_bps=1300,
            edge_worst_bps=1000,
            first_seen_at_ms=seeded["now_ms"],
            last_seen_at_ms=seeded["now_ms"] + 1000,
        )
        _arm_live_execution(store, seeded["route_id"])
        approved = _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="live-full-idempotency-approval",
        )
        engine = ArbitrageEngine(store)

        first = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-sol-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        second = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-sol-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert duplicate_id == seeded["opportunity_id"]
        assert first["ok"] is True
        assert first["run"]["payload"]["approval"]["approval_id"] == approved["id"]
        assert second["existing"] is True
        assert second["run"]["id"] == first["run"]["id"]
        steps = store.fetch_execution_steps(first["run"]["id"])
        assert [row["step_key"] for row in steps] == [
            "precheck",
            "dex_buy",
            "exit_route_select",
            "cex_deposit",
            "cex_sell",
            "settle",
        ]


def test_execution_run_idempotency_claim_is_atomic_and_keeps_first_scope():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)

        first = store.insert_execution_run(
            execution_key="claim:first",
            idempotency_key="atomic-run-claim",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            status="ENTERING",
            requested_by="first",
            payload={"trade_amount_krw": 100_000, "owner": "first"},
        )
        duplicate = store.insert_execution_run(
            execution_key="claim:second",
            idempotency_key="atomic-run-claim",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            status="BLOCKED",
            requested_by="second",
            payload={"trade_amount_krw": 200_000, "owner": "second"},
        )

        assert first["created"] is True
        assert duplicate["created"] is False
        assert duplicate["id"] == first["id"]
        assert duplicate["execution_key"] == "claim:first"
        assert duplicate["status"] == "ENTERING"
        assert duplicate["requested_by"] == "first"
        assert duplicate["payload"] == {"trade_amount_krw": 100_000, "owner": "first"}


def test_dry_run_dex_swap_adapter_records_idempotent_synthetic_transaction():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = seed_demo_sol_opportunity(store)
        route = store.get_route(seeded["route_id"])
        run = store.insert_execution_run(
            execution_key="auto-small-dry-run-adapter-contract",
            idempotency_key="auto-small-dry-run-adapter-contract",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            status="ENTERING",
            requested_by="test",
            payload={"dry_run": True, "route_type": "same_dex_sell"},
        )
        step_id = store.insert_execution_step(run_id=run["id"], step_key="dex_buy", status="PENDING")
        adapter = DryRunDexSwapAdapter()
        request = DexSwapRequest(
            route_id=seeded["route_id"],
            opportunity_id=seeded["opportunity_id"],
            chain="POLYGON",
            buy_market=store.get_market_detail(route["buy_market_id"]),
            sell_market=store.get_market_detail(route["sell_market_id"]),
            token_ca=seeded["token_ca"],
            pool_ca=seeded["buy_pool_ca"],
            amount_krw=99_862,
            slippage_bps=150,
            idempotency_key="dex-buy-dry-run-1",
            step_key="dex_buy",
        )

        quote = adapter.quote(request)
        build = adapter.build(request, quote)
        result = adapter.submit(request, build)
        duplicate = adapter.submit(request, build)
        reconciled = adapter.reconcile(request, result)
        unknown_request = DexSwapRequest(
            route_id=seeded["route_id"],
            opportunity_id=seeded["opportunity_id"],
            chain="POLYGON",
            buy_market=store.get_market_detail(route["buy_market_id"]),
            sell_market=store.get_market_detail(route["sell_market_id"]),
            token_ca=seeded["token_ca"],
            pool_ca=seeded["buy_pool_ca"],
            amount_krw=99_862,
            slippage_bps=150,
            idempotency_key="dex-buy-dry-run-unknown",
            step_key="dex_buy",
            payload={"dry_run_simulation": {"unknown_outcome_steps": ["dex_buy"]}},
        )
        unknown_quote = adapter.quote(unknown_request)
        unknown_build = adapter.build(unknown_request, unknown_quote)
        unknown_result = adapter.submit(unknown_request, unknown_build)

        assert adapter.dry_run is True
        assert adapter.capabilities == DEX_SWAP_SUBMIT_CAPABILITIES
        assert CAPABILITY_SWAP_QUOTE in adapter.capabilities
        assert CAPABILITY_SWAP_BUILD_TX in adapter.capabilities
        assert quote.status == "success"
        assert build.status == "success"
        assert result.status == "success"
        assert result.dry_run is True
        assert result.tx_hash.startswith("dryrun_")
        assert result.tx_hash == duplicate.tx_hash
        assert result.submit_ref.startswith("dryrun_submit_")
        assert result.quote_evidence["adapter_capabilities"] == list(adapter.capabilities)
        assert result.payload_evidence["adapter_capabilities"] == list(adapter.capabilities)
        assert "provider_key" not in result.payload_evidence
        assert result.payload_evidence["external_submission"] is False
        assert result.payload_evidence["real_chain_state"] is False
        assert reconciled.status == "success"
        assert reconciled.evidence["real_chain_state"] is False
        assert unknown_result.status == "unknown"
        assert unknown_result.payload_evidence["simulated_outcome"] is True
        assert unknown_result.payload_evidence["real_chain_state"] is False

        tx = store.record_dry_run_transaction(
            chain_id=request.chain,
            tx_hash=result.tx_hash,
            run_id=run["id"],
            step_id=step_id,
            tx_type="dex_swap",
            adapter_name=result.adapter_name,
            submit_ref=result.submit_ref,
            payload=result.to_dict(),
        )
        duplicate_tx = store.record_dry_run_transaction(
            chain_id=request.chain,
            tx_hash=result.tx_hash,
            run_id=run["id"],
            step_id=step_id,
            tx_type="dex_swap",
            adapter_name=result.adapter_name,
            submit_ref=result.submit_ref,
            payload=result.to_dict(),
        )
        rows = store.fetch_transactions_for_run_step(run["id"], "dex_buy")

        assert duplicate_tx["id"] == tx["id"]
        assert len(rows) == 1
        assert rows[0]["status"] == "DRY_RUN_SUCCESS"
        assert rows[0]["submitted_at_ms"] is None
        assert rows[0]["confirmed_at_ms"] is None
        assert rows[0]["payload"]["dry_run"] is True
        assert rows[0]["payload"]["synthetic"] is True
        assert rows[0]["payload"]["real_chain_state"] is False
        with pytest.raises(ValueError, match="dry_run_transaction_cannot_claim_chain_state"):
            store.upsert_transaction(
                chain_id=request.chain,
                tx_hash="dryrun_invalid_confirmed",
                run_id=run["id"],
                step_id=step_id,
                tx_type="dex_swap",
                status="CONFIRMED",
                payload={"dry_run": True},
            )


def test_bridge_and_cex_adapters_record_idempotent_transfers_and_orders_without_secrets():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="bridge_cex_sell")
        route = store.get_route(seeded["route_id"])
        run = store.insert_execution_run(
            execution_key="bridge-cex-adapter-contract",
            idempotency_key="bridge-cex-adapter-contract",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            status="ENTERING",
            requested_by="test",
            payload={"route_type": "bridge_cex_sell", "simulated": True},
        )
        step_ids = {
            step_key: store.insert_execution_step(run_id=run["id"], step_key=step_key, status="PENDING")
            for step_key in ROUTE_STEPS["bridge_cex_sell"]
        }
        buy_market = store.get_market_detail(route["buy_market_id"])
        sell_market = store.get_market_detail(route["sell_market_id"])
        route_payload = {
            "bridge_simulation": {"status_by_step": {"bridge": "unknown"}},
            "cex_simulation": {"order_reconcile_status_by_step": {"cex_sell": "partial"}},
            "api_key": "nested-raw-secret",
        }

        bridge_adapter = DeterministicBridgeSubmitAdapter()
        bridge_request = BridgeSubmitRequest(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            run_id=run["id"],
            step_key="bridge",
            route_type="bridge_cex_sell",
            source_chain="POLYGON",
            destination_chain="ARBITRUM",
            source_venue=buy_market["venue"],
            destination_venue=sell_market["venue"],
            token_ca=buy_market["token_ca"],
            pool_ca=buy_market["pool_ca"],
            cex_market=sell_market["market"],
            deposit_network=sell_market["deposit_network"],
            amount_krw=LIVE_TRADE_KRW,
            slippage_bps=150,
            idempotency_key="bridge-adapter-contract",
            provider_refs={"quote_ref": "lifi-quote-1", "api_key": "raw-secret"},
            payload={"route_payload": route_payload},
        )
        bridge_quote = bridge_adapter.quote(bridge_request)
        bridge_build = bridge_adapter.build(bridge_request, bridge_quote)
        bridge_submit = bridge_adapter.submit(bridge_request, bridge_build)
        bridge_status = bridge_adapter.status(bridge_request, bridge_submit)
        bridge_reconcile = bridge_adapter.reconcile(bridge_request, bridge_submit)

        assert bridge_adapter.dry_run is True
        assert bridge_adapter.simulated is True
        assert bridge_adapter.capabilities == BRIDGE_SUBMIT_CAPABILITIES
        assert CAPABILITY_BRIDGE_STATUS in bridge_adapter.capabilities
        assert bridge_quote.status == "success"
        assert bridge_build.status == "success"
        assert bridge_submit.status == "success"
        assert bridge_status.status == "unknown"
        assert bridge_status.terminal is False
        assert bridge_reconcile.status == "unknown"
        assert bridge_submit.submit_ref.startswith("bridge_submit_")
        assert bridge_submit.bridge_ref.startswith("bridge_quote_")
        assert bridge_submit.payload_evidence["network_calls"] == 0
        assert bridge_submit.payload_evidence["requires_secret"] is False
        assert bridge_submit.payload_evidence["adapter_capabilities"] == list(BRIDGE_SUBMIT_CAPABILITIES)
        bridge_evidence_json = json.dumps(bridge_reconcile.to_dict(), sort_keys=True)
        assert "raw-secret" not in bridge_evidence_json
        assert "nested-raw-secret" not in bridge_evidence_json

        for simulated_status in ("success", "pending", "partial", "failed", "unknown"):
            status_request = BridgeSubmitRequest(
                opportunity_id=seeded["opportunity_id"],
                route_id=seeded["route_id"],
                run_id=run["id"],
                step_key="bridge",
                route_type="bridge_cex_sell",
                source_chain="POLYGON",
                destination_chain="ARBITRUM",
                token_ca=buy_market["token_ca"],
                amount_krw=LIVE_TRADE_KRW,
                slippage_bps=150,
                idempotency_key=f"bridge-sim-{simulated_status}",
                payload={"bridge_simulation": {"submit_status": simulated_status}},
            )
            status_quote = bridge_adapter.quote(status_request)
            status_build = bridge_adapter.build(status_request, status_quote)
            assert bridge_adapter.submit(status_request, status_build).status == simulated_status

        bridge_transfer = store.upsert_transfer(
            transfer_key=f"bridge:{run['id']}:bridge",
            run_id=run["id"],
            step_id=step_ids["bridge"],
            from_location="POLYGON",
            to_location="ARBITRUM",
            status=f"SIMULATED_{bridge_submit.status}",
            amount_raw="",
            payload={**bridge_submit.to_dict(), "api_secret": "raw-secret"},
        )
        duplicate_bridge_transfer = store.upsert_transfer(
            transfer_key=f"bridge:{run['id']}:bridge",
            run_id=run["id"],
            step_id=step_ids["bridge"],
            from_location="POLYGON",
            to_location="ARBITRUM",
            status=f"SIMULATED_{bridge_submit.status}",
            amount_raw="",
            payload={**bridge_submit.to_dict(), "api_secret": "raw-secret"},
        )
        assert duplicate_bridge_transfer["id"] == bridge_transfer["id"]
        assert store.fetch_transfers_for_run_step(run["id"], "bridge")[0]["payload"]["api_secret"] == "[REDACTED]"

        cex_adapter = DeterministicCexTradeAdapter()
        cex_request = CexTradeRequest(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            run_id=run["id"],
            step_key="cex_sell",
            route_type="bridge_cex_sell",
            source_chain="ARBITRUM",
            destination_chain="KRW",
            source_venue=buy_market["venue"],
            destination_venue=sell_market["venue"],
            cex_market=sell_market["market"],
            deposit_network=sell_market["deposit_network"],
            token_ca=buy_market["token_ca"],
            pool_ca=buy_market["pool_ca"],
            amount_krw=LIVE_TRADE_KRW,
            slippage_bps=150,
            idempotency_key="cex-adapter-contract",
            provider_refs={"deposit_ref": "upbit-deposit-1", "secret": "raw-secret"},
            payload={"route_payload": route_payload},
        )
        deposit_status = cex_adapter.deposit_status(cex_request)
        order_submit = cex_adapter.submit_order(cex_request)
        order_reconcile = cex_adapter.reconcile_order(cex_request, order_submit)

        assert cex_adapter.dry_run is True
        assert cex_adapter.simulated is True
        assert cex_adapter.capabilities == CEX_TRADE_CAPABILITIES
        assert {
            CAPABILITY_CEX_DEPOSIT_STATUS,
            CAPABILITY_CEX_ORDER_SUBMIT,
            CAPABILITY_CEX_ORDER_RECONCILE,
        }.issubset(cex_adapter.capabilities)
        assert deposit_status.status == "success"
        assert deposit_status.deposit_ref.startswith("cex_deposit_")
        assert order_submit.status == "success"
        assert order_submit.order_ref.startswith("cex_order_")
        assert order_submit.payload_evidence["cex_withdrawal"] is False
        assert order_submit.payload_evidence["external_submission"] is False
        assert order_reconcile.status == "partial"
        assert order_reconcile.filled_amount_krw == LIVE_TRADE_KRW * 0.5
        assert order_reconcile.payload_evidence["adapter_capabilities"] == list(CEX_TRADE_CAPABILITIES)
        cex_evidence_json = json.dumps(order_reconcile.to_dict(), sort_keys=True)
        assert "raw-secret" not in cex_evidence_json
        assert "nested-raw-secret" not in cex_evidence_json

        for simulated_status in ("success", "pending", "partial", "failed", "unknown"):
            status_request = CexTradeRequest(
                opportunity_id=seeded["opportunity_id"],
                route_id=seeded["route_id"],
                run_id=run["id"],
                step_key="cex_sell",
                route_type="bridge_cex_sell",
                source_venue=buy_market["venue"],
                destination_venue=sell_market["venue"],
                cex_market=sell_market["market"],
                deposit_network=sell_market["deposit_network"],
                token_ca=buy_market["token_ca"],
                amount_krw=LIVE_TRADE_KRW,
                slippage_bps=150,
                idempotency_key=f"cex-sim-{simulated_status}",
                payload={
                    "cex_simulation": {
                        "deposit_status": simulated_status,
                        "order_submit_status": simulated_status,
                        "order_reconcile_status": simulated_status,
                    }
                },
            )
            simulated_deposit = cex_adapter.deposit_status(status_request)
            simulated_order = cex_adapter.submit_order(status_request)
            simulated_reconcile = cex_adapter.reconcile_order(status_request, simulated_order)
            assert simulated_deposit.status == simulated_status
            assert simulated_order.status == simulated_status
            assert simulated_reconcile.status == simulated_status

        cex_deposit_transfer = store.upsert_transfer(
            transfer_key=f"cex_deposit:{run['id']}:cex_deposit",
            run_id=run["id"],
            step_id=step_ids["cex_deposit"],
            from_location="ARBITRUM",
            to_location=sell_market["venue"],
            status=f"SIMULATED_{deposit_status.status}",
            amount_raw="",
            payload=deposit_status.to_dict(),
        )
        order = store.upsert_order(
            order_key=f"cex_order:{run['id']}:cex_sell",
            run_id=run["id"],
            step_id=step_ids["cex_sell"],
            venue_code=sell_market["venue"],
            market_key=sell_market["market_key"],
            side="SELL",
            order_type="MARKET",
            amount_value_krw=LIVE_TRADE_KRW,
            avg_price_krw=115000,
            status=f"SIMULATED_{order_reconcile.status}",
            external_order_id=order_submit.order_ref,
            payload={**order_reconcile.to_dict(), "api_key": "raw-secret"},
        )
        duplicate_order = store.upsert_order(
            order_key=f"cex_order:{run['id']}:cex_sell",
            run_id=run["id"],
            step_id=step_ids["cex_sell"],
            venue_code=sell_market["venue"],
            market_key=sell_market["market_key"],
            side="SELL",
            order_type="MARKET",
            amount_value_krw=LIVE_TRADE_KRW,
            avg_price_krw=115000,
            status=f"SIMULATED_{order_reconcile.status}",
            external_order_id=order_submit.order_ref,
            payload={**order_reconcile.to_dict(), "api_key": "raw-secret"},
        )

        assert cex_deposit_transfer["id"] > 0
        assert duplicate_order["id"] == order["id"]
        assert len(store.fetch_transfers_for_run_step(run["id"])) == 2
        assert len(store.fetch_transfers_for_run_step(run["id"], "cex_deposit")) == 1
        assert store.fetch_orders_for_run_step(run["id"], "cex_sell")[0]["payload"]["api_key"] == "[REDACTED]"
        assert store.fetch_orders_for_run_step(run["id"])[0]["external_order_id"] == order_submit.order_ref


def test_auto_small_same_dex_dry_run_advances_steps_transactions_positions_and_is_idempotent():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = seed_demo_sol_opportunity(store)
        _arm_auto_small_execution(store, seeded["route_id"])
        before_seq = store.latest_event_seq()
        engine = ArbitrageEngine(store)

        first = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key="auto-small-same-dex-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert first["ok"] is True
        run = store.get_execution_run(first["run"]["id"])
        assert run["mode"] == "auto_small"
        assert run["status"] == "SETTLED"
        assert run["payload"]["dry_run"] is True
        assert run["payload"]["same_chain_dex_only"] is True
        assert run["payload"]["no_real_submission"] is True
        assert run["payload"]["no_signed_payload"] is True

        steps = store.fetch_execution_steps(run["id"])
        assert [row["step_key"] for row in steps] == ROUTE_STEPS["same_dex_sell"]
        assert {row["status"] for row in steps} == {"COMPLETED"}
        for step in steps:
            assert step["started_at_ms"] is not None
            assert step["completed_at_ms"] is not None
            assert step["duration_ms"] > 0
            assert step["completed_at_ms"] - step["started_at_ms"] == step["duration_ms"]

        dex_buy_txs = store.fetch_transactions_for_run_step(run["id"], "dex_buy")
        dex_sell_txs = store.fetch_transactions_for_run_step(run["id"], "same_dex_sell")
        assert len(dex_buy_txs) == 1
        assert len(dex_sell_txs) == 1
        for tx in [*dex_buy_txs, *dex_sell_txs]:
            assert tx["status"] == "DRY_RUN_SUCCESS"
            assert tx["submitted_at_ms"] is None
            assert tx["confirmed_at_ms"] is None
            assert tx["payload"]["dry_run"] is True
            assert tx["payload"]["dry_run_only"] is True
            assert tx["payload"]["payload_evidence"]["real_chain_state"] is False
            assert tx["payload"]["payload_evidence"]["external_submission"] is False
            assert tx["payload"]["build_evidence"]["signed_payload"] is None
            assert tx["payload"]["build_evidence"]["raw_transaction"] is None

        positions = [row for row in store.fetch_positions() if row["run_id"] == run["id"]]
        assert len(positions) == 1
        assert positions[0]["status"] == "SETTLED"
        assert positions[0]["payload"]["mode"] == "auto_small"
        assert positions[0]["payload"]["dry_run"] is True
        assert positions[0]["payload"]["not_live_trading"] is True
        marks = store.fetch_position_marks(positions[0]["id"])
        assert marks[-1]["route_status"]["dry_run"] is True

        snapshot = engine.snapshot(selected_opportunity_id=seeded["opportunity_id"])
        assert snapshot["selected_execution_run"]["id"] == run["id"]
        assert len(snapshot["transactions"]) == 2
        assert {tx["payload"]["step_key"] for tx in snapshot["transactions"]} == {"dex_buy", "same_dex_sell"}
        assert all(tx["payload"]["dry_run"] is True for tx in snapshot["transactions"])

        assert _table_count(store, "arb_orders") == 0
        assert _table_count(store, "arb_transfers") == 0
        replay = list(reversed(store.fetch_event_log(after_seq=before_seq, limit=500)))
        event_types = {row["event_type"] for row in replay}
        assert {
            "execution.step.started",
            "execution.step.completed",
            "execution.log.append",
            "flow.node.update",
            "flow.edge.update",
            "position.update",
        }.issubset(event_types)

        counts = {
            "steps": _table_count(store, "arb_execution_steps"),
            "events": _table_count(store, "arb_event_log"),
            "transactions": _table_count(store, "arb_transactions"),
            "positions": _table_count(store, "arb_positions"),
            "marks": _table_count(store, "arb_position_marks"),
        }
        second = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key="auto-small-same-dex-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert second["existing"] is True
        assert second["run"]["id"] == run["id"]
        assert _table_count(store, "arb_execution_steps") == counts["steps"]
        assert _table_count(store, "arb_event_log") == counts["events"]
        assert _table_count(store, "arb_transactions") == counts["transactions"]
        assert _table_count(store, "arb_positions") == counts["positions"]
        assert _table_count(store, "arb_position_marks") == counts["marks"]


@pytest.mark.parametrize("route_type", ["bridge_dex_sell", "bridge_cex_sell", "direct_cex_sell"])
def test_auto_small_rejects_bridge_and_cex_route_types(route_type: str):
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type=route_type)
        _arm_auto_small_execution(store, seeded["route_id"])

        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key=f"auto-small-reject-{route_type}",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        assert result["run"]["status"] == "BLOCKED"
        assert "route_type_not_supported" in result["run"]["error_code"]
        assert store.fetch_execution_steps(result["run"]["id"]) == []
        assert _table_count(store, "arb_orders") == 0
        assert _table_count(store, "arb_transactions") == 0
        assert _table_count(store, "arb_transfers") == 0


def test_auto_small_requires_enabled_caps_trade_amount_wallet_and_fresh_evidence():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = seed_demo_sol_opportunity(store)

        disabled = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key="auto-small-gates-disabled",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert disabled["ok"] is False
        assert disabled["run"]["status"] == "BLOCKED"
        assert "auto_small_disabled" in disabled["run"]["error_code"]
        assert "trade_cap_not_configured" in disabled["run"]["error_code"]
        assert "daily_loss_cap_not_configured" in disabled["run"]["error_code"]
        assert "missing_hot_wallet" in disabled["run"]["error_code"]

        _arm_auto_small_execution(store, seeded["route_id"])
        store.set_route_freshness(seeded["route_id"], {"rpc_block": int(time.time() * 1000) - 1})
        missing_amount = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key="auto-small-gates-missing-amount",
            requested_by="test",
        )

        assert missing_amount["ok"] is False
        assert "trade_amount_missing" in missing_amount["run"]["error_code"]
        assert "edge_component_stale:rpc_freshness" in missing_amount["run"]["error_code"]
        assert "stale_rpc_block" in missing_amount["run"]["error_code"]
        assert store.fetch_execution_steps(missing_amount["run"]["id"]) == []


class UnknownDexBuyDryRunAdapter(DryRunDexSwapAdapter):
    adapter_name = "unknown_dex_buy_dry_run"

    def submit(self, request, build):  # type: ignore[no-untyped-def]
        result = super().submit(request, build)
        if request.step_key != "dex_buy":
            return result
        return DexSwapSubmitResult(
            adapter_name=result.adapter_name,
            dry_run=result.dry_run,
            status="unknown",
            tx_hash=result.tx_hash,
            submit_ref=result.submit_ref,
            gas_krw=result.gas_krw,
            fee_krw=result.fee_krw,
            quote_evidence=result.quote_evidence,
            build_evidence=result.build_evidence,
            payload_evidence={**result.payload_evidence, "status": "unknown"},
        )


class CountingDryRunAdapter(DryRunDexSwapAdapter):
    def __init__(self):
        self.calls: list[str] = []

    def quote(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(f"quote:{request.step_key}")
        return super().quote(request)

    def build(self, request, quote):  # type: ignore[no-untyped-def]
        self.calls.append(f"build:{request.step_key}")
        return super().build(request, quote)

    def submit(self, request, build):  # type: ignore[no-untyped-def]
        self.calls.append(f"submit:{request.step_key}")
        return super().submit(request, build)

    def status(self, request, submit_result):  # type: ignore[no-untyped-def]
        self.calls.append(f"status:{request.step_key}")
        return super().status(request, submit_result)


class WriteTransactionGuardStore(ArbitrageStore):
    WRITE_STATEMENTS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}

    def __init__(self, path: str):
        super().__init__(path)
        self.active_write_transactions = 0

    @contextmanager
    def conn(self):  # type: ignore[no-untyped-def]
        with super().conn() as conn:
            wrote = False

            def trace(statement: str) -> None:
                nonlocal wrote
                head = statement.strip().split(maxsplit=1)[0].upper() if statement.strip() else ""
                if head in self.WRITE_STATEMENTS and not wrote:
                    wrote = True
                    self.active_write_transactions += 1

            conn.set_trace_callback(trace)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)
                if wrote:
                    self.active_write_transactions -= 1


class NoWriteTransactionAdapter(DryRunDexSwapAdapter):
    def __init__(self, store: WriteTransactionGuardStore):
        self.store = store
        self.calls: list[str] = []
        self.max_active_write_transactions = 0

    def _guard(self, phase: str, step_key: str) -> None:
        self.calls.append(f"{phase}:{step_key}")
        self.max_active_write_transactions = max(self.max_active_write_transactions, self.store.active_write_transactions)
        assert self.store.active_write_transactions == 0

    def quote(self, request):  # type: ignore[no-untyped-def]
        self._guard("quote", request.step_key)
        return super().quote(request)

    def build(self, request, quote):  # type: ignore[no-untyped-def]
        self._guard("build", request.step_key)
        return super().build(request, quote)

    def submit(self, request, build):  # type: ignore[no-untyped-def]
        self._guard("submit", request.step_key)
        return super().submit(request, build)

    def status(self, request, submit_result):  # type: ignore[no-untyped-def]
        self._guard("status", request.step_key)
        return super().status(request, submit_result)


class InvalidSubmitPayloadAdapter(DryRunDexSwapAdapter):
    adapter_name = "invalid_submit_payload_dry_run"

    def submit(self, request, build):  # type: ignore[no-untyped-def]
        result = super().submit(request, build)
        return DexSwapSubmitResult(
            adapter_name=result.adapter_name,
            dry_run=result.dry_run,
            status=result.status,
            tx_hash="",
            submit_ref=result.submit_ref,
            gas_krw=result.gas_krw,
            fee_krw=result.fee_krw,
            quote_evidence=result.quote_evidence,
            build_evidence=result.build_evidence,
            payload_evidence=result.payload_evidence,
        )


class CountingBridgeAdapter(DeterministicBridgeSubmitAdapter):
    def __init__(self):
        self.calls: list[str] = []

    def quote(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(f"quote:{request.step_key}")
        return super().quote(request)

    def build(self, request, quote):  # type: ignore[no-untyped-def]
        self.calls.append(f"build:{request.step_key}")
        return super().build(request, quote)

    def submit(self, request, build):  # type: ignore[no-untyped-def]
        self.calls.append(f"submit:{request.step_key}")
        return super().submit(request, build)

    def status(self, request, submit_result):  # type: ignore[no-untyped-def]
        self.calls.append(f"status:{request.step_key}")
        return super().status(request, submit_result)

    def reconcile(self, request, submit_result):  # type: ignore[no-untyped-def]
        self.calls.append(f"reconcile:{request.step_key}")
        return super().reconcile(request, submit_result)


class CountingCexAdapter(DeterministicCexTradeAdapter):
    def __init__(self):
        self.calls: list[str] = []

    def deposit_status(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(f"deposit_status:{request.step_key}")
        return super().deposit_status(request)

    def submit_order(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(f"submit_order:{request.step_key}")
        return super().submit_order(request)

    def reconcile_order(self, request, submit_result):  # type: ignore[no-untyped-def]
        self.calls.append(f"reconcile_order:{request.step_key}")
        return super().reconcile_order(request, submit_result)


class NoWriteBridgeAdapter(DeterministicBridgeSubmitAdapter):
    def __init__(self, store: WriteTransactionGuardStore):
        self.store = store
        self.calls: list[str] = []
        self.max_active_write_transactions = 0

    def _guard(self, phase: str, request) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(f"{phase}:{request.step_key}")
        self.max_active_write_transactions = max(self.max_active_write_transactions, self.store.active_write_transactions)
        assert self.store.active_write_transactions == 0
        assert [row["step_key"] for row in self.store.fetch_execution_steps(request.run_id)] == ROUTE_STEPS[request.route_type]

    def quote(self, request):  # type: ignore[no-untyped-def]
        self._guard("quote", request)
        return super().quote(request)

    def build(self, request, quote):  # type: ignore[no-untyped-def]
        self._guard("build", request)
        return super().build(request, quote)

    def submit(self, request, build):  # type: ignore[no-untyped-def]
        self._guard("submit", request)
        return super().submit(request, build)

    def status(self, request, submit_result):  # type: ignore[no-untyped-def]
        self._guard("status", request)
        return super().status(request, submit_result)


class NoWriteCexAdapter(DeterministicCexTradeAdapter):
    def __init__(self, store: WriteTransactionGuardStore):
        self.store = store
        self.calls: list[str] = []
        self.max_active_write_transactions = 0

    def _guard(self, phase: str, request) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(f"{phase}:{request.step_key}")
        self.max_active_write_transactions = max(self.max_active_write_transactions, self.store.active_write_transactions)
        assert self.store.active_write_transactions == 0
        assert [row["step_key"] for row in self.store.fetch_execution_steps(request.run_id)] == ROUTE_STEPS[request.route_type]

    def deposit_status(self, request):  # type: ignore[no-untyped-def]
        self._guard("deposit_status", request)
        return super().deposit_status(request)

    def submit_order(self, request):  # type: ignore[no-untyped-def]
        self._guard("submit_order", request)
        return super().submit_order(request)

    def reconcile_order(self, request, submit_result):  # type: ignore[no-untyped-def]
        self._guard("reconcile_order", request)
        return super().reconcile_order(request, submit_result)


def test_auto_small_unknown_adapter_status_stops_before_later_steps():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = seed_demo_sol_opportunity(store)
        _arm_auto_small_execution(store, seeded["route_id"])

        result = ArbitrageEngine(store, dex_adapter=UnknownDexBuyDryRunAdapter()).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key="auto-small-unknown-dex-buy",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        run = store.get_execution_run(result["run"]["id"])
        assert run["status"] == "MANUAL_REVIEW"
        steps = {row["step_key"]: row for row in store.fetch_execution_steps(run["id"])}
        assert steps["precheck"]["status"] == "COMPLETED"
        assert steps["dex_buy"]["status"] == "RECONCILE"
        assert steps["dex_buy"]["error_code"] == "auto_small_adapter_submit_unknown"
        assert steps["exit_route_select"]["status"] == "PENDING"
        assert steps["same_dex_sell"]["status"] == "PENDING"
        assert store.fetch_transactions_for_run_step(run["id"], "dex_buy")[0]["status"] == "DRY_RUN_UNKNOWN"
        assert store.fetch_transactions_for_run_step(run["id"], "same_dex_sell") == []
        assert store.fetch_dead_letters()[-1]["reason"] == "auto_small_reconcile_required"
        assert "execution.step.reconcile" in {row["event_type"] for row in store.fetch_event_log(limit=500)}


@pytest.mark.parametrize("unknown_step", ["dex_buy", "same_dex_sell"])
def test_auto_small_route_payload_unknown_outcome_enters_reconcile_and_is_idempotent(unknown_step: str):
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = seed_demo_sol_opportunity(store)
        _arm_auto_small_execution(store, seeded["route_id"])
        route_payload = dict(store.get_route(seeded["route_id"])["payload"])
        route_payload["dry_run_simulation"] = {"unknown_outcome_steps": [unknown_step]}
        _set_route_payload(store, seeded["route_id"], route_payload)
        adapter = CountingDryRunAdapter()
        engine = ArbitrageEngine(store, dex_adapter=adapter)
        idempotency_key = f"auto-small-route-unknown-{unknown_step}"

        result = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key=idempotency_key,
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        run = store.get_execution_run(result["run"]["id"])
        assert run["status"] == "MANUAL_REVIEW"
        steps = {row["step_key"]: row for row in store.fetch_execution_steps(run["id"])}
        assert steps[unknown_step]["status"] == "RECONCILE"
        assert steps[unknown_step]["error_code"] == "auto_small_adapter_submit_unknown"
        later_steps = ROUTE_STEPS["same_dex_sell"][ROUTE_STEPS["same_dex_sell"].index(unknown_step) + 1 :]
        assert all(steps[step_key]["status"] not in {"COMPLETED", "SUCCEEDED"} for step_key in later_steps)

        transactions = store.fetch_transactions_for_run_step(run["id"], unknown_step)
        assert len(transactions) == 1
        assert transactions[0]["status"] == "DRY_RUN_UNKNOWN"
        assert transactions[0]["payload"]["payload_evidence"]["simulated_outcome"] is True
        deadletter = store.fetch_dead_letters()[-1]
        assert deadletter["reason"] == "auto_small_reconcile_required"
        assert deadletter["retryable"] == 0
        assert deadletter["payload"]["step_key"] == unknown_step
        assert deadletter["payload"]["tx_hash"] or deadletter["payload"]["submit_ref"]

        reconcile_events = [
            row for row in store.fetch_event_log(limit=500) if row["event_type"] == "execution.step.reconcile"
        ]
        assert reconcile_events
        event = reconcile_events[0]
        assert event["run_id"] == run["id"]
        assert event["route_id"] == seeded["route_id"]
        assert event["payload"]["run_id"] == run["id"]
        assert event["payload"]["route_id"] == seeded["route_id"]
        assert event["payload"]["step_key"] == unknown_step
        assert event["payload"]["mode"] == "auto_small"
        assert event["payload"]["dry_run"] is True
        assert event["payload"]["run_status"] == "MANUAL_REVIEW"
        assert event["payload"]["error_code"] == "auto_small_adapter_submit_unknown"
        assert event["payload"]["tx_hash"] or event["payload"]["submit_ref"]

        counts = {
            "steps": _table_count(store, "arb_execution_steps"),
            "events": _table_count(store, "arb_event_log"),
            "transactions": _table_count(store, "arb_transactions"),
            "deadletters": _table_count(store, "arb_dead_letters"),
        }
        call_count = len(adapter.calls)

        duplicate = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key=idempotency_key,
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert duplicate["existing"] is True
        assert duplicate["ok"] is False
        assert duplicate["run"]["status"] == "MANUAL_REVIEW"
        assert duplicate["run"]["id"] == run["id"]
        assert len(adapter.calls) == call_count
        assert _table_count(store, "arb_execution_steps") == counts["steps"]
        assert _table_count(store, "arb_event_log") == counts["events"]
        assert _table_count(store, "arb_transactions") == counts["transactions"]
        assert _table_count(store, "arb_dead_letters") == counts["deadletters"]


def test_auto_small_adapter_calls_happen_outside_sqlite_write_transactions():
    with TemporaryDirectory() as td:
        store = WriteTransactionGuardStore(str(Path(td) / "arbitrage.db"))
        store.init()
        seeded = seed_demo_sol_opportunity(store)
        _arm_auto_small_execution(store, seeded["route_id"])
        adapter = NoWriteTransactionAdapter(store)

        result = ArbitrageEngine(store, dex_adapter=adapter).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key="auto-small-no-write-tx-adapter-calls",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is True
        assert store.get_execution_run(result["run"]["id"])["status"] == "SETTLED"
        assert adapter.max_active_write_transactions == 0
        for expected_call in (
            "quote:dex_buy",
            "build:dex_buy",
            "submit:dex_buy",
            "quote:same_dex_sell",
            "build:same_dex_sell",
            "submit:same_dex_sell",
        ):
            assert expected_call in adapter.calls


def test_auto_small_invalid_adapter_payload_is_bounded_deadletter_without_success():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = seed_demo_sol_opportunity(store)
        _arm_auto_small_execution(store, seeded["route_id"])

        result = ArbitrageEngine(store, dex_adapter=InvalidSubmitPayloadAdapter()).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="auto_small",
            idempotency_key="auto-small-invalid-adapter-payload",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        run = store.get_execution_run(result["run"]["id"])
        assert run["status"] == "FAILED"
        assert run["error_code"] == "auto_small_adapter_error"
        steps = {row["step_key"]: row for row in store.fetch_execution_steps(run["id"])}
        assert steps["dex_buy"]["status"] == "FAILED"
        assert steps["dex_buy"]["error_code"] == "auto_small_adapter_error"
        assert steps["exit_route_select"]["status"] == "PENDING"
        assert steps["same_dex_sell"]["status"] == "PENDING"
        assert store.fetch_transactions_for_run_step(run["id"]) == []
        deadletter = store.fetch_dead_letters()[-1]
        assert deadletter["reason"] == "auto_small_adapter_error"
        assert deadletter["retryable"] == 0
        assert deadletter["payload"]["step_key"] == "dex_buy"
        assert "adapter_tx_hash_required" in deadletter["payload"]["error_msg"]


def test_live_full_bridge_unknown_enters_reconcile_without_later_steps_or_duplicate_adapter_calls():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="bridge_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="live-full-bridge-unknown-reconcile",
        )
        _set_route_payload(
            store,
            seeded["route_id"],
            {"bridge_simulation": {"status_by_step": {"bridge": "unknown"}}, "api_key": "raw-secret"},
        )
        bridge_adapter = CountingBridgeAdapter()
        cex_adapter = CountingCexAdapter()
        engine = ArbitrageEngine(store, bridge_adapter=bridge_adapter, cex_adapter=cex_adapter)

        result = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="live-full-bridge-unknown-run",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        run = store.get_execution_run(result["run"]["id"])
        assert run["status"] == "MANUAL_REVIEW"
        steps = {row["step_key"]: row for row in store.fetch_execution_steps(run["id"])}
        assert steps["precheck"]["status"] == "COMPLETED"
        assert steps["dex_buy"]["status"] == "COMPLETED"
        assert steps["bridge"]["status"] == "RECONCILE"
        assert steps["bridge"]["error_code"] == "live_full_adapter_status_unknown"
        assert steps["cex_deposit"]["status"] == "PENDING"
        assert steps["cex_sell"]["status"] == "PENDING"
        assert steps["settle"]["status"] == "PENDING"
        assert len(store.fetch_transfers_for_run_step(run["id"], "bridge")) == 1
        assert store.fetch_transfers_for_run_step(run["id"], "bridge")[0]["status"] == "SIMULATED_UNKNOWN"
        assert store.fetch_orders_for_run_step(run["id"]) == []
        assert cex_adapter.calls == []

        deadletter = store.fetch_dead_letters()[-1]
        assert deadletter["reason"] == "unknown_external_outcome"
        assert deadletter["retryable"] == 0
        assert deadletter["payload"]["run_id"] == run["id"]
        assert deadletter["payload"]["route_id"] == seeded["route_id"]
        assert deadletter["payload"]["step_key"] == "bridge"
        assert deadletter["payload"]["external_ref"]
        assert deadletter["payload"]["error_code"] == "live_full_adapter_status_unknown"

        serialized = json.dumps(
            {
                "steps": steps,
                "transfers": store.fetch_transfers_for_run_step(run["id"]),
                "events": store.fetch_event_log(limit=500),
                "deadletters": store.fetch_dead_letters(),
            },
            sort_keys=True,
        )
        assert "raw-secret" not in serialized
        assert "execution.step.reconcile" in {row["event_type"] for row in store.fetch_event_log(limit=500)}

        counts = {
            "steps": _table_count(store, "arb_execution_steps"),
            "events": _table_count(store, "arb_event_log"),
            "transactions": _table_count(store, "arb_transactions"),
            "transfers": _table_count(store, "arb_transfers"),
            "orders": _table_count(store, "arb_orders"),
            "deadletters": _table_count(store, "arb_dead_letters"),
        }
        bridge_call_count = len(bridge_adapter.calls)
        duplicate = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="live-full-bridge-unknown-run",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert duplicate["existing"] is True
        assert duplicate["ok"] is False
        assert duplicate["run"]["id"] == run["id"]
        assert len(bridge_adapter.calls) == bridge_call_count
        assert _table_count(store, "arb_execution_steps") == counts["steps"]
        assert _table_count(store, "arb_event_log") == counts["events"]
        assert _table_count(store, "arb_transactions") == counts["transactions"]
        assert _table_count(store, "arb_transfers") == counts["transfers"]
        assert _table_count(store, "arb_orders") == counts["orders"]
        assert _table_count(store, "arb_dead_letters") == counts["deadletters"]


def test_live_full_cex_partial_fill_stops_before_settle_with_order_reconcile_evidence():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="live-full-cex-partial-fill",
        )
        _set_route_payload(
            store,
            seeded["route_id"],
            {"cex_simulation": {"order_reconcile_status_by_step": {"cex_sell": "partial"}}},
        )

        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="live-full-cex-partial-fill-run",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        run = store.get_execution_run(result["run"]["id"])
        assert run["status"] == "MANUAL_REVIEW"
        steps = {row["step_key"]: row for row in store.fetch_execution_steps(run["id"])}
        assert steps["cex_deposit"]["status"] == "COMPLETED"
        assert steps["cex_sell"]["status"] == "RECONCILE"
        assert steps["cex_sell"]["error_code"] == "live_full_adapter_order_reconcile_partial"
        assert steps["settle"]["status"] == "PENDING"
        orders = store.fetch_orders_for_run_step(run["id"], "cex_sell")
        assert len(orders) == 1
        assert orders[0]["status"] == "SIMULATED_PARTIAL"
        assert orders[0]["payload"]["filled_amount_krw"] == LIVE_TRADE_KRW * 0.5
        assert orders[0]["payload"]["cex_withdrawal_enabled"] is False
        deadletter = store.fetch_dead_letters()[-1]
        assert deadletter["reason"] == "live_full_reconcile_required"
        assert deadletter["payload"]["order_ref"]


def test_live_full_adapter_calls_happen_after_step_persistence_and_outside_write_transactions():
    with TemporaryDirectory() as td:
        store = WriteTransactionGuardStore(str(Path(td) / "arbitrage.db"))
        store.init()
        seeded = _seed_live_route(store, route_type="bridge_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="live-full-no-write-tx-adapter",
        )
        dex_adapter = NoWriteTransactionAdapter(store)
        bridge_adapter = NoWriteBridgeAdapter(store)
        cex_adapter = NoWriteCexAdapter(store)

        result = ArbitrageEngine(
            store,
            dex_adapter=dex_adapter,
            bridge_adapter=bridge_adapter,
            cex_adapter=cex_adapter,
        ).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="live-full-no-write-tx-adapter-run",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is True
        assert store.get_execution_run(result["run"]["id"])["status"] == "SETTLED"
        assert dex_adapter.max_active_write_transactions == 0
        assert bridge_adapter.max_active_write_transactions == 0
        assert cex_adapter.max_active_write_transactions == 0
        assert "quote:dex_buy" in dex_adapter.calls
        assert {"quote:bridge", "build:bridge", "submit:bridge", "status:bridge"}.issubset(set(bridge_adapter.calls))
        assert {
            "deposit_status:cex_deposit",
            "submit_order:cex_sell",
            "reconcile_order:cex_sell",
        }.issubset(set(cex_adapter.calls))


@pytest.mark.parametrize("route_type", sorted(ROUTE_STEPS))
def test_paper_execution_advances_steps_positions_and_logs_without_submissions(route_type: str):
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type=route_type)
        engine = ArbitrageEngine(store)

        result = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            idempotency_key=f"paper-{route_type}",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is True
        run = store.get_execution_run(result["run"]["id"])
        assert run["mode"] == "paper"
        assert run["status"] == "SETTLED"
        steps = store.fetch_execution_steps(run["id"])
        assert [row["step_key"] for row in steps] == ROUTE_STEPS[route_type]
        assert {row["status"] for row in steps} == {"COMPLETED"}
        for step in steps:
            assert step["started_at_ms"] is not None
            assert step["completed_at_ms"] is not None
            assert step["duration_ms"] > 0
            assert step["completed_at_ms"] - step["started_at_ms"] == step["duration_ms"]

        positions = [row for row in store.fetch_positions() if row["run_id"] == run["id"]]
        assert len(positions) == 1
        assert positions[0]["status"] == "SETTLED"
        assert positions[0]["avg_buy_price_krw"] == LIVE_TRADE_KRW
        assert positions[0]["payload"]["mode"] == "paper"
        assert positions[0]["payload"]["live_exit_estimate_krw"] >= LIVE_TRADE_KRW
        marks = store.fetch_position_marks(positions[0]["id"])
        assert len(marks) >= 2
        assert marks[-1]["route_status"]["mode"] == "paper"

        assert _table_count(store, "arb_orders") == 0
        assert _table_count(store, "arb_transactions") == 0
        assert _table_count(store, "arb_transfers") == 0
        event_types = {row["event_type"] for row in store.fetch_event_log(limit=500)}
        assert "execution.step.started" in event_types
        assert "execution.step.completed" in event_types
        assert "execution.log.append" in event_types
        assert "position.update" in event_types


def test_paper_buy_then_hold_policy_stops_after_buy_with_wallet_hold_flow():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="same_dex_sell")
        engine = ArbitrageEngine(store)

        result = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            idempotency_key="paper-buy-then-hold",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
            execution_policy="buy_then_hold",
        )

        assert result["ok"] is True
        run = store.get_execution_run(result["run"]["id"])
        assert run["mode"] == "paper"
        assert run["status"] == "POSITION_OPEN"
        assert run["payload"]["execution_policy"] == "buy_then_hold"
        assert run["payload"]["stop_after_buy"] is True

        steps = store.fetch_execution_steps(run["id"])
        assert [row["step_key"] for row in steps] == ["precheck", "dex_buy", "wallet_hold"]
        assert {row["status"] for row in steps} == {"COMPLETED"}

        positions = [row for row in store.fetch_positions() if row["run_id"] == run["id"]]
        assert len(positions) == 1
        assert positions[0]["status"] == "OPEN"

        snapshot = engine.snapshot(selected_opportunity_id=seeded["opportunity_id"])
        nodes = {node["id"]: node for node in snapshot["flow_nodes"]}
        edges = {edge["id"]: edge for edge in snapshot["flow_edges"]}
        assert nodes["dexBuy"]["state"] == "done"
        assert nodes["walletHold"]["state"] == "done"
        assert nodes["walletHold"]["step_keys"] == ["wallet_hold"]
        assert edges["buy-wallet-hold"]["state"] == "done"
        assert edges["buy-wallet-hold"]["step_keys"] == ["wallet_hold"]
        assert "sameDexSell" in nodes
        assert nodes["sameDexSell"]["state"] == "skipped"
        assert edges["buy-same"]["state"] == "skipped"


def test_api_execution_accepts_buy_then_hold_policy_for_paper_wallet_hold():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="same_dex_sell")
        server = _start_server(store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request(
                "POST",
                "/api/arbitrage/executions",
                body=json.dumps(
                    {
                        "opportunity_id": seeded["opportunity_id"],
                        "route_id": seeded["route_id"],
                        "mode": "paper",
                        "execution_policy": "buy_then_hold",
                        "idempotency_key": "api-buy-then-hold",
                        "trade_amount_krw": LIVE_TRADE_KRW,
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 202
            body = json.loads(response.read().decode("utf-8"))
            assert body["run"]["status"] == "POSITION_OPEN"
            assert body["run"]["payload"]["execution_policy"] == "buy_then_hold"

            conn.request("GET", f"/api/arbitrage/snapshot?selected_opportunity_id={seeded['opportunity_id']}")
            response = conn.getresponse()
            snapshot = json.loads(response.read().decode("utf-8"))
            nodes = {node["id"]: node for node in snapshot["flow_nodes"]}
            edges = {edge["id"]: edge for edge in snapshot["flow_edges"]}
            assert nodes["walletHold"]["state"] == "done"
            assert edges["buy-wallet-hold"]["state"] == "done"
            assert snapshot["positions"][0]["status"] == "OPEN"
        finally:
            conn.close()
            server.shutdown()
            server.server_close()


def test_duplicate_paper_idempotency_returns_existing_without_duplicate_steps_or_logs():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        engine = ArbitrageEngine(store)

        first = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            idempotency_key="paper-dup-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        step_count = _table_count(store, "arb_execution_steps")
        log_count = _table_count(store, "arb_event_log")
        position_count = _table_count(store, "arb_positions")
        mark_count = _table_count(store, "arb_position_marks")

        second = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            idempotency_key="paper-dup-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert second["existing"] is True
        assert second["run"]["id"] == first["run"]["id"]
        assert _table_count(store, "arb_execution_steps") == step_count
        assert _table_count(store, "arb_event_log") == log_count
        assert _table_count(store, "arb_positions") == position_count
        assert _table_count(store, "arb_position_marks") == mark_count


def test_operator_approval_request_store_is_idempotent_and_logs_once():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)

        first = store.request_operator_approval(
            approval_key="approval-sol-one-click-1",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            requested_by="ops",
            reason="hold private execution until reviewed",
            payload={"edge_worst_bps": 900, "source": "test"},
        )
        second = store.request_operator_approval(
            approval_key="approval-sol-one-click-1",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            requested_by="ops",
            reason="duplicate should be idempotent",
            payload={"edge_worst_bps": 901},
        )

        assert first["created"] is True
        assert second["created"] is False
        assert second["id"] == first["id"]
        approval = store.get_operator_approval(first["id"])
        assert approval["approval_key"] == "approval-sol-one-click-1"
        assert approval["opportunity_id"] == seeded["opportunity_id"]
        assert approval["route_id"] == seeded["route_id"]
        assert approval["run_id"] is None
        assert approval["mode"] == "one_click"
        assert approval["requested_by"] == "ops"
        assert approval["reason"] == "hold private execution until reviewed"
        assert approval["status"] == "PENDING"
        assert approval["requested_at_ms"] is not None
        assert approval["decided_at_ms"] is None
        assert approval["operator"] == ""
        assert approval["payload"] == {"edge_worst_bps": 900, "source": "test"}
        assert store.get_operator_approval_by_key("approval-sol-one-click-1")["id"] == first["id"]

        listed = store.list_operator_approvals(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            status="pending",
        )
        assert [row["id"] for row in listed] == [first["id"]]
        summary = store.summarize_operator_approvals(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
        )
        assert summary["total"] == 1
        assert summary["by_status"] == {"PENDING": 1}
        assert summary["latest"]["id"] == first["id"]

        events = [
            row
            for row in store.fetch_event_log(limit=500)
            if row["event_type"] == "operator_approval.requested"
        ]
        assert len(events) == 1
        event = events[0]
        assert event["seq"] > 0
        assert event["event_id"].startswith("evt_")
        assert event["occurred_at_ms"] is not None
        assert event["opportunity_id"] == seeded["opportunity_id"]
        assert event["route_id"] == seeded["route_id"]
        assert event["payload"]["approval_id"] == first["id"]
        assert _table_count(store, "arb_operator_approvals") == 1
        assert _table_count(store, "arb_orders") == 0
        assert _table_count(store, "arb_transactions") == 0
        assert _table_count(store, "arb_transfers") == 0


def test_operator_approval_persistence_events_and_alerts_redact_sensitive_payloads():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)

        approval = store.request_operator_approval(
            approval_key="approval-redaction",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            requested_by="ops",
            reason="redaction coverage",
            payload={
                "trade_amount_krw": LIVE_TRADE_KRW,
                "expires_at_ms": int(time.time() * 1000) + 30_000,
                "api_key": "raw-secret",
                "nested": {"authorization": "Bearer raw-secret"},
            },
        )
        decided = store.decide_operator_approval(
            approval["id"],
            status="APPROVED",
            operator="ops",
            decision_payload={"ticket": "OPS-1", "signature": "raw-secret"},
        )

        assert store.get_operator_approval(approval["id"])["payload"]["api_key"] == "[REDACTED]"
        assert decided["decision_payload"]["signature"] == "[REDACTED]"
        serialized = json.dumps(
            {
                "approval": store.get_operator_approval(approval["id"]),
                "events": store.fetch_event_log(limit=500),
                "alerts": store.fetch_alerts(channel="db_sse", limit=100),
            },
            sort_keys=True,
        )
        assert "raw-secret" not in serialized
        assert "Bearer raw-secret" not in serialized


def test_paper_runner_rejects_non_paper_mode_without_creating_run():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)

        result = PaperExecutionRunner(store).start(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="paper-mode-reject",
            requested_by="test",
        )

        assert result["ok"] is False
        assert result["error_code"] == "paper_mode_required"
        assert store.get_execution_by_idempotency("paper-mode-reject") is None


def test_paper_unknown_simulated_outcome_moves_run_to_manual_review_without_retry():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)

        result = PaperExecutionRunner(store).start(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            idempotency_key="paper-unknown-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
            simulated_outcomes={"dex_buy": "unknown"},
        )

        assert result["ok"] is False
        run = store.get_execution_run(result["run"]["id"])
        assert run["status"] == "MANUAL_REVIEW"
        assert run["completed_at_ms"] is not None
        steps = store.fetch_execution_steps(run["id"])
        by_key = {row["step_key"]: row for row in steps}
        assert by_key["precheck"]["status"] == "COMPLETED"
        assert by_key["dex_buy"]["status"] == "RECONCILE"
        assert by_key["dex_buy"]["attempt_no"] == 1
        assert [row for row in steps if row["step_key"] == "dex_buy"] == [by_key["dex_buy"]]
        assert by_key["exit_route_select"]["status"] == "PENDING"
        assert store.fetch_dead_letters()[-1]["reason"] == "paper_unknown_outcome"

        duplicate = PaperExecutionRunner(store).start(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            idempotency_key="paper-unknown-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert duplicate["existing"] is True
        assert duplicate["ok"] is False
        assert duplicate["run"]["status"] == "MANUAL_REVIEW"


def test_paper_advance_run_does_not_progress_terminal_run():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        run = store.insert_execution_run(
            execution_key="paper-terminal-advance",
            idempotency_key="paper-terminal-advance",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            status="ENTERING",
            requested_by="test",
        )
        for step_key in ROUTE_STEPS["same_dex_sell"]:
            store.insert_execution_step(run_id=run["id"], step_key=step_key, status="PENDING")
        store.update_execution_run(run["id"], status="ABORTED", error_code="operator_abort")

        result = PaperExecutionRunner(store).advance_run(run["id"])

        assert result["terminal"] is True
        assert result["completed"] is False
        assert result["run"]["status"] == "ABORTED"
        assert {row["status"] for row in store.fetch_execution_steps(run["id"])} == {"PENDING"}


def test_paper_advance_run_does_not_progress_terminal_position_open_run():
    # buy_then_hold paper run은 POSITION_OPEN에서 정상 종료(success terminal)한다.
    # 재진입 시 진행/이벤트 재발행 없이 terminal+completed로 반환해야 한다.
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        run = store.insert_execution_run(
            execution_key="paper-terminal-hold",
            idempotency_key="paper-terminal-hold",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            status="ENTERING",
            requested_by="test",
        )
        for step_key in ROUTE_STEPS["same_dex_sell"]:
            store.insert_execution_step(run_id=run["id"], step_key=step_key, status="PENDING")
        store.update_execution_run(run["id"], status="POSITION_OPEN")
        events_before = len(store.fetch_event_log(run_id=run["id"], limit=1000))

        result = PaperExecutionRunner(store).advance_run(run["id"])

        assert result["terminal"] is True
        assert result["completed"] is True
        assert result["run"]["status"] == "POSITION_OPEN"
        assert {row["status"] for row in store.fetch_execution_steps(run["id"])} == {"PENDING"}
        # 재진입이 로그/이벤트를 재발행하지 않았는지 확인
        assert len(store.fetch_event_log(run_id=run["id"], limit=1000)) == events_before


def test_dead_letter_duplicate_key_increments_attempts_and_updates_payload():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        first_id = store.append_dead_letter(
            reason="provider_job_failed",
            deadletter_key="deadletter-dup",
            error_code="first_error",
            retryable=True,
            payload={"attempt": 1},
        )
        with store.conn() as conn:
            conn.execute(
                "UPDATE arb_dead_letters SET status = 'RESOLVED', resolved_at_ms = ? WHERE id = ?",
                (123456789, first_id),
            )
        second_id = store.append_dead_letter(
            reason="provider_job_failed",
            deadletter_key="deadletter-dup",
            error_code="second_error",
            retryable=True,
            payload={"attempt": 2},
        )

        rows = [row for row in store.fetch_dead_letters() if row["deadletter_key"] == "deadletter-dup"]
        assert first_id == second_id
        assert len(rows) == 1
        assert rows[0]["attempts"] == 1
        assert rows[0]["status"] == "OPEN"
        assert rows[0]["resolved_at_ms"] is None
        assert rows[0]["error_code"] == "second_error"
        assert rows[0]["payload"]["attempt"] == 2


def test_stale_quote_blocks_execution_and_creates_deadletter():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        _arm_live_execution(store, seeded["route_id"])
        store.mark_route_stale(seeded["route_id"], quote_fresh_until_ms=seeded["now_ms"] - 1)
        engine = ArbitrageEngine(store)

        result = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-stale-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        assert result["run"]["status"] == "BLOCKED"
        assert "stale_quote" in result["run"]["error_code"]
        deadletters = store.fetch_dead_letters()
        assert deadletters[-1]["reason"] == "execution_gate_failed"


def test_cex_deposit_block_only_blocks_cex_route_branch():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        same = _seed_live_route(store, route_type="same_dex_sell")
        cex_route_id = store.upsert_route(
            route_key="SOL:direct_cex_sell:bucket1",
            opportunity_id=same["opportunity_id"],
            route_type="direct_cex_sell",
            buy_market_id=same["buy_market_id"],
            sell_market_id=same["sell_market_id"],
            safety_status="WARN",
            route_status="CHECKING",
            edge_expected_bps=1200,
            edge_worst_bps=800,
            selected=False,
            quote_fresh_until_ms=same["now_ms"] + 30_000,
        )
        engine = ArbitrageEngine(store)

        engine.run_precheck(
            opportunity_id=same["opportunity_id"],
            route_id=cex_route_id,
            checks=[
                {"check_name": "sell_quote", "status": "PASS"},
                {"check_name": "cex_deposit_status", "status": "BLOCK", "error_code": "deposit_disabled"},
            ],
        )

        same_route = store.get_route(same["route_id"])
        cex_route = store.get_route(cex_route_id)
        assert same_route["route_status"] == "OPEN"
        assert same_route["safety_status"] == "PASS"
        assert cex_route["route_status"] == "BLOCKED"
        assert cex_route["safety_status"] == "BLOCK"


def test_unknown_external_outcome_moves_run_to_manual_review():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="live-full-unknown-approval",
        )
        engine = ArbitrageEngine(store)
        started = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-unknown-1",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        engine.mark_unknown_outcome(
            run_id=started["run"]["id"],
            step_key="dex_buy",
            external_ref="0xabc",
            error_code="tx_status_unknown",
        )

        run = store.get_execution_run(started["run"]["id"])
        step = [row for row in store.fetch_execution_steps(started["run"]["id"]) if row["step_key"] == "dex_buy"][0]
        assert run["status"] == "MANUAL_REVIEW"
        assert step["status"] == "RECONCILE"
        assert store.fetch_dead_letters()[-1]["reason"] == "unknown_external_outcome"


def test_snapshot_and_sse_contract_include_sequence_recovery_fields():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        store.append_event(
            event_type="opportunity.upsert",
            opportunity_id=seeded["opportunity_id"],
            payload={"hello": "world"},
        )
        engine = ArbitrageEngine(store)

        snapshot = engine.snapshot(selected_opportunity_id=seeded["opportunity_id"])
        assert snapshot["selected_opportunity_id"] == seeded["opportunity_id"]
        assert {"server_time", "snapshot_seq", "opportunities", "flow_nodes", "flow_edges", "logs", "positions"}.issubset(snapshot)
        assert snapshot["logs"][0]["event_id"]

        encoded = encode_sse_event(snapshot["logs"][0])
        assert encoded.startswith(f"id: {snapshot['logs'][0]['seq']}\n")
        assert "event: opportunity.upsert\n" in encoded
        assert '"event_id"' in encoded


def test_api_server_exposes_snapshot_precheck_execution_and_abort():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="api-live-full-approval",
        )
        server = _start_server(store)
        host, port = server.server_address
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/api/arbitrage/snapshot")
            response = conn.getresponse()
            assert response.status == 200
            body = json.loads(response.read().decode("utf-8"))
            assert "opportunities" in body

            conn.request(
                "POST",
                f"/api/arbitrage/opportunities/{seeded['opportunity_id']}/precheck",
                body=json.dumps({"route_id": seeded["route_id"], "checks": [{"check_name": "sell_quote", "status": "PASS"}]}),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 200
            assert json.loads(response.read().decode("utf-8"))["status"] == "PASS"

            conn.request(
                "POST",
                "/api/arbitrage/executions",
                body=json.dumps(
                    {
                        "opportunity_id": seeded["opportunity_id"],
                        "route_id": seeded["route_id"],
                        "mode": "live_full",
                        "idempotency_key": "api-exec-missing-boundary",
                        "trade_amount_krw": LIVE_TRADE_KRW,
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 400
            assert json.loads(response.read().decode("utf-8"))["error"] == "live_full_boundary_ack_required"
            assert store.get_execution_by_idempotency("api-exec-missing-boundary") is None

            conn.request(
                "POST",
                "/api/arbitrage/executions",
                body=json.dumps(
                    {
                        "opportunity_id": seeded["opportunity_id"],
                        "route_id": seeded["route_id"],
                        "mode": "live_full",
                        "idempotency_key": "api-exec-1",
                        "trade_amount_krw": LIVE_TRADE_KRW,
                        "simulated": True,
                        "provider_boundary": "deterministic_default_or_configured_provider_adapter",
                        "live_full_boundary_ack": True,
                        "cex_withdrawal_enabled": False,
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 202
            execution = json.loads(response.read().decode("utf-8"))
            run_id = execution["run"]["id"]

            conn.request("POST", f"/api/arbitrage/executions/{run_id}/abort", body=b"{}")
            response = conn.getresponse()
            assert response.status == 409
            abort_body = json.loads(response.read().decode("utf-8"))
            assert abort_body["error_code"] == "execution_run_terminal"
            assert store.get_execution_run(run_id)["status"] == "SETTLED"
        finally:
            server.shutdown()
            server.server_close()


def test_api_operator_approval_request_and_list_contract():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        server = _start_server(store)
        host, port = server.server_address
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            request = {
                "approval_key": "api-approval-sol-1",
                "opportunity_id": seeded["opportunity_id"],
                "route_id": seeded["route_id"],
                "mode": "one_click",
                "requested_by": "monitor",
                "reason": "operator confirmation required",
                "payload": {"edge_worst_bps": 900, "route_type": "same_dex_sell"},
            }
            conn.request(
                "POST",
                "/api/arbitrage/approvals",
                body=json.dumps(request),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 201
            created = json.loads(response.read().decode("utf-8"))
            approval = created["approval"]
            assert created["existing"] is False
            assert approval["approval_key"] == "api-approval-sol-1"
            assert approval["opportunity_id"] == seeded["opportunity_id"]
            assert approval["route_id"] == seeded["route_id"]
            assert approval["mode"] == "one_click"
            assert approval["requested_by"] == "monitor"
            assert approval["reason"] == "operator confirmation required"
            assert approval["status"] == "PENDING"
            assert approval["payload"]["route_type"] == "same_dex_sell"

            conn.request(
                "POST",
                "/api/arbitrage/approvals",
                body=json.dumps({**request, "reason": "duplicate"}),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 200
            duplicate = json.loads(response.read().decode("utf-8"))
            assert duplicate["existing"] is True
            assert duplicate["approval"]["id"] == approval["id"]
            assert duplicate["approval"]["reason"] == "operator confirmation required"

            conn.request(
                "GET",
                f"/api/arbitrage/approvals?opportunity_id={seeded['opportunity_id']}&route_id={seeded['route_id']}&status=PENDING",
            )
            response = conn.getresponse()
            assert response.status == 200
            listed = json.loads(response.read().decode("utf-8"))
            assert [row["id"] for row in listed["approvals"]] == [approval["id"]]
            assert listed["summary"]["total"] == 1
            assert listed["summary"]["by_status"] == {"PENDING": 1}

            events = [
                row
                for row in store.fetch_event_log(limit=500)
                if row["event_type"] == "operator_approval.requested"
            ]
            assert len(events) == 1
            assert events[0]["payload"]["approval_id"] == approval["id"]
            assert _table_count(store, "arb_operator_approvals") == 1
            assert _table_count(store, "arb_orders") == 0
            assert _table_count(store, "arb_transactions") == 0
            assert _table_count(store, "arb_transfers") == 0
        finally:
            server.shutdown()
            server.server_close()


def test_api_operator_approval_decisions_are_idempotent():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        approval = store.request_operator_approval(
            approval_key="api-decision-approve",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            requested_by="monitor",
            reason="approval decision test",
        )
        rejectable = store.request_operator_approval(
            approval_key="api-decision-reject",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            requested_by="monitor",
            reason="approval reject test",
        )
        server = _start_server(store)
        host, port = server.server_address

        def post(path: str, body: dict) -> tuple[int, dict]:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                path,
                body=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))

        try:
            status, body = post(
                f"/api/arbitrage/approvals/{approval['id']}/approve",
                {"operator": "alice", "payload": {"ticket": "OPS-1"}},
            )
            assert status == 200
            approved = body["approval"]
            assert approved["status"] == "APPROVED"
            assert approved["operator"] == "alice"
            assert approved["decided_at_ms"] is not None
            assert approved["decision_payload"] == {"ticket": "OPS-1"}

            status, body = post(
                f"/api/arbitrage/approvals/{approval['id']}/approve",
                {"operator": "bob", "payload": {"ticket": "OPS-2"}},
            )
            assert status == 200
            duplicate = body["approval"]
            assert duplicate["operator"] == "alice"
            assert duplicate["decided_at_ms"] == approved["decided_at_ms"]
            assert duplicate["decision_payload"] == {"ticket": "OPS-1"}

            status, body = post(f"/api/arbitrage/approvals/{approval['id']}/reject", {"operator": "carol"})
            assert status == 409
            assert body["error"] == "approval_already_approved"

            status, body = post(
                f"/api/arbitrage/approvals/{rejectable['id']}/reject",
                {"operator": "dana", "decision_payload": {"reason": "edge regressed"}},
            )
            assert status == 200
            rejected = body["approval"]
            assert rejected["status"] == "REJECTED"
            assert rejected["operator"] == "dana"
            assert rejected["decision_payload"] == {"reason": "edge regressed"}
        finally:
            server.shutdown()
            server.server_close()


def test_operator_approval_events_alert_snapshot_and_sse_metadata():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        engine = ArbitrageEngine(store)

        first = store.request_operator_approval(
            approval_key="approval-events-1",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            requested_by="monitor",
            reason="approval event coverage",
            payload={"edge_worst_bps": 900},
        )
        duplicate = store.request_operator_approval(
            approval_key="approval-events-1",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            requested_by="monitor",
            reason="duplicate should not emit",
            payload={"edge_worst_bps": 901},
        )

        assert first["created"] is True
        assert duplicate["created"] is False
        request_events = [
            row
            for row in store.fetch_event_log(limit=500)
            if row["event_type"] == "operator_approval.requested"
        ]
        alert_events = [
            row
            for row in store.fetch_event_log(limit=500)
            if row["event_type"] == "alert.operator_approval_requested"
        ]
        assert len(request_events) == 1
        assert len(alert_events) == 1
        assert request_events[0]["approval_id"] == first["id"]
        assert alert_events[0]["approval_id"] == first["id"]
        assert request_events[0]["occurred_at"] == request_events[0]["occurred_at_ms"]
        assert alert_events[0]["payload"]["external_notification"] is False
        assert len(store.fetch_alerts(channel="db_sse")) == 1

        pending_snapshot = engine.snapshot(selected_opportunity_id=seeded["opportunity_id"])
        assert [row["id"] for row in pending_snapshot["pending_approvals"]] == [first["id"]]
        assert len(pending_snapshot["alerts"]) == 1
        assert {
            "operator_approval.requested",
            "alert.operator_approval_requested",
        }.issubset({row["event_type"] for row in pending_snapshot["logs"]})
        selected_route = pending_snapshot["selected_route"]
        assert selected_route["id"] == seeded["route_id"]
        assert selected_route["approval_required"] is True
        assert selected_route["approval_status"] == "PENDING"
        assert selected_route["approval_id"] == first["id"]
        assert selected_route["latest_approval"]["approval_id"] == first["id"]
        assert pending_snapshot["opportunities"][0]["selected_route"]["approval_status"] == "PENDING"

        before_decision_seq = store.latest_event_seq()
        approved = store.decide_operator_approval(
            first["id"],
            status="APPROVED",
            operator="ops",
            decision_payload={"ticket": "OPS-9"},
        )
        duplicate_approved = store.decide_operator_approval(
            first["id"],
            status="APPROVED",
            operator="other-operator",
            decision_payload={"ticket": "OPS-10"},
        )

        assert duplicate_approved["operator"] == "ops"
        replay = list(reversed(store.fetch_event_log(after_seq=before_decision_seq, limit=500)))
        decision_events = [row for row in replay if row["event_type"] == "operator_approval.approved"]
        assert len(decision_events) == 1
        decision_event = decision_events[0]
        assert decision_event["opportunity_id"] == seeded["opportunity_id"]
        assert decision_event["route_id"] == seeded["route_id"]
        assert decision_event["approval_id"] == approved["id"]
        assert decision_event["payload"]["operator"] == "ops"
        assert decision_event["payload"]["decision_payload"] == {"ticket": "OPS-9"}

        encoded = encode_sse_event(decision_event)
        data_line = next(line for line in encoded.splitlines() if line.startswith("data: "))
        sse_payload = json.loads(data_line.removeprefix("data: "))
        assert sse_payload["seq"] == decision_event["seq"]
        assert sse_payload["event_id"] == decision_event["event_id"]
        assert sse_payload["occurred_at"] == decision_event["occurred_at_ms"]
        assert sse_payload["opportunity_id"] == seeded["opportunity_id"]
        assert sse_payload["route_id"] == seeded["route_id"]
        assert sse_payload["approval_id"] == approved["id"]

        decided_snapshot = engine.snapshot(selected_opportunity_id=seeded["opportunity_id"])
        assert decided_snapshot["pending_approvals"] == []
        assert decided_snapshot["selected_route"]["approval_status"] == "APPROVED"
        assert decided_snapshot["selected_route"]["latest_approval_decision"]["operator"] == "ops"
        assert decided_snapshot["selected_route"]["latest_approval_decision"]["decision_payload"] == {"ticket": "OPS-9"}
        assert _table_count(store, "arb_orders") == 0
        assert _table_count(store, "arb_transactions") == 0
        assert _table_count(store, "arb_transfers") == 0


def test_api_one_click_missing_approval_returns_pending_request_without_execution_run():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_one_click_execution(store, seeded["route_id"])
        server = _start_server(store)
        host, port = server.server_address

        def post_execution() -> tuple[int, dict]:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/api/arbitrage/executions",
                body=json.dumps(
                    {
                        "opportunity_id": seeded["opportunity_id"],
                        "route_id": seeded["route_id"],
                        "mode": "one_click",
                        "idempotency_key": "one-click-approval-required",
                        "requested_by": "monitor",
                        "trade_amount_krw": LIVE_TRADE_KRW,
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))

        try:
            status, body = post_execution()
            assert status == 409
            assert body["ok"] is False
            assert body["error_code"] == "approval_required"
            assert body["approval_required"] is True
            assert body["approval"]["status"] == "PENDING"
            assert body["approval"]["mode"] == "one_click"
            assert body["approval"]["route_id"] == seeded["route_id"]
            assert "approval_required" in body["blockers"]
            assert store.get_execution_by_idempotency("one-click-approval-required") is None
            assert _table_count(store, "arb_operator_approvals") == 1

            status, duplicate = post_execution()
            assert status == 409
            assert duplicate["approval"]["id"] == body["approval"]["id"]
            assert _table_count(store, "arb_operator_approvals") == 1
            assert _table_count(store, "arb_orders") == 0
            assert _table_count(store, "arb_transactions") == 0
            assert _table_count(store, "arb_transfers") == 0
        finally:
            server.shutdown()
            server.server_close()


def test_approved_one_click_creates_held_exec_ready_run_without_submissions():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_one_click_execution(store, seeded["route_id"])
        approval = store.request_operator_approval(
            approval_key="approved-one-click",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            requested_by="monitor",
            reason="operator confirmed held execution",
        )
        approved = store.decide_operator_approval(
            approval["id"],
            status="APPROVED",
            operator="ops",
            decision_payload={"ticket": "OPS-7"},
        )

        first = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            idempotency_key="one-click-held",
            requested_by="monitor",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        second = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            idempotency_key="one-click-held",
            requested_by="monitor",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert first["ok"] is True
        assert first["held"] is True
        run = store.get_execution_run(first["run"]["id"])
        assert run["mode"] == "one_click"
        assert run["status"] == "EXEC_READY"
        assert run["payload"]["held"] is True
        assert run["payload"]["non_submitting"] is True
        assert run["payload"]["approval"]["approval_id"] == approved["id"]
        assert run["payload"]["approval"]["approval_status"] == "APPROVED"
        assert run["payload"]["approval"]["operator"] == "ops"
        steps = store.fetch_execution_steps(run["id"])
        assert [row["step_key"] for row in steps] == ROUTE_STEPS["direct_cex_sell"]
        assert {row["status"] for row in steps} == {"PENDING"}
        assert all(row["external_ref"] == "" for row in steps)
        assert second["existing"] is True
        assert second["run"]["id"] == run["id"]
        assert second["run"]["status"] == "EXEC_READY"
        events = [row for row in store.fetch_event_log(limit=500) if row["run_id"] == run["id"]]
        assert any(row["event_type"] == "execution.log.append" and row["payload"]["status"] == "EXEC_READY" for row in events)
        assert not any(row["payload"].get("status") == "ENTERING" for row in events)

        snapshot = ArbitrageEngine(store).snapshot(selected_opportunity_id=seeded["opportunity_id"])
        assert snapshot["selected_execution_run"]["id"] == run["id"]
        assert snapshot["selected_execution_run"]["mode"] == "one_click"
        assert snapshot["selected_execution_run"]["status"] == "EXEC_READY"
        assert snapshot["selected_execution_run"]["payload"]["approval"]["approval_id"] == approved["id"]
        assert snapshot["selected_paper_run"] is None
        assert snapshot["current_step_key"] == ROUTE_STEPS["direct_cex_sell"][0]
        assert [row["step_key"] for row in snapshot["execution_steps"]] == ROUTE_STEPS["direct_cex_sell"]
        assert {row["status"] for row in snapshot["execution_steps"]} == {"PENDING"}
        assert snapshot["selected_route"]["approval_status"] == "APPROVED"
        assert snapshot["selected_route"]["latest_approval_decision"]["operator"] == "ops"
        snapshot_event_types = {row["event_type"] for row in snapshot["logs"]}
        assert "operator_approval.requested" in snapshot_event_types
        assert "operator_approval.approved" in snapshot_event_types
        assert "alert.operator_approval_requested" in snapshot_event_types
        assert _table_count(store, "arb_orders") == 0
        assert _table_count(store, "arb_transactions") == 0
        assert _table_count(store, "arb_transfers") == 0


def test_rejected_or_unknown_one_click_approval_never_passes_gate():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        rejected_seed = _seed_live_route(store, route_type="bridge_dex_sell")
        _arm_one_click_execution(store, rejected_seed["route_id"])
        approval = store.request_operator_approval(
            approval_key="rejected-one-click",
            opportunity_id=rejected_seed["opportunity_id"],
            route_id=rejected_seed["route_id"],
            mode="one_click",
            requested_by="monitor",
            reason="operator rejected",
        )
        store.decide_operator_approval(
            approval["id"],
            status="REJECTED",
            operator="ops",
            decision_payload={"reason": "liquidity moved"},
        )

        rejected = ArbitrageEngine(store).start_execution(
            opportunity_id=rejected_seed["opportunity_id"],
            route_id=rejected_seed["route_id"],
            mode="one_click",
            idempotency_key="one-click-rejected",
            requested_by="monitor",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert rejected["ok"] is False
        assert rejected["run"]["status"] == "BLOCKED"
        assert "operator_approval_rejected" in rejected["run"]["error_code"]

        unknown_seed = _seed_live_route(store, route_type="bridge_cex_sell")
        _arm_one_click_execution(store, unknown_seed["route_id"])
        unknown = store.request_operator_approval(
            approval_key="unknown-one-click",
            opportunity_id=unknown_seed["opportunity_id"],
            route_id=unknown_seed["route_id"],
            mode="one_click",
            requested_by="monitor",
            reason="unknown status guard",
        )
        with store.conn() as conn:
            conn.execute("UPDATE arb_operator_approvals SET status = 'MAYBE' WHERE id = ?", (unknown["id"],))

        blocked = ArbitrageEngine(store).start_execution(
            opportunity_id=unknown_seed["opportunity_id"],
            route_id=unknown_seed["route_id"],
            mode="one_click",
            idempotency_key="one-click-unknown",
            requested_by="monitor",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert blocked["ok"] is False
        assert blocked["run"]["status"] == "BLOCKED"
        assert "operator_approval_status_unknown" in blocked["run"]["error_code"]
        assert _table_count(store, "arb_orders") == 0
        assert _table_count(store, "arb_transactions") == 0
        assert _table_count(store, "arb_transfers") == 0


def test_approved_one_click_still_requires_existing_hard_gates():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        approval = store.request_operator_approval(
            approval_key="approved-but-disabled",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            requested_by="monitor",
            reason="hard gate proof",
        )
        store.decide_operator_approval(approval["id"], status="APPROVED", operator="ops")

        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            idempotency_key="one-click-hard-gates",
            requested_by="monitor",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        assert result["run"]["status"] == "BLOCKED"
        assert "one_click_disabled" in result["run"]["error_code"]
        assert "trade_cap_not_configured" in result["run"]["error_code"]
        assert store.fetch_execution_steps(result["run"]["id"]) == []


def test_api_operator_approval_request_validates_route_and_opportunity():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        other_opportunity_id = store.upsert_opportunity(
            opportunity_key="SOL:POLYGON:QUICKSWAP:UPBIT:bucket2",
            asset_id=seeded["asset_id"],
            anomaly_type="dex_cex_spread",
            lifecycle_status="PRECHECK_PASS",
            safety_status="PASS",
            buy_market_id=seeded["buy_market_id"],
            sell_market_id=seeded["sell_market_id"],
            spread_bps=1200,
            edge_expected_bps=900,
            edge_worst_bps=700,
            first_seen_at_ms=seeded["now_ms"],
            last_seen_at_ms=seeded["now_ms"],
        )
        other_route_id = store.upsert_route(
            route_key="SOL:same_dex_sell:approval-conflict",
            opportunity_id=seeded["opportunity_id"],
            route_type="same_dex_sell",
            buy_market_id=seeded["buy_market_id"],
            sell_market_id=seeded["sell_market_id"],
            safety_status="PASS",
            route_status="OPEN",
            edge_expected_bps=1000,
            edge_worst_bps=800,
            selected=False,
            quote_fresh_until_ms=seeded["now_ms"] + 30_000,
            edge_worst_verified=True,
        )
        server = _start_server(store)
        host, port = server.server_address

        def post_approval(body: dict) -> tuple[int, dict]:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/api/arbitrage/approvals",
                body=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))

        try:
            base = {
                "approval_key": "api-invalid-approval",
                "opportunity_id": seeded["opportunity_id"],
                "route_id": seeded["route_id"],
            }
            status, body = post_approval({**base, "opportunity_id": 999_999})
            assert status == 404
            assert body["error"] == "opportunity_not_found"
            assert _table_count(store, "arb_operator_approvals") == 0

            status, body = post_approval({**base, "route_id": 999_999})
            assert status == 404
            assert body["error"] == "route_not_found"
            assert _table_count(store, "arb_operator_approvals") == 0

            status, body = post_approval({**base, "opportunity_id": other_opportunity_id})
            assert status == 400
            assert body["error"] == "route_opportunity_mismatch"
            assert _table_count(store, "arb_operator_approvals") == 0
            assert not [
                row
                for row in store.fetch_event_log(limit=500)
                if row["event_type"] == "operator_approval.requested"
            ]

            created = store.request_operator_approval(
                approval_key="api-conflicting-approval-key",
                opportunity_id=seeded["opportunity_id"],
                route_id=seeded["route_id"],
                mode="one_click",
            )
            status, body = post_approval({**base, "approval_key": created["approval_key"], "route_id": other_route_id})
            assert status == 409
            assert body["error"] == "approval_key_conflict"
            assert _table_count(store, "arb_operator_approvals") == 1
            assert not [
                row
                for row in store.fetch_event_log(limit=500)
                if row["event_type"] == "operator_approval.requested" and row["route_id"] == other_route_id
            ]
        finally:
            server.shutdown()
            server.server_close()


def test_api_paper_execution_snapshot_restores_flow_logs_and_positions():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        server = _start_server(store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            request = {
                "opportunity_id": seeded["opportunity_id"],
                "route_id": seeded["route_id"],
                "mode": "paper",
                "idempotency_key": "api-paper-flow-1",
                "trade_amount_krw": LIVE_TRADE_KRW,
            }
            conn.request(
                "POST",
                "/api/arbitrage/executions",
                body=json.dumps(request),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 202
            execution = json.loads(response.read().decode("utf-8"))
            run_id = execution["run"]["id"]
            assert execution["run"]["mode"] == "paper"
            assert execution["run"]["status"] == "SETTLED"

            conn.request(
                "POST",
                "/api/arbitrage/executions",
                body=json.dumps(request),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 202
            duplicate = json.loads(response.read().decode("utf-8"))
            assert duplicate["existing"] is True
            assert duplicate["run"]["id"] == run_id

            event_types = {row["event_type"] for row in store.fetch_event_log(limit=500)}
            assert {
                "execution.step.started",
                "execution.step.completed",
                "execution.log.append",
                "flow.node.update",
                "flow.edge.update",
                "position.update",
            }.issubset(event_types)

            conn.request("GET", f"/api/arbitrage/snapshot?selected_opportunity_id={seeded['opportunity_id']}")
            response = conn.getresponse()
            assert response.status == 200
            snapshot = json.loads(response.read().decode("utf-8"))
            assert snapshot["selected_execution_run"]["id"] == run_id
            assert snapshot["selected_paper_run"]["id"] == run_id
            assert snapshot["selected_route_id"] == seeded["route_id"]
            assert snapshot["current_step_key"] == "settle"
            assert [step["step_key"] for step in snapshot["execution_steps"]] == ROUTE_STEPS["direct_cex_sell"]

            nodes = {node["id"]: node for node in snapshot["flow_nodes"]}
            assert nodes["dexBuy"]["state"] == "done"
            assert nodes["dexBuy"]["run_id"] == run_id
            assert nodes["directCexDeposit"]["duration_ms"] == 580
            assert nodes["directCexSell"]["state"] == "done"

            edges = {edge["id"]: edge for edge in snapshot["flow_edges"]}
            assert edges["precheck-buy"]["duration_ms"] == 240
            assert edges["buy-direct-cex"]["duration_ms"] == 580
            assert edges["direct-cex-sell"]["duration_ms"] == 260
            assert edges["direct-cex-sell"]["route_id"] == seeded["route_id"]
            assert edges["direct-cex-sell"]["run_id"] == run_id

            assert any(row["event_type"] == "execution.log.append" and row["run_id"] == run_id for row in snapshot["logs"])
            assert len(snapshot["positions"]) == 1
            assert snapshot["positions"][0]["run_id"] == run_id
            assert snapshot["positions"][0]["status"] == "SETTLED"
            assert snapshot["positions"][0]["latest_mark"]["route_status"]["run_id"] == run_id
        finally:
            conn.close()
            server.shutdown()
            server.server_close()


def test_sse_paper_flow_replay_events_include_run_route_and_sequence_metadata():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        before_seq = store.latest_event_seq()
        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            idempotency_key="paper-sse-metadata",
            requested_by="test",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        replay = list(reversed(store.fetch_event_log(after_seq=before_seq, limit=500)))
        flow_event = next(row for row in replay if row["event_type"] == "flow.edge.update")

        assert flow_event["seq"] > before_seq
        assert flow_event["event_id"]
        assert flow_event["occurred_at_ms"] > 0
        assert flow_event["opportunity_id"] == seeded["opportunity_id"]
        assert flow_event["route_id"] == seeded["route_id"]
        assert flow_event["run_id"] == result["run"]["id"]

        encoded = encode_sse_event(flow_event)
        data_line = next(line for line in encoded.splitlines() if line.startswith("data: "))
        payload = json.loads(data_line.removeprefix("data: "))
        assert payload["seq"] == flow_event["seq"]
        assert payload["event_id"] == flow_event["event_id"]
        assert payload["route_id"] == seeded["route_id"]
        assert payload["run_id"] == result["run"]["id"]
        assert payload["payload"]["edge_id"] == "signal-precheck"


def test_demo_sol_seed_is_idempotent_snapshot_ready_and_paper_only():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))

        first = seed_demo_sol_opportunity(store)
        counts_after_first = {
            table: _table_count(store, table)
            for table in (
                "arb_opportunities",
                "arb_routes",
                "arb_precheck_runs",
                "arb_precheck_results",
                "arb_execution_runs",
                "arb_event_log",
                "arb_positions",
            )
        }
        second = seed_demo_sol_opportunity(store)
        counts_after_second = {table: _table_count(store, table) for table in counts_after_first}

        assert second["opportunity_id"] == first["opportunity_id"]
        assert second["route_id"] == first["route_id"]
        assert counts_after_second == counts_after_first

        opportunity = store.get_opportunity(first["opportunity_id"])
        route = store.get_route(first["route_id"])
        freshness = store.fetch_route_freshness(first["route_id"])
        profile = store.get_strategy_profile("default")

        assert opportunity["selected_route_id"] == first["route_id"]
        assert opportunity["safety_status"] == "PASS"
        assert opportunity["lifecycle_status"] == "PRECHECK_PASS"
        assert route["route_type"] == "same_dex_sell"
        assert route["selected"] == 1
        assert route["edge_worst_bps"] == 1000
        assert route["edge_worst_verified"] == 1
        assert {"buy_quote", "sell_quote", "rpc_block"}.issubset(freshness)
        assert _table_count(store, "arb_precheck_results") == 8
        assert profile["paper_enabled"] == 1
        assert profile["one_click_enabled"] == 0
        assert profile["auto_small_enabled"] == 0
        assert profile["live_full_enabled"] == 0

        snapshot = ArbitrageEngine(store).snapshot(selected_opportunity_id=first["opportunity_id"])
        card = next(row for row in snapshot["opportunities"] if row["id"] == first["opportunity_id"])
        assert card["symbol"] == "SOL"
        assert card["buy"]["venue"] == "QUICKSWAP"
        assert card["buy"]["chain"] == "POLYGON"
        assert card["buy"]["token_ca"] == first["token_ca"]
        assert card["buy"]["pool_ca"] == first["buy_pool_ca"]
        assert card["sell"]["venue"] == "UNISWAP"
        assert card["sell"]["pool_ca"] == first["sell_pool_ca"]
        assert card["spread_bps"] == 2098
        assert card["edge_worst_bps"] == 1000
        assert card["selected_route"]["id"] == first["route_id"]
        assert card["selected_route"]["freshness"]["buy_quote"] >= snapshot["server_time"]
        assert DEMO_SOL_ID in json.dumps(snapshot, sort_keys=True)
        assert "MAPO" not in json.dumps(snapshot, sort_keys=True).upper()


def test_demo_sol_seed_paper_execution_progresses_from_backend_rows():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = seed_demo_sol_opportunity(store)
        engine = ArbitrageEngine(store)

        result = engine.start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="paper",
            idempotency_key="demo-sol-paper",
            requested_by="test",
            trade_amount_krw=99_862,
        )

        assert result["ok"] is True
        assert result["run"]["status"] == "SETTLED"
        assert _table_count(store, "arb_orders") == 0
        assert _table_count(store, "arb_transactions") == 0
        assert _table_count(store, "arb_transfers") == 0

        snapshot = engine.snapshot(selected_opportunity_id=seeded["opportunity_id"])
        assert snapshot["selected_paper_run"]["id"] == result["run"]["id"]
        assert snapshot["current_step_key"] == "settle"
        assert [step["step_key"] for step in snapshot["execution_steps"]] == ROUTE_STEPS["same_dex_sell"]
        assert {step["status"] for step in snapshot["execution_steps"]} == {"COMPLETED"}

        nodes = {node["id"]: node for node in snapshot["flow_nodes"]}
        edges = {edge["id"]: edge for edge in snapshot["flow_edges"]}
        assert nodes["signal"]["state"] == "done"
        assert nodes["precheck"]["state"] == "done"
        assert nodes["dexBuy"]["state"] == "done"
        assert nodes["sameDexSell"]["state"] == "done"
        assert "exit_route_select" in nodes["sameDexSell"]["step_keys"]
        assert edges["signal-precheck"]["duration_ms"] == 120
        assert edges["precheck-buy"]["duration_ms"] == 240
        assert edges["buy-same"]["duration_ms"] == 360

        event_types = {row["event_type"] for row in snapshot["logs"]}
        assert "execution.log.append" in event_types
        assert "position.update" in event_types
        assert snapshot["positions"][0]["status"] == "SETTLED"
        assert snapshot["positions"][0]["latest_mark"]["route_status"]["mode"] == "paper"


def test_api_demo_seed_endpoint_initializes_snapshot_without_live_modes():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        server = _start_server(store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request("POST", "/api/arbitrage/demo/seed", body=b"{}")
            response = conn.getresponse()
            assert response.status == 200
            seeded = json.loads(response.read().decode("utf-8"))
            assert seeded["mode"] == "paper_demo_only"

            conn.request("GET", f"/api/arbitrage/snapshot?selected_opportunity_id={seeded['opportunity_id']}")
            response = conn.getresponse()
            snapshot = json.loads(response.read().decode("utf-8"))
            card = snapshot["opportunities"][0]
            assert card["id"] == seeded["opportunity_id"]
            assert card["selected_route_id"] == seeded["route_id"]
            assert card["selected_route"]["edge_worst_verified"] == 1
            assert card["selected_route"]["freshness"]["sell_quote"] >= snapshot["server_time"]

            profile = store.get_strategy_profile("default")
            assert profile["paper_enabled"] == 1
            assert profile["one_click_enabled"] == 0
            assert profile["auto_small_enabled"] == 0
            assert profile["live_full_enabled"] == 0
        finally:
            conn.close()
            server.shutdown()
            server.server_close()


def test_collect_failure_does_not_advance_cursor_and_degrades_provider():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        store.record_collect_success(
            provider_key="alchemy:polygon",
            scope_key="pool:0xpool",
            cursor_value="12345",
            collected_count=10,
            inserted_count=8,
            latency_ms=120,
        )

        store.record_collect_failure(
            provider_key="alchemy:polygon",
            scope_key="pool:0xpool",
            cursor_before="12345",
            error_code="rpc_result_null",
            retryable=True,
            raw_payload={"result": None},
        )

        assert store.get_collect_cursor("alchemy:polygon", "pool:0xpool") == "12345"
        health = [row for row in store.fetch_provider_health() if row["provider_key"] == "alchemy:polygon"][0]
        assert health["status"] == "DEGRADED"
        assert health["consecutive_failures"] == 1
        deadletter = store.fetch_dead_letters()[-1]
        assert deadletter["reason"] == "collect_failure"
        assert deadletter["error_code"] == "rpc_result_null"


def test_stream_is_long_lived_and_replays_events_after_seq():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        first = store.append_event(event_type="opportunity.upsert", payload={"n": 1})
        second = store.append_event(event_type="flow.node.update", payload={"n": 2})
        server = _start_server(store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=2)
        try:
            conn.request("GET", f"/api/arbitrage/stream?after_seq={first['seq']}")
            response = conn.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            assert response.status == 200
            assert headers["content-type"].startswith("text/event-stream")
            assert "content-length" not in headers
            assert response.fp.readline().decode("utf-8") == f"id: {second['seq']}\n"
            assert response.fp.readline().decode("utf-8") == "event: flow.node.update\n"
        finally:
            conn.close()
            server.shutdown()
            server.server_close()


def test_stream_reconnect_honors_last_event_id_header():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        first = store.append_event(event_type="opportunity.upsert", payload={"n": 1})
        second = store.append_event(event_type="flow.node.update", payload={"n": 2})
        server = _start_server(store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=2)
        try:
            conn.request("GET", "/api/arbitrage/stream?after_seq=0", headers={"Last-Event-ID": str(first["seq"])})
            response = conn.getresponse()
            assert response.status == 200
            assert response.fp.readline().decode("utf-8") == f"id: {second['seq']}\n"
            assert response.fp.readline().decode("utf-8") == "event: flow.node.update\n"
        finally:
            conn.close()
            server.shutdown()
            server.server_close()


def test_stream_sends_heartbeat_without_closing_when_no_events():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        server = _start_server(store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=2)
        try:
            conn.request("GET", "/api/arbitrage/stream?after_seq=999")
            response = conn.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            assert response.status == 200
            assert "content-length" not in headers
            assert response.fp.readline().decode("utf-8") == "event: heartbeat\n"
            assert response.fp.readline().decode("utf-8") == "data: {}\n"
        finally:
            conn.close()
            server.shutdown()
            server.server_close()


def test_stream_large_replay_gap_sends_truncated_signal():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        for index in range(501):
            store.append_event(event_type="execution.log.append", payload={"index": index})
        server = _start_server(store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=2)
        try:
            conn.request("GET", "/api/arbitrage/stream?after_seq=0")
            response = conn.getresponse()
            assert response.status == 200
            assert response.fp.readline().decode("utf-8").startswith("id: ")
            assert response.fp.readline().decode("utf-8") == "event: replay_truncated\n"
            assert "snapshot_reload_required" in response.fp.readline().decode("utf-8")
        finally:
            conn.close()
            server.shutdown()
            server.server_close()


def test_snapshot_logs_are_scoped_to_selected_opportunity():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        first = _seed_live_route(store)
        second_opportunity_id = store.upsert_opportunity(
            opportunity_key="SOL:SECOND:LOGS",
            asset_id=first["asset_id"],
            anomaly_type="dex_cex_spread",
            lifecycle_status="PRECHECK_PASS",
            safety_status="PASS",
            buy_market_id=first["buy_market_id"],
            sell_market_id=first["sell_market_id"],
            spread_bps=900,
            edge_expected_bps=800,
            edge_worst_bps=700,
            first_seen_at_ms=first["now_ms"],
            last_seen_at_ms=first["now_ms"],
        )
        store.append_event(
            event_type="execution.log.append",
            opportunity_id=first["opportunity_id"],
            route_id=first["route_id"],
            payload={"message": "selected-log"},
        )
        store.append_event(
            event_type="execution.log.append",
            opportunity_id=second_opportunity_id,
            payload={"message": "other-log"},
        )

        snapshot = ArbitrageEngine(store).snapshot(selected_opportunity_id=first["opportunity_id"])
        messages = [row.get("payload", {}).get("message") for row in snapshot["logs"]]

        assert "selected-log" in messages
        assert "other-log" not in messages


def test_live_full_blocks_when_trade_or_daily_loss_cap_is_zero():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        store.enable_strategy_mode("default", live_full_enabled=True)
        store.ensure_wallet(
            wallet_key="paper-hot-polygon",
            chain_code="POLYGON",
            address="0x9999999999999999999999999999999999999999",
            wallet_type="HOT",
            mode="live_full",
            enabled=True,
            withdrawal_enabled=False,
        )
        store.set_route_freshness(
            seeded["route_id"],
            {
                "buy_quote": seeded["now_ms"] + 30_000,
                "sell_quote": seeded["now_ms"] + 30_000,
                "orderbook": seeded["now_ms"] + 30_000,
                "fx": seeded["now_ms"] + 30_000,
                "rpc_block": seeded["now_ms"] + 30_000,
            },
        )
        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-cap-zero",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert result["ok"] is False
        assert "trade_cap_not_configured" in result["run"]["error_code"]
        assert "daily_loss_cap_not_configured" in result["run"]["error_code"]


def test_live_full_blocks_bridge_or_cex_route_without_operator_approval():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-approval-required",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert result["ok"] is False
        assert "operator_approval_required" in result["run"]["error_code"]

        one_click_approval = store.request_operator_approval(
            approval_key="one-click-only-does-not-unblock-live-full",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
        )
        store.decide_operator_approval(one_click_approval["id"], status="APPROVED", operator="ops")
        one_click_only = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-approval-one-click-only",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert one_click_only["ok"] is False
        assert "operator_approval_required" in one_click_only["run"]["error_code"]


def test_live_full_blocks_same_dex_route_type_for_part8_boundary():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="same_dex_sell")
        _arm_live_execution(store, seeded["route_id"])

        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="live-full-same-dex-unsupported",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        assert result["run"]["status"] == "BLOCKED"
        assert "route_type_not_supported" in result["run"]["error_code"]
        assert store.fetch_execution_steps(result["run"]["id"]) == []


@pytest.mark.parametrize("route_type", ["direct_cex_sell", "bridge_dex_sell", "bridge_cex_sell"])
def test_live_full_supported_routes_settle_simulated_saga_and_are_idempotent(route_type: str):
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type=route_type)
        _arm_live_execution(store, seeded["route_id"])
        approved = _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key=f"live-full-approved-{route_type}",
        )

        first = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key=f"live-full-start-{route_type}",
            requested_by="monitor",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        second = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key=f"live-full-start-{route_type}",
            requested_by="monitor",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert first["ok"] is True
        assert first["run"]["status"] == "SETTLED"
        assert first["run"]["payload"]["route_type"] == route_type
        assert first["run"]["payload"]["approval"]["approval_id"] == approved["id"]
        assert first["run"]["payload"]["simulated"] is True
        assert first["run"]["payload"]["cex_withdrawal_enabled"] is False
        steps = store.fetch_execution_steps(first["run"]["id"])
        assert [row["step_key"] for row in steps] == ROUTE_STEPS[route_type]
        assert {row["status"] for row in steps} == {"COMPLETED"}

        transactions = store.fetch_transactions_for_run_step(first["run"]["id"])
        transfers = store.fetch_transfers_for_run_step(first["run"]["id"])
        orders = store.fetch_orders_for_run_step(first["run"]["id"])
        expected_transactions = 2 if route_type == "bridge_dex_sell" else 1
        expected_transfers = 2 if route_type == "bridge_cex_sell" else 1
        expected_orders = 0 if route_type == "bridge_dex_sell" else 1
        assert len(transactions) == expected_transactions
        assert len(transfers) == expected_transfers
        assert len(orders) == expected_orders
        assert all(tx["payload"]["mode"] == "live_full" for tx in transactions)
        assert all(tx["payload"]["cex_withdrawal_enabled"] is False for tx in transactions)
        assert all(transfer["payload"]["cex_withdrawal_enabled"] is False for transfer in transfers)
        assert all(order["payload"]["cex_withdrawal_enabled"] is False for order in orders)

        positions = store.fetch_positions(run_id=first["run"]["id"])
        assert len(positions) == 1
        assert positions[0]["status"] == "SETTLED"
        assert positions[0]["payload"]["mode"] == "live_full"
        assert positions[0]["payload"]["simulated"] is True
        assert positions[0]["payload"]["cex_withdrawal_enabled"] is False
        assert store.fetch_position_marks(positions[0]["id"])[-1]["route_status"]["mode"] == "live_full"

        assert second["existing"] is True
        assert second["run"]["id"] == first["run"]["id"]
        assert _table_count(store, "arb_orders") == expected_orders
        assert _table_count(store, "arb_transactions") == expected_transactions
        assert _table_count(store, "arb_transfers") == expected_transfers
        event_types = {row["event_type"] for row in store.fetch_event_log(limit=500)}
        assert {
            "execution.step.started",
            "execution.step.completed",
            "execution.log.append",
            "flow.node.update",
            "flow.edge.update",
            "position.update",
            "transfer.update",
        }.issubset(event_types)
        if expected_orders:
            assert "order.update" in event_types


def test_live_full_snapshot_includes_bridge_cex_refs_boundary_and_position_marks():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="bridge_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        approved = _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="live-full-snapshot-approval",
        )
        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="live-full-snapshot-refs",
            requested_by="monitor",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        snapshot = ArbitrageEngine(store).snapshot(selected_opportunity_id=seeded["opportunity_id"])

        assert result["ok"] is True
        assert snapshot["current_route_type"] == "bridge_cex_sell"
        assert snapshot["selected_execution_run"]["id"] == result["run"]["id"]
        assert snapshot["selected_route"]["live_full_approval_status"] == "APPROVED"
        assert snapshot["selected_route"]["live_full_approval_id"] == approved["id"]
        assert snapshot["selected_route"]["live_full_approval_amount_krw"] == LIVE_TRADE_KRW
        assert snapshot["blockers"] == []
        assert snapshot["live_full_boundary"]["simulated"] is True
        assert snapshot["live_full_boundary"]["dry_run"] is True
        assert snapshot["live_full_boundary"]["cex_withdrawal_enabled"] is False
        assert snapshot["live_full_boundary"]["real_external_submit_enabled"] is False
        assert snapshot["live_full_boundary"]["provider_boundary"] == "deterministic_default_or_explicit_adapter"
        assert len(snapshot["transactions"]) == 1
        assert len(snapshot["transfers"]) == 2
        assert len(snapshot["orders"]) == 1
        assert {row["payload"]["step_key"] for row in snapshot["transfers"]} == {"bridge", "cex_deposit"}
        assert snapshot["orders"][0]["payload"]["order_ref"]
        assert snapshot["orders"][0]["payload"]["cex_withdrawal_enabled"] is False
        assert snapshot["positions"][0]["marks"]
        assert snapshot["positions"][0]["latest_mark"]["route_status"]["mode"] == "live_full"
        node_refs = [
            ref
            for node in snapshot["flow_nodes"]
            for ref in node.get("external_refs", [])
        ]
        assert any(str(ref).startswith("bridge_") or str(ref).startswith("cex_") for ref in node_refs)
        transfer_events = [
            row["payload"]
            for row in store.fetch_event_log(limit=500)
            if row["event_type"] == "transfer.update"
        ]
        order_events = [
            row["payload"]
            for row in store.fetch_event_log(limit=500)
            if row["event_type"] == "order.update"
        ]
        assert any(payload.get("bridge_ref") for payload in transfer_events)
        assert any(payload.get("deposit_ref") for payload in transfer_events)
        assert any(payload.get("order_ref") for payload in order_events)


def test_live_full_blocked_snapshot_exposes_exact_blocker_list():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="live-full-snapshot-blocked",
        )
        _set_route_payload(
            store,
            seeded["route_id"],
            {"cex_deposit_status": {"status": "BLOCK", "error_code": "deposit_disabled"}},
        )

        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="live-full-snapshot-blocked-run",
            requested_by="monitor",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        snapshot = ArbitrageEngine(store).snapshot(selected_opportunity_id=seeded["opportunity_id"])

        assert result["ok"] is False
        assert snapshot["selected_execution_run"]["status"] == "BLOCKED"
        assert "cex_deposit_blocked" in snapshot["blockers"]
        assert "deposit_disabled" in snapshot["blockers"]
        assert snapshot["orders"] == []
        assert snapshot["transfers"] == []


def test_live_full_rejects_amount_mismatched_and_expired_approvals():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            amount_krw=LIVE_TRADE_KRW + 1,
            approval_key="live-full-amount-mismatch",
        )

        mismatch = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="live-full-amount-mismatch-run",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert mismatch["ok"] is False
        assert "operator_approval_amount_mismatch" in mismatch["run"]["error_code"]

        expired_seed = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, expired_seed["route_id"])
        _approve_live_full(
            store,
            opportunity_id=expired_seed["opportunity_id"],
            route_id=expired_seed["route_id"],
            expires_delta_ms=-1,
            approval_key="live-full-expired",
        )
        expired = ArbitrageEngine(store).start_execution(
            opportunity_id=expired_seed["opportunity_id"],
            route_id=expired_seed["route_id"],
            mode="live_full",
            idempotency_key="live-full-expired-run",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert expired["ok"] is False
        assert "operator_approval_expired" in expired["run"]["error_code"]


def test_live_full_blocks_cex_withdrawal_permission_on_wallet_or_venue():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="live-full-wallet-withdrawal",
        )
        store.ensure_wallet(
            wallet_key="unsafe-live-full-wallet",
            chain_code="POLYGON",
            address="0x8888888888888888888888888888888888888888",
            wallet_type="HOT",
            mode="live_full",
            enabled=True,
            withdrawal_enabled=True,
        )

        wallet_block = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="live-full-wallet-withdrawal-block",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert wallet_block["ok"] is False
        assert "cex_withdrawal_permission_must_be_disabled" in wallet_block["run"]["error_code"]

        venue_seed = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, venue_seed["route_id"])
        _approve_live_full(
            store,
            opportunity_id=venue_seed["opportunity_id"],
            route_id=venue_seed["route_id"],
            approval_key="live-full-venue-withdrawal",
        )
        with store.conn() as conn:
            conn.execute("UPDATE arb_wallets SET withdrawal_enabled = 0")
            conn.execute("UPDATE arb_venues SET withdrawal_enabled = 1 WHERE venue_code = 'UPBIT'")

        venue_block = ArbitrageEngine(store).start_execution(
            opportunity_id=venue_seed["opportunity_id"],
            route_id=venue_seed["route_id"],
            mode="live_full",
            idempotency_key="live-full-venue-withdrawal-block",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert venue_block["ok"] is False
        assert "cex_withdrawal_permission_must_be_disabled" in venue_block["run"]["error_code"]


def test_live_full_blocks_when_fx_or_orderbook_or_rpc_is_stale():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        _arm_live_execution(store, seeded["route_id"])
        store.set_route_freshness(
            seeded["route_id"],
            {
                "buy_quote": seeded["now_ms"] + 30_000,
                "sell_quote": seeded["now_ms"] + 30_000,
                "orderbook": seeded["now_ms"] - 1,
                "fx": seeded["now_ms"] - 1,
                "rpc_block": seeded["now_ms"] - 1,
            },
        )
        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-stale-sources",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert result["ok"] is False
        assert "stale_orderbook" in result["run"]["error_code"]
        assert "stale_fx" in result["run"]["error_code"]
        assert "stale_rpc_block" in result["run"]["error_code"]
        assert store.fetch_dead_letters()[-1]["payload"]["blockers"]


def test_live_full_blocks_route_scoped_bridge_and_cex_provider_status_failures():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        direct = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, direct["route_id"])
        _approve_live_full(
            store,
            opportunity_id=direct["opportunity_id"],
            route_id=direct["route_id"],
            approval_key="live-full-deposit-blocked",
        )
        with store.conn() as conn:
            conn.execute("UPDATE arb_markets SET deposit_network = '' WHERE id = ?", (direct["sell_market_id"],))
        _set_route_payload(
            store,
            direct["route_id"],
            {"cex_deposit_status": {"status": "BLOCK", "error_code": "deposit_disabled"}},
        )

        direct_result = ArbitrageEngine(store).start_execution(
            opportunity_id=direct["opportunity_id"],
            route_id=direct["route_id"],
            mode="live_full",
            idempotency_key="live-full-deposit-blocked-run",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert direct_result["ok"] is False
        assert "missing_deposit_network" in direct_result["run"]["error_code"]
        assert "cex_deposit_blocked" in direct_result["run"]["error_code"]
        assert "deposit_disabled" in direct_result["run"]["error_code"]

        bridge = _seed_live_route(store, route_type="bridge_dex_sell")
        _arm_live_execution(store, bridge["route_id"])
        _approve_live_full(
            store,
            opportunity_id=bridge["opportunity_id"],
            route_id=bridge["route_id"],
            approval_key="live-full-bridge-unknown",
        )
        _set_route_payload(
            store,
            bridge["route_id"],
            {
                "bridge_status": {"status": "unknown"},
                "bridge_fee_verified": False,
            },
        )

        bridge_result = ArbitrageEngine(store).start_execution(
            opportunity_id=bridge["opportunity_id"],
            route_id=bridge["route_id"],
            mode="live_full",
            idempotency_key="live-full-bridge-unknown-run",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert bridge_result["ok"] is False
        assert "provider_status_unknown:bridge" in bridge_result["run"]["error_code"]
        assert "bridge_fee_unverified" in bridge_result["run"]["error_code"]


def test_snapshot_opportunity_cards_include_buy_sell_contract_and_deposit_fields():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        card = ArbitrageEngine(store).snapshot(selected_opportunity_id=seeded["opportunity_id"])["opportunities"][0]
        assert card["buy"]["venue"] == "QUICKSWAP"
        assert card["buy"]["chain"] == "POLYGON"
        assert card["buy"]["token_ca"] == "0x1111111111111111111111111111111111111111"
        assert card["buy"]["pool_ca"] == "0x2222222222222222222222222222222222222222"
        assert card["sell"]["venue"] == "UPBIT"
        assert card["sell"]["market"] == "SOL/KRW"
        assert card["sell"]["deposit_network"] == "POLYGON"
        assert card["selected_route"]["id"] == seeded["route_id"]


def test_selected_route_is_unique_per_opportunity():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="same_dex_sell")
        second_route_id = store.upsert_route(
            route_key="SOL:bridge_dex_sell:bucket1",
            opportunity_id=seeded["opportunity_id"],
            route_type="bridge_dex_sell",
            buy_market_id=seeded["buy_market_id"],
            sell_market_id=seeded["sell_market_id"],
            safety_status="PASS",
            route_status="OPEN",
            edge_expected_bps=1200,
            edge_worst_bps=900,
            selected=True,
            quote_fresh_until_ms=seeded["now_ms"] + 30_000,
            edge_worst_verified=True,
        )
        selected_routes = [row for row in store.fetch_routes_for_opportunity(seeded["opportunity_id"]) if row["selected"] == 1]
        assert [row["id"] for row in selected_routes] == [second_route_id]
        assert store.get_opportunity(seeded["opportunity_id"])["selected_route_id"] == second_route_id


def test_blocked_selected_route_requires_reselection_or_blocks_execution():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        selected = _seed_live_route(store, route_type="direct_cex_sell")
        same_route_id = store.upsert_route(
            route_key="SOL:same_dex_sell:bucket1",
            opportunity_id=selected["opportunity_id"],
            route_type="same_dex_sell",
            buy_market_id=selected["buy_market_id"],
            sell_market_id=selected["sell_market_id"],
            safety_status="PASS",
            route_status="OPEN",
            edge_expected_bps=1200,
            edge_worst_bps=900,
            selected=False,
            quote_fresh_until_ms=selected["now_ms"] + 30_000,
            edge_worst_verified=True,
        )
        ArbitrageEngine(store).run_precheck(
            opportunity_id=selected["opportunity_id"],
            route_id=selected["route_id"],
            checks=[{"check_name": "cex_deposit_status", "status": "BLOCK", "error_code": "deposit_disabled"}],
        )
        opportunity = store.get_opportunity(selected["opportunity_id"])
        assert opportunity["selected_route_id"] == same_route_id
        assert store.get_route(same_route_id)["selected"] == 1


def test_edge_worst_must_be_verified_before_live_execution():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        store.set_route_edge_verification(seeded["route_id"], verified=False)
        _arm_live_execution(store, seeded["route_id"])
        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-edge-unverified",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        assert result["ok"] is False
        assert "edge_worst_unverified" in result["run"]["error_code"]


def test_live_full_blocks_missing_evaluator_component_and_stale_route_freshness():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        _arm_live_execution(store, seeded["route_id"])
        _set_route_payload(
            store,
            seeded["route_id"],
            {"edge_evaluation": {"missing_components": ["gas"]}},
        )
        store.set_route_freshness(seeded["route_id"], {"rpc_block": seeded["now_ms"] - 1})

        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-edge-components",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        assert "edge_component_missing:gas" in result["run"]["error_code"]
        assert "edge_component_stale:rpc_freshness" in result["run"]["error_code"]
        assert "stale_rpc_block" in result["run"]["error_code"]


def test_live_full_blocks_blocked_precheck_route_with_canonical_blockers():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        _arm_live_execution(store, seeded["route_id"])
        ArbitrageEngine(store).run_precheck(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            checks=[{"check_name": "route_edge", "status": "BLOCK", "error_code": "edge_regressed"}],
        )

        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="exec-blocked-precheck",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        assert "route_blocked" in result["run"]["error_code"]
        assert "precheck_blocked" in result["run"]["error_code"]


def test_invalid_opportunity_or_route_execution_returns_api_error_without_creating_live_run():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store)
        server = _start_server(store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request(
                "POST",
                "/api/arbitrage/executions",
                body=json.dumps(
                    {
                        "opportunity_id": 99999,
                        "route_id": 99999,
                        "mode": "live_full",
                        "idempotency_key": "api-invalid",
                        "trade_amount_krw": LIVE_TRADE_KRW,
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 404
            assert json.loads(response.read().decode("utf-8"))["error"] == "opportunity_not_found"
            assert store.get_execution_by_idempotency("api-invalid") is None

            conn.request(
                "POST",
                "/api/arbitrage/executions",
                body=json.dumps(
                    {
                        "opportunity_id": seeded["opportunity_id"],
                        "route_id": 99999,
                        "mode": "live_full",
                        "idempotency_key": "api-invalid-route",
                        "trade_amount_krw": LIVE_TRADE_KRW,
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            assert response.status == 404
            assert json.loads(response.read().decode("utf-8"))["error"] == "route_not_found"
            assert store.get_execution_by_idempotency("api-invalid-route") is None
        finally:
            conn.close()
            server.shutdown()
            server.server_close()


def test_live_full_runner_direct_call_cannot_bypass_engine_hard_gates():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="bridge_cex_sell")

        result = LiveFullBridgeCexRunner(store).start(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="direct-runner-bypass",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        assert result["run"] is None
        assert result["error_code"] == "engine_gate_required"
        assert store.get_execution_by_idempotency("direct-runner-bypass") is None


def test_live_full_idempotency_key_scope_conflict_does_not_return_existing_run():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        first = _seed_live_route(store, route_type="bridge_cex_sell")
        second = _seed_live_route(store, route_type="direct_cex_sell")
        _arm_live_execution(store, first["route_id"])
        _arm_live_execution(store, second["route_id"])
        _approve_live_full(store, opportunity_id=first["opportunity_id"], route_id=first["route_id"], approval_key="scope-first")
        _approve_live_full(store, opportunity_id=second["opportunity_id"], route_id=second["route_id"], approval_key="scope-second")

        first_result = ArbitrageEngine(store).start_execution(
            opportunity_id=first["opportunity_id"],
            route_id=first["route_id"],
            mode="live_full",
            idempotency_key="scoped-live-key",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        conflict = ArbitrageEngine(store).start_execution(
            opportunity_id=second["opportunity_id"],
            route_id=second["route_id"],
            mode="live_full",
            idempotency_key="scoped-live-key",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert first_result["ok"] is True
        assert conflict["ok"] is False
        assert conflict["error_code"] == "idempotency_scope_conflict"
        assert conflict["run"]["id"] == first_result["run"]["id"]


def test_live_full_approval_is_single_use_by_default():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="bridge_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        approval = _approve_live_full(
            store,
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            approval_key="single-use-approval",
        )

        first = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="single-use-run-1",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        second = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="single-use-run-2",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        consumed = store.get_operator_approval(approval["id"])
        assert first["ok"] is True
        assert consumed["consumed_run_id"] == first["run"]["id"]
        assert second["ok"] is False
        assert "operator_approval_required" in second["run"]["error_code"]


def test_terminal_execution_run_cannot_be_aborted():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="bridge_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(store, opportunity_id=seeded["opportunity_id"], route_id=seeded["route_id"])
        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="terminal-abort-run",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        abort = ArbitrageEngine(store).abort_execution(result["run"]["id"])
        stored = store.get_execution_run(result["run"]["id"])

        assert result["run"]["status"] == "SETTLED"
        assert abort["ok"] is False
        assert abort["error_code"] == "execution_run_terminal"
        assert stored["status"] == "SETTLED"


def test_required_freshness_is_checked_even_when_edge_evaluation_payload_exists():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="bridge_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(store, opportunity_id=seeded["opportunity_id"], route_id=seeded["route_id"])
        _set_route_payload(store, seeded["route_id"], {"edge_evaluation": {"freshness": {}}})
        with store.conn() as conn:
            conn.execute("DELETE FROM arb_route_freshness WHERE route_id = ?", (seeded["route_id"],))

        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="missing-freshness-with-edge-eval",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        assert "missing_buy_quote_freshness" in result["run"]["error_code"]
        assert "missing_rpc_block_freshness" in result["run"]["error_code"]


def test_payload_only_edge_evaluation_freshness_cannot_replace_db_freshness():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="bridge_cex_sell")
        _arm_live_execution(store, seeded["route_id"])
        _approve_live_full(store, opportunity_id=seeded["opportunity_id"], route_id=seeded["route_id"])
        route = store.get_route(seeded["route_id"])
        required = (
            "buy_quote",
            "sell_quote_or_orderbook",
            "orderbook",
            "rpc_freshness",
            "bridge_fee",
            "deposit_or_bridge_status",
        )
        _set_route_payload(
            store,
            seeded["route_id"],
            {
                **(route.get("payload") or {}),
                "edge_evaluation": {
                    "freshness": {
                        name: {"component": name, "status": "fresh", "fresh_until_ms": seeded["now_ms"] + 30_000}
                        for name in required
                    }
                },
            },
        )
        with store.conn() as conn:
            conn.execute("DELETE FROM arb_route_freshness WHERE route_id = ?", (seeded["route_id"],))

        result = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="live_full",
            idempotency_key="payload-only-freshness",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        assert result["ok"] is False
        assert "missing_buy_quote_freshness" in result["run"]["error_code"]
        assert "missing_rpc_block_freshness" in result["run"]["error_code"]


def test_engine_precheck_rejects_route_opportunity_mismatch_without_mutation():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        first = _seed_live_route(store)
        second_opportunity_id = store.upsert_opportunity(
            opportunity_key="SOL:SECOND:ENGINE:PRECHECK",
            asset_id=first["asset_id"],
            anomaly_type="dex_cex_spread",
            lifecycle_status="PRECHECK_PASS",
            safety_status="PASS",
            buy_market_id=first["buy_market_id"],
            sell_market_id=first["sell_market_id"],
            spread_bps=1200,
            edge_expected_bps=900,
            edge_worst_bps=800,
            first_seen_at_ms=first["now_ms"],
            last_seen_at_ms=first["now_ms"],
        )
        second_route_id = store.upsert_route(
            route_key="SOL:SECOND:ENGINE:PRECHECK:bridge_cex_sell",
            opportunity_id=second_opportunity_id,
            route_type="bridge_cex_sell",
            buy_market_id=first["buy_market_id"],
            sell_market_id=first["sell_market_id"],
            safety_status="PASS",
            route_status="OPEN",
            edge_expected_bps=900,
            edge_worst_bps=800,
            selected=True,
            quote_fresh_until_ms=first["now_ms"] + 30_000,
            edge_worst_verified=True,
        )

        result = ArbitrageEngine(store).run_precheck(
            opportunity_id=first["opportunity_id"],
            route_id=second_route_id,
            checks=[{"check_name": "wrong_route", "status": "BLOCK"}],
        )

        assert result["ok"] is False
        assert result["error_code"] == "route_opportunity_mismatch"
        assert store.get_route(second_route_id)["route_status"] == "OPEN"
        assert _table_count(store, "arb_precheck_runs") == 0


def test_one_click_approval_is_single_use_by_default():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        seeded = _seed_live_route(store, route_type="same_dex_sell")
        _arm_one_click_execution(store, seeded["route_id"])
        approval = store.request_operator_approval(
            approval_key="one-click-single-use",
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            requested_by="ops",
            reason="single one_click approval",
            payload={"trade_amount_krw": LIVE_TRADE_KRW},
        )
        store.decide_operator_approval(approval["id"], status="APPROVED", operator="ops")

        first = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            idempotency_key="one-click-single-use-1",
            trade_amount_krw=LIVE_TRADE_KRW,
        )
        second = ArbitrageEngine(store).start_execution(
            opportunity_id=seeded["opportunity_id"],
            route_id=seeded["route_id"],
            mode="one_click",
            idempotency_key="one-click-single-use-2",
            trade_amount_krw=LIVE_TRADE_KRW,
        )

        consumed = store.get_operator_approval(approval["id"])
        assert first["ok"] is True
        assert consumed["consumed_run_id"] == first["run"]["id"]
        assert second["ok"] is False
        assert second["error_code"] == "approval_required"


def test_precheck_api_rejects_route_opportunity_mismatch_without_mutating_route():
    with TemporaryDirectory() as td:
        store = _store(str(Path(td) / "arbitrage.db"))
        first = _seed_live_route(store)
        first_route = store.get_route(first["route_id"])
        second_opportunity_id = store.upsert_opportunity(
            opportunity_key="SOL:SECOND:PRECHECK:MISMATCH",
            asset_id=first["asset_id"],
            anomaly_type="dex_cex_spread",
            lifecycle_status="PRECHECK_PASS",
            safety_status="PASS",
            buy_market_id=first["buy_market_id"],
            sell_market_id=first["sell_market_id"],
            spread_bps=1200,
            edge_expected_bps=900,
            edge_worst_bps=800,
            first_seen_at_ms=first["now_ms"],
            last_seen_at_ms=first["now_ms"],
        )
        second_route_id = store.upsert_route(
            route_key="SOL:SECOND:PRECHECK:MISMATCH:bridge_cex_sell",
            opportunity_id=second_opportunity_id,
            route_type="bridge_cex_sell",
            buy_market_id=first_route["buy_market_id"],
            sell_market_id=first_route["sell_market_id"],
            safety_status="PASS",
            route_status="OPEN",
            edge_expected_bps=900,
            edge_worst_bps=800,
            selected=True,
            quote_fresh_until_ms=first["now_ms"] + 30_000,
            edge_worst_verified=True,
        )
        server = _start_server(store)
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request(
                "POST",
                    f"/api/arbitrage/opportunities/{first['opportunity_id']}/precheck",
                    body=json.dumps(
                        {
                            "route_id": second_route_id,
                            "checks": [{"check_name": "wrong_route", "status": "BLOCK"}],
                        }
                    ),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            body = json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()
            server.shutdown()
            server.server_close()

        assert response.status == 400
        assert body["error"] == "route_opportunity_mismatch"
        assert store.get_route(second_route_id)["route_status"] == "OPEN"
        assert _table_count(store, "arb_precheck_runs") == 0
