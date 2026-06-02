from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .file_perms import secure_sqlite_artifacts


DEFAULT_DB_PATH = os.getenv("ARBITRAGE_DB_PATH", "data/arbitrage/arbitrage.db")


def now_ms() -> int:
    return int(time.time() * 1000)


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _redact_sensitive_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redacted_value(item) if _sensitive_key(str(key)) else _redact_sensitive_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive_payload(item) for item in value]
    return value


def _redacted_value(value: Any) -> Any:
    return None if value is None else "[REDACTED]"


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized.startswith(("no_", "not_")):
        return False
    if normalized in {"token", "access_token", "refresh_token", "id_token", "session_token", "bearer_token", "auth_token"}:
        return True
    return any(
        part in normalized
        for part in (
            "api_key",
            "apikey",
            "secret",
            "token_secret",
            "private_key",
            "password",
            "authorization",
            "signature",
            "signed_payload",
            "raw_transaction",
        )
    )


def _payload_float(payload: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in payload:
            continue
        try:
            return float(payload.get(key))
        except (TypeError, ValueError):
            return None
    return None


def _payload_int(payload: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key not in payload:
            continue
        try:
            return int(payload.get(key))
        except (TypeError, ValueError):
            return None
    return None


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in (
        "payload_json",
        "details_json",
        "depth_json",
        "quote_json",
        "blocker_reasons_json",
        "warning_reasons_json",
        "route_status_json",
        "decision_payload_json",
    ):
        if key in out:
            fallback = [] if key.endswith("_reasons_json") else {}
            out[key[:-5] if key.endswith("_json") else key] = _loads(out.get(key), fallback)
    if "occurred_at_ms" in out and "occurred_at" not in out:
        out["occurred_at"] = out["occurred_at_ms"]
    payload = out.get("payload")
    if "event_type" in out and isinstance(payload, dict) and "approval_id" in payload:
        out["approval_id"] = payload.get("approval_id")
    return out


def _refresh_opportunity_route_state(conn: sqlite3.Connection, opportunity_id: int, *, stamp: int) -> None:
    routes = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM arb_routes WHERE opportunity_id = ? ORDER BY id",
            (int(opportunity_id),),
        ).fetchall()
    ]
    if not routes:
        return

    viable_routes = [route for route in routes if _route_is_viable(route)]
    selected = next((route for route in routes if int(route.get("selected") or 0) == 1), None)
    if not selected or not _route_is_viable(selected):
        selected = _best_route(viable_routes)
    selected_route_id = int(selected["id"]) if selected else None

    conn.execute(
        "UPDATE arb_routes SET selected = CASE WHEN id = ? THEN 1 ELSE 0 END, updated_at_ms = ? WHERE opportunity_id = ?",
        (selected_route_id or -1, int(stamp), int(opportunity_id)),
    )

    safety_status, lifecycle_status = _opportunity_status_from_routes(routes)
    conn.execute(
        """
        UPDATE arb_opportunities
        SET safety_status = ?,
            lifecycle_status = ?,
            selected_route_id = ?,
            updated_at_ms = ?
        WHERE id = ?
        """,
        (
            safety_status,
            lifecycle_status,
            selected_route_id,
            int(stamp),
            int(opportunity_id),
        ),
    )


def _route_is_viable(route: dict[str, Any]) -> bool:
    return (
        str(route.get("route_status") or "") in {"OPEN", "WARN", "DONE"}
        and str(route.get("safety_status") or "") in {"PASS", "WARN"}
        and not _route_has_blockers(route)
    )


def _route_is_open_pass(route: dict[str, Any]) -> bool:
    return (
        str(route.get("route_status") or "") in {"OPEN", "DONE"}
        and str(route.get("safety_status") or "") == "PASS"
        and int(route.get("edge_worst_verified") or 0) == 1
        and not _route_has_blockers(route)
    )


def _route_has_blockers(route: dict[str, Any]) -> bool:
    blockers = _loads(str(route.get("blocker_reasons_json") or "[]"), [])
    return isinstance(blockers, list) and len(blockers) > 0


def _best_route(routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not routes:
        return None
    route_status_rank = {"OPEN": 0, "DONE": 0, "WARN": 1}
    safety_rank = {"PASS": 0, "WARN": 1}
    return sorted(
        routes,
        key=lambda route: (
            route_status_rank.get(str(route.get("route_status") or ""), 9),
            safety_rank.get(str(route.get("safety_status") or ""), 9),
            -int(route.get("edge_worst_verified") or 0),
            -float(route.get("edge_worst_bps") or 0.0),
            int(route.get("id") or 0),
        ),
    )[0]


def _opportunity_status_from_routes(routes: list[dict[str, Any]]) -> tuple[str, str]:
    if any(_route_is_open_pass(route) for route in routes):
        return "PASS", "PRECHECK_PASS"
    if any(_route_is_viable(route) for route in routes):
        return "WARN", "PRECHECK_WARN"
    if any(str(route.get("safety_status") or "") == "ERROR" for route in routes):
        return "ERROR", "BLOCKED"
    if any(
        str(route.get("safety_status") or "") == "BLOCK"
        or str(route.get("route_status") or "") == "BLOCKED"
        for route in routes
    ):
        return "BLOCK", "BLOCKED"
    return "WARN", "PRECHECK_WARN"


class ArbitrageStore:
    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = str(path)

    @contextmanager
    def conn(self):
        secure_sqlite_artifacts(self.path)
        conn = sqlite3.connect(self.path, timeout=120)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 120000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
            secure_sqlite_artifacts(self.path)

    def init(self) -> None:
        secure_sqlite_artifacts(self.path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as conn:
            _run_migrations(conn)
            _seed_defaults(conn)

    def ensure_asset(self, *, symbol: str, name: str = "", canonical_source: str = "manual") -> int:
        symbol = str(symbol).upper().strip()
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO arb_assets(symbol, name, canonical_source)
                VALUES (?, ?, ?)
                """,
                (symbol, str(name or ""), str(canonical_source or "manual")),
            )
            row = conn.execute("SELECT id FROM arb_assets WHERE symbol = ?", (symbol,)).fetchone()
            return int(row["id"])

    def ensure_token(
        self,
        *,
        asset_id: int,
        chain_id: str,
        chain_code: str,
        contract_address: str,
        decimals: int,
        wrapped_kind: str = "",
        bridge_group: str = "",
    ) -> int:
        contract_address = str(contract_address).lower().strip()
        chain_id = str(chain_id).strip()
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO arb_tokens(
                    asset_id, chain_id, chain_code, contract_address, decimals, wrapped_kind, bridge_group
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(asset_id),
                    chain_id,
                    str(chain_code).upper().strip(),
                    contract_address,
                    int(decimals),
                    str(wrapped_kind or ""),
                    str(bridge_group or ""),
                ),
            )
            row = conn.execute(
                "SELECT id FROM arb_tokens WHERE chain_id = ? AND contract_address = ?",
                (chain_id, contract_address),
            ).fetchone()
            return int(row["id"])

    def ensure_venue(self, venue_code: str, venue_type: str, name: str = "") -> int:
        venue_code = str(venue_code).upper().strip()
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO arb_venues(venue_code, venue_type, name)
                VALUES (?, ?, ?)
                """,
                (venue_code, str(venue_type).upper().strip(), str(name or venue_code)),
            )
            row = conn.execute("SELECT id FROM arb_venues WHERE venue_code = ?", (venue_code,)).fetchone()
            return int(row["id"])

    def ensure_market(
        self,
        *,
        market_key: str,
        asset_id: int,
        venue_id: int,
        market_type: str,
        chain_code: str = "",
        pool_address: str = "",
        market_symbol: str = "",
        quote_asset: str = "",
        deposit_network: str = "",
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_markets(
                    market_key, asset_id, venue_id, market_type, chain_code, pool_address,
                    market_symbol, quote_asset, deposit_network, payload_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_key) DO UPDATE SET
                    asset_id = excluded.asset_id,
                    venue_id = excluded.venue_id,
                    market_type = excluded.market_type,
                    chain_code = excluded.chain_code,
                    pool_address = excluded.pool_address,
                    market_symbol = excluded.market_symbol,
                    quote_asset = excluded.quote_asset,
                    deposit_network = excluded.deposit_network,
                    payload_json = excluded.payload_json,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    str(market_key),
                    int(asset_id),
                    int(venue_id),
                    str(market_type),
                    str(chain_code or "").upper(),
                    str(pool_address or "").lower(),
                    str(market_symbol or ""),
                    str(quote_asset or "").upper(),
                    str(deposit_network or "").upper(),
                    _json(payload or {}),
                    now_ms(),
                ),
            )
            row = conn.execute("SELECT id FROM arb_markets WHERE market_key = ?", (str(market_key),)).fetchone()
            return int(row["id"])

    def record_market_tick(
        self,
        *,
        market_id: int,
        source: str,
        observed_at_ms: int,
        raw_price: float | None = None,
        price_usd: float | None = None,
        price_krw: float | None = None,
        best_bid: float | None = None,
        best_ask: float | None = None,
        liquidity_usd: float | None = None,
        volume_24h: float | None = None,
        stale: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO arb_market_ticks(
                    market_id, source, observed_at_ms, raw_price, price_usd, price_krw,
                    best_bid, best_ask, liquidity_usd, volume_24h, stale, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(market_id),
                    str(source),
                    int(observed_at_ms),
                    raw_price,
                    price_usd,
                    price_krw,
                    best_bid,
                    best_ask,
                    liquidity_usd,
                    volume_24h,
                    1 if stale else 0,
                    _json(payload or {}),
                ),
            )
            row = conn.execute(
                "SELECT id FROM arb_market_ticks WHERE market_id = ? AND source = ? AND observed_at_ms = ?",
                (int(market_id), str(source), int(observed_at_ms)),
            ).fetchone()
            return int(row["id"])

    def record_orderbook_snapshot(
        self,
        *,
        market_id: int,
        source: str,
        observed_at_ms: int,
        best_bid: float | None,
        best_ask: float | None,
        depth: list[dict[str, Any]],
        stale: bool = False,
    ) -> int:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO arb_orderbook_snapshots(
                    market_id, source, observed_at_ms, best_bid, best_ask, depth_json, stale
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (int(market_id), str(source), int(observed_at_ms), best_bid, best_ask, _json(depth), 1 if stale else 0),
            )
            row = conn.execute(
                "SELECT id FROM arb_orderbook_snapshots WHERE market_id = ? AND source = ? AND observed_at_ms = ?",
                (int(market_id), str(source), int(observed_at_ms)),
            ).fetchone()
            return int(row["id"])

    def upsert_opportunity(
        self,
        *,
        opportunity_key: str,
        asset_id: int,
        anomaly_type: str,
        lifecycle_status: str,
        safety_status: str,
        buy_market_id: int,
        sell_market_id: int,
        spread_bps: float,
        edge_expected_bps: float,
        edge_worst_bps: float,
        first_seen_at_ms: int,
        last_seen_at_ms: int,
        source_signalhub_event_id: str = "",
        payload: dict[str, Any] | None = None,
        emit_event: bool = True,
    ) -> int:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_opportunities(
                    opportunity_key, asset_id, anomaly_type, lifecycle_status, safety_status,
                    buy_market_id, sell_market_id, spread_bps, edge_expected_bps, edge_worst_bps,
                    first_seen_at_ms, last_seen_at_ms, source_signalhub_event_id, payload_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_key) DO UPDATE SET
                    lifecycle_status = excluded.lifecycle_status,
                    safety_status = excluded.safety_status,
                    buy_market_id = excluded.buy_market_id,
                    sell_market_id = excluded.sell_market_id,
                    spread_bps = excluded.spread_bps,
                    edge_expected_bps = excluded.edge_expected_bps,
                    edge_worst_bps = excluded.edge_worst_bps,
                    last_seen_at_ms = excluded.last_seen_at_ms,
                    source_signalhub_event_id = excluded.source_signalhub_event_id,
                    payload_json = excluded.payload_json,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    str(opportunity_key),
                    int(asset_id),
                    str(anomaly_type),
                    str(lifecycle_status),
                    str(safety_status),
                    int(buy_market_id),
                    int(sell_market_id),
                    float(spread_bps),
                    float(edge_expected_bps),
                    float(edge_worst_bps),
                    int(first_seen_at_ms),
                    int(last_seen_at_ms),
                    str(source_signalhub_event_id or ""),
                    _json(payload or {}),
                    now_ms(),
                ),
            )
            row = conn.execute("SELECT id FROM arb_opportunities WHERE opportunity_key = ?", (str(opportunity_key),)).fetchone()
            opportunity_id = int(row["id"])
        if emit_event:
            self.append_event(
                event_type="opportunity.upsert",
                opportunity_id=opportunity_id,
                payload={
                    "opportunity_id": opportunity_id,
                    "opportunity_key": str(opportunity_key),
                    "anomaly_type": str(anomaly_type),
                    "status": str(lifecycle_status),
                    "safety_status": str(safety_status),
                    "spread_bps": float(spread_bps),
                    "edge_worst_bps": float(edge_worst_bps),
                },
            )
        return opportunity_id

    def upsert_route(
        self,
        *,
        route_key: str,
        opportunity_id: int,
        route_type: str,
        buy_market_id: int,
        sell_market_id: int,
        safety_status: str,
        route_status: str,
        edge_expected_bps: float,
        edge_worst_bps: float,
        selected: bool = False,
        quote_fresh_until_ms: int | None = None,
        edge_worst_verified: bool = False,
        blocker_reasons: list[str] | None = None,
        warning_reasons: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_routes(
                    route_key, opportunity_id, route_type, buy_market_id, sell_market_id,
                    safety_status, route_status, edge_expected_bps, edge_worst_bps,
                    blocker_reasons_json, warning_reasons_json, selected, quote_fresh_until_ms,
                    edge_worst_verified, payload_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_key) DO UPDATE SET
                    opportunity_id = excluded.opportunity_id,
                    route_type = excluded.route_type,
                    buy_market_id = excluded.buy_market_id,
                    sell_market_id = excluded.sell_market_id,
                    safety_status = excluded.safety_status,
                    route_status = excluded.route_status,
                    edge_expected_bps = excluded.edge_expected_bps,
                    edge_worst_bps = excluded.edge_worst_bps,
                    blocker_reasons_json = excluded.blocker_reasons_json,
                    warning_reasons_json = excluded.warning_reasons_json,
                    selected = excluded.selected,
                    quote_fresh_until_ms = excluded.quote_fresh_until_ms,
                    edge_worst_verified = excluded.edge_worst_verified,
                    payload_json = excluded.payload_json,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    str(route_key),
                    int(opportunity_id),
                    str(route_type),
                    int(buy_market_id),
                    int(sell_market_id),
                    str(safety_status),
                    str(route_status),
                    float(edge_expected_bps),
                    float(edge_worst_bps),
                    _json(blocker_reasons or []),
                    _json(warning_reasons or []),
                    1 if selected else 0,
                    quote_fresh_until_ms,
                    1 if edge_worst_verified else 0,
                    _json(payload or {}),
                    now_ms(),
                ),
            )
            row = conn.execute("SELECT id FROM arb_routes WHERE route_key = ?", (str(route_key),)).fetchone()
            route_id = int(row["id"])
            if selected:
                conn.execute(
                    "UPDATE arb_routes SET selected = 0, updated_at_ms = ? WHERE opportunity_id = ? AND id != ?",
                    (now_ms(), int(opportunity_id), route_id),
                )
                conn.execute(
                    "UPDATE arb_routes SET selected = 1, updated_at_ms = ? WHERE id = ?",
                    (now_ms(), route_id),
                )
                conn.execute("UPDATE arb_opportunities SET selected_route_id = ? WHERE id = ?", (route_id, int(opportunity_id)))
            return route_id

    def record_route_quote(
        self,
        *,
        route_id: int,
        leg_type: str,
        source: str,
        destination: str,
        amount_in_raw: str = "",
        amount_out_expected_raw: str = "",
        amount_out_min_raw: str = "",
        amount_in_value_krw: float | None = None,
        amount_out_expected_krw: float | None = None,
        amount_out_min_krw: float | None = None,
        gas_krw: float | None = None,
        fee_krw: float | None = None,
        price_impact_bps: float | None = None,
        eta_seconds: int | None = None,
        observed_at_ms: int | None = None,
        expires_at_ms: int | None = None,
        stale: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO arb_route_quotes(
                    route_id, leg_type, source, destination, amount_in_raw, amount_out_expected_raw,
                    amount_out_min_raw, amount_in_value_krw, amount_out_expected_krw,
                    amount_out_min_krw, gas_krw, fee_krw, price_impact_bps, eta_seconds,
                    observed_at_ms, expires_at_ms, stale, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(route_id),
                    str(leg_type),
                    str(source),
                    str(destination),
                    str(amount_in_raw or ""),
                    str(amount_out_expected_raw or ""),
                    str(amount_out_min_raw or ""),
                    amount_in_value_krw,
                    amount_out_expected_krw,
                    amount_out_min_krw,
                    gas_krw,
                    fee_krw,
                    price_impact_bps,
                    eta_seconds,
                    int(observed_at_ms or now_ms()),
                    expires_at_ms,
                    1 if stale else 0,
                    _json(payload or {}),
                ),
            )
            return int(cur.lastrowid)

    def enable_strategy_mode(
        self,
        profile_code: str,
        *,
        paper_enabled: bool | None = None,
        one_click_enabled: bool | None = None,
        auto_small_enabled: bool | None = None,
        live_full_enabled: bool | None = None,
    ) -> None:
        fields = {
            "paper_enabled": paper_enabled,
            "one_click_enabled": one_click_enabled,
            "auto_small_enabled": auto_small_enabled,
            "live_full_enabled": live_full_enabled,
        }
        sets = [f"{key} = ?" for key, value in fields.items() if value is not None]
        values = [1 if value else 0 for value in fields.values() if value is not None]
        if not sets:
            return
        values.append(str(profile_code))
        with self.conn() as conn:
            conn.execute(
                f"UPDATE arb_strategy_profiles SET {', '.join(sets)}, updated_at_ms = ? WHERE profile_code = ?",
                [*values[:-1], now_ms(), values[-1]],
            )

    def configure_strategy_profile(
        self,
        profile_code: str,
        *,
        paper_enabled: bool | None = None,
        one_click_enabled: bool | None = None,
        auto_small_enabled: bool | None = None,
        live_full_enabled: bool | None = None,
        max_trade_krw: float | None = None,
        max_daily_loss_krw: float | None = None,
        min_edge_worst_bps: float | None = None,
        active: bool | None = None,
    ) -> None:
        bool_fields = {
            "paper_enabled": paper_enabled,
            "one_click_enabled": one_click_enabled,
            "auto_small_enabled": auto_small_enabled,
            "live_full_enabled": live_full_enabled,
            "active": active,
        }
        numeric_fields = {
            "max_trade_krw": max_trade_krw,
            "max_daily_loss_krw": max_daily_loss_krw,
            "min_edge_worst_bps": min_edge_worst_bps,
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in bool_fields.items():
            if value is not None:
                sets.append(f"{key} = ?")
                values.append(1 if value else 0)
        for key, value in numeric_fields.items():
            if value is not None:
                sets.append(f"{key} = ?")
                values.append(float(value))
        if not sets:
            return
        with self.conn() as conn:
            conn.execute(
                f"UPDATE arb_strategy_profiles SET {', '.join(sets)}, updated_at_ms = ? WHERE profile_code = ?",
                [*values, now_ms(), str(profile_code)],
            )

    def ensure_wallet(
        self,
        *,
        wallet_key: str,
        chain_code: str,
        address: str,
        wallet_type: str,
        mode: str,
        enabled: bool,
        withdrawal_enabled: bool,
    ) -> int:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_wallets(wallet_key, chain_code, address, wallet_type, mode, enabled, withdrawal_enabled, updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_key) DO UPDATE SET
                    chain_code = excluded.chain_code,
                    address = excluded.address,
                    wallet_type = excluded.wallet_type,
                    mode = excluded.mode,
                    enabled = excluded.enabled,
                    withdrawal_enabled = excluded.withdrawal_enabled,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    str(wallet_key),
                    str(chain_code).upper(),
                    str(address).lower(),
                    str(wallet_type).upper(),
                    str(mode),
                    1 if enabled else 0,
                    1 if withdrawal_enabled else 0,
                    now_ms(),
                ),
            )
            row = conn.execute("SELECT id FROM arb_wallets WHERE wallet_key = ?", (str(wallet_key),)).fetchone()
            return int(row["id"])

    def mark_route_stale(self, route_id: int, *, quote_fresh_until_ms: int) -> None:
        with self.conn() as conn:
            conn.execute(
                "UPDATE arb_routes SET quote_fresh_until_ms = ?, route_status = 'STALE', updated_at_ms = ? WHERE id = ?",
                (int(quote_fresh_until_ms), now_ms(), int(route_id)),
            )

    def set_route_freshness(self, route_id: int, freshness: dict[str, int]) -> None:
        stamp = now_ms()
        with self.conn() as conn:
            for source_key, fresh_until_ms in freshness.items():
                conn.execute(
                    """
                    INSERT INTO arb_route_freshness(route_id, source_key, fresh_until_ms, updated_at_ms)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(route_id, source_key) DO UPDATE SET
                        fresh_until_ms = excluded.fresh_until_ms,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (int(route_id), str(source_key), int(fresh_until_ms), stamp),
                )

    def fetch_route_freshness(self, route_id: int) -> dict[str, int]:
        with self.conn() as conn:
            return {
                str(row["source_key"]): int(row["fresh_until_ms"])
                for row in conn.execute(
                    "SELECT source_key, fresh_until_ms FROM arb_route_freshness WHERE route_id = ?",
                    (int(route_id),),
                ).fetchall()
            }

    def set_route_edge_verification(self, route_id: int, *, verified: bool) -> None:
        with self.conn() as conn:
            conn.execute(
                "UPDATE arb_routes SET edge_worst_verified = ?, updated_at_ms = ? WHERE id = ?",
                (1 if verified else 0, now_ms(), int(route_id)),
            )

    def route_has_operator_approval(self, route_id: int, *, mode: str | None = None) -> bool:
        clauses = ["route_id = ?", "status = 'APPROVED'"]
        params: list[Any] = [int(route_id)]
        if mode is not None and str(mode).strip():
            clauses.append("(mode = ? OR COALESCE(mode, '') = '')")
            params.append(str(mode).strip())
        with self.conn() as conn:
            row = conn.execute(
                f"""
                SELECT 1 FROM arb_operator_approvals
                WHERE {' AND '.join(clauses)}
                LIMIT 1
                """,
                params,
            ).fetchone()
            return row is not None

    def find_matching_operator_approval(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        mode: str,
        trade_amount_krw: float,
        now_at_ms: int,
        allow_consumed_run_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Return an approved operator record matching live execution scope.

        Live approvals are intentionally matched in Python because the approval
        evidence is JSON and may be extended with provider-specific fields.
        """
        for approval in self.list_operator_approvals(
            opportunity_id=int(opportunity_id),
            route_id=int(route_id),
            mode=str(mode),
            status="APPROVED",
            limit=100,
        ):
            consumed_run_id = int(approval.get("consumed_run_id") or 0)
            if consumed_run_id > 0 and consumed_run_id != int(allow_consumed_run_id or 0):
                continue
            payload = approval.get("payload") if isinstance(approval.get("payload"), Mapping) else {}
            approved_amount = _payload_float(payload, "trade_amount_krw", "amount_krw", "approved_amount_krw")
            expires_at_ms = _payload_int(
                payload,
                "expires_at_ms",
                "approval_expires_at_ms",
                "valid_until_ms",
                "window_expires_at_ms",
            )
            if approved_amount is None or abs(float(approved_amount) - float(trade_amount_krw)) > 0.000001:
                continue
            if expires_at_ms is None or int(expires_at_ms) <= int(now_at_ms):
                continue
            return approval
        return None

    def consume_operator_approval(self, approval_id: int, *, run_id: int) -> dict[str, Any] | None:
        stamp = now_ms()
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM arb_operator_approvals WHERE id = ?",
                (int(approval_id),),
            ).fetchone()
            approval = _row(row)
            if not approval:
                return None
            if int(approval.get("consumed_run_id") or 0) > 0:
                return approval
            conn.execute(
                """
                UPDATE arb_operator_approvals
                SET consumed_run_id = ?, consumed_at_ms = ?
                WHERE id = ? AND COALESCE(consumed_run_id, 0) = 0
                """,
                (int(run_id), stamp, int(approval_id)),
            )
            return _row(
                conn.execute(
                    "SELECT * FROM arb_operator_approvals WHERE id = ?",
                    (int(approval_id),),
                ).fetchone()
            )

    def request_operator_approval(
        self,
        *,
        approval_key: str,
        opportunity_id: int,
        route_id: int,
        run_id: int | None = None,
        mode: str = "one_click",
        requested_by: str = "system",
        reason: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stamp = now_ms()
        normalized_mode = str(mode or "one_click").strip() or "one_click"
        normalized_key = str(approval_key or "").strip()
        if not normalized_key:
            normalized_key = f"operator_approval:{int(opportunity_id)}:{int(route_id)}:{run_id or 'none'}:{normalized_mode}"
        normalized_run_id = int(run_id) if run_id is not None else None
        normalized_requested_by = str(requested_by or "system").strip() or "system"
        normalized_reason = str(reason or "").strip()
        payload_data = _redact_sensitive_payload(payload or {})
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO arb_operator_approvals(
                    approval_key, run_id, opportunity_id, route_id, mode, requested_by, reason,
                    status, requested_at_ms, operator, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, '', ?)
                """,
                (
                    normalized_key,
                    normalized_run_id,
                    int(opportunity_id),
                    int(route_id),
                    normalized_mode,
                    normalized_requested_by,
                    normalized_reason,
                    stamp,
                    _json(payload_data),
                ),
            )
            created = cur.rowcount == 1
            row = conn.execute(
                "SELECT * FROM arb_operator_approvals WHERE approval_key = ?",
                (normalized_key,),
            ).fetchone()
            approval = _row(row) or {}
            if approval:
                existing_run_id = approval.get("run_id")
                existing_run_id = int(existing_run_id) if existing_run_id is not None else None
                existing_mode = str(approval.get("mode") or "").strip()
                if (
                    int(approval.get("opportunity_id") or 0) != int(opportunity_id)
                    or int(approval.get("route_id") or 0) != int(route_id)
                    or existing_run_id != normalized_run_id
                    or (existing_mode and existing_mode != normalized_mode)
                ):
                    raise ValueError("approval_key_conflict")
            if created:
                request_payload = {
                    "approval_id": approval.get("id"),
                    "approval_key": normalized_key,
                    "mode": normalized_mode,
                    "requested_by": normalized_requested_by,
                    "reason": normalized_reason,
                    "status": "PENDING",
                    "evidence": payload_data,
                }
                conn.execute(
                    """
                    INSERT INTO arb_event_log(
                        event_id, event_type, opportunity_id, route_id, run_id, severity, payload_json, occurred_at_ms
                    ) VALUES (?, 'operator_approval.requested', ?, ?, ?, 'warning', ?, ?)
                    """,
                    (
                        f"evt_{uuid.uuid4().hex}",
                        int(opportunity_id),
                        int(route_id),
                        int(run_id) if run_id is not None else None,
                        _json(request_payload),
                        stamp,
                    ),
                )
                alert_source = f"operator_approval_requested:{normalized_key}"
                alert_cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO arb_alerts(
                        opportunity_id, channel, chat_id, message_id, status, payload_json, created_at_ms
                    ) VALUES (?, 'db_sse', ?, '', 'ACTIVE', ?, ?)
                    """,
                    (
                        int(opportunity_id),
                        alert_source,
                        _json(
                            {
                                **request_payload,
                                "alert_type": "operator_approval_requested",
                                "alert_source": alert_source,
                                "external_notification": False,
                            }
                        ),
                        stamp,
                    ),
                )
                if alert_cur.rowcount == 1:
                    alert_row = conn.execute(
                        """
                        SELECT * FROM arb_alerts
                        WHERE opportunity_id = ? AND channel = 'db_sse' AND chat_id = ?
                        """,
                        (int(opportunity_id), alert_source),
                    ).fetchone()
                    conn.execute(
                        """
                        INSERT INTO arb_event_log(
                            event_id, event_type, opportunity_id, route_id, run_id, severity, payload_json, occurred_at_ms
                        ) VALUES (?, 'alert.operator_approval_requested', ?, ?, ?, 'warning', ?, ?)
                        """,
                        (
                            f"evt_{uuid.uuid4().hex}",
                            int(opportunity_id),
                            int(route_id),
                            int(run_id) if run_id is not None else None,
                            _json(
                                {
                                    **request_payload,
                                    "alert_id": int(alert_row["id"]) if alert_row else None,
                                    "alert_source": alert_source,
                                    "external_notification": False,
                                }
                            ),
                            stamp,
                        ),
                    )
            approval["created"] = created
            return approval

    def decide_operator_approval(
        self,
        approval_id: int,
        *,
        status: str,
        operator: str,
        decision_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        final_status = str(status or "").strip().upper()
        if final_status not in {"APPROVED", "REJECTED"}:
            raise ValueError("invalid_approval_decision")
        normalized_operator = str(operator or "api").strip() or "api"
        decision_payload_data = _redact_sensitive_payload(decision_payload or {})
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM arb_operator_approvals WHERE id = ?",
                (int(approval_id),),
            ).fetchone()
            if row is None:
                return None

            current = str(row["status"] or "").strip().upper()
            if current == final_status:
                return _row(row)
            if current in {"APPROVED", "REJECTED"}:
                raise ValueError(f"approval_already_{current.lower()}")

            stamp = now_ms()
            conn.execute(
                """
                UPDATE arb_operator_approvals
                SET status = ?,
                    decided_at_ms = ?,
                    operator = ?,
                    decision_payload_json = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    stamp,
                    normalized_operator,
                    _json(decision_payload_data),
                    int(approval_id),
                ),
            )
            updated = _row(
                conn.execute(
                    "SELECT * FROM arb_operator_approvals WHERE id = ?",
                    (int(approval_id),),
                ).fetchone()
            )
            if updated:
                conn.execute(
                    """
                    INSERT INTO arb_event_log(
                        event_id, event_type, opportunity_id, route_id, run_id, severity, payload_json, occurred_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"evt_{uuid.uuid4().hex}",
                        "operator_approval.approved" if final_status == "APPROVED" else "operator_approval.rejected",
                        updated.get("opportunity_id"),
                        updated.get("route_id"),
                        updated.get("run_id"),
                        "info" if final_status == "APPROVED" else "warning",
                        _json(
                            {
                                "approval_id": updated.get("id"),
                                "approval_key": updated.get("approval_key"),
                                "mode": updated.get("mode"),
                                "status": final_status,
                                "operator": normalized_operator,
                                "decided_at_ms": stamp,
                                "decision_payload": decision_payload_data,
                            }
                        ),
                        stamp,
                    ),
                )
            return updated

    def get_operator_approval(self, approval_id: int) -> dict[str, Any] | None:
        with self.conn() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM arb_operator_approvals WHERE id = ?",
                    (int(approval_id),),
                ).fetchone()
            )

    def get_operator_approval_by_key(self, approval_key: str) -> dict[str, Any] | None:
        with self.conn() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM arb_operator_approvals WHERE approval_key = ?",
                    (str(approval_key),),
                ).fetchone()
            )

    def get_latest_operator_approval(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        mode: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["opportunity_id = ?", "route_id = ?"]
        params: list[Any] = [int(opportunity_id), int(route_id)]
        if mode is not None and str(mode).strip():
            clauses.append("mode = ?")
            params.append(str(mode).strip())
        with self.conn() as conn:
            return _row(
                conn.execute(
                    f"""
                    SELECT * FROM arb_operator_approvals
                    WHERE {' AND '.join(clauses)}
                    ORDER BY COALESCE(decided_at_ms, requested_at_ms) DESC, id DESC
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
            )

    def list_operator_approvals(
        self,
        *,
        opportunity_id: int | None = None,
        route_id: int | None = None,
        mode: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            params.append(int(opportunity_id))
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(int(route_id))
        if mode is not None and str(mode).strip():
            clauses.append("mode = ?")
            params.append(str(mode).strip())
        if status is not None and str(status).strip():
            clauses.append("status = ?")
            params.append(str(status).strip().upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self.conn() as conn:
            return [
                _row(row)
                for row in conn.execute(
                    f"""
                    SELECT * FROM arb_operator_approvals
                    {where}
                    ORDER BY requested_at_ms DESC, id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            ]

    def summarize_operator_approvals(
        self,
        *,
        opportunity_id: int | None = None,
        route_id: int | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            params.append(int(opportunity_id))
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(int(route_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.conn() as conn:
            rows = conn.execute(
                f"""
                SELECT status, COUNT(*) AS count
                FROM arb_operator_approvals
                {where}
                GROUP BY status
                """,
                params,
            ).fetchall()
            by_status = {str(row["status"]): int(row["count"]) for row in rows}
            latest = _row(
                conn.execute(
                    f"""
                    SELECT * FROM arb_operator_approvals
                    {where}
                    ORDER BY requested_at_ms DESC, id DESC
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
            )
            return {"total": sum(by_status.values()), "by_status": by_status, "latest": latest}

    def get_market_detail(self, market_id: int) -> dict[str, Any] | None:
        with self.conn() as conn:
            market = _row(
                conn.execute(
                    """
                    SELECT m.*, v.venue_code, v.venue_type
                    FROM arb_markets m
                    JOIN arb_venues v ON v.id = m.venue_id
                    WHERE m.id = ?
                    """,
                    (int(market_id),),
                ).fetchone()
            )
            if not market:
                return None
            token = _row(
                conn.execute(
                    """
                    SELECT contract_address, decimals
                    FROM arb_tokens
                    WHERE asset_id = ?
                      AND UPPER(chain_code) = UPPER(?)
                    ORDER BY id
                    LIMIT 1
                    """,
                    (int(market["asset_id"]), str(market.get("chain_code") or "")),
                ).fetchone()
            )
            return {
                "id": market["id"],
                "venue": market.get("venue_code"),
                "venue_type": market.get("venue_type"),
                "chain": market.get("chain_code"),
                "market": market.get("market_symbol") or market.get("market_key"),
                "market_key": market.get("market_key"),
                "market_type": market.get("market_type"),
                "token_ca": (token or {}).get("contract_address", ""),
                "pool_ca": market.get("pool_address") or "",
                "quote_asset": market.get("quote_asset") or "",
                "deposit_network": market.get("deposit_network") or "",
            }

    def get_strategy_profile(self, profile_code: str = "default") -> dict[str, Any] | None:
        with self.conn() as conn:
            return _row(conn.execute("SELECT * FROM arb_strategy_profiles WHERE profile_code = ?", (profile_code,)).fetchone())

    def get_kill_switches(self) -> list[dict[str, Any]]:
        with self.conn() as conn:
            return [_row(r) for r in conn.execute("SELECT * FROM arb_kill_switches ORDER BY switch_code").fetchall()]

    def is_kill_switch_active(self) -> bool:
        with self.conn() as conn:
            row = conn.execute("SELECT 1 FROM arb_kill_switches WHERE enabled = 1 LIMIT 1").fetchone()
            return row is not None

    def has_execution_wallet(self, mode: str, *, route_id: int | None = None) -> tuple[bool, str]:
        if str(mode) == "paper":
            return True, ""
        with self.conn() as conn:
            bad = conn.execute(
                "SELECT wallet_key FROM arb_wallets WHERE enabled = 1 AND withdrawal_enabled = 1 LIMIT 1"
            ).fetchone()
            if bad:
                return False, "cex_withdrawal_permission_must_be_disabled"
            if route_id is None:
                bad_venue = conn.execute(
                    "SELECT venue_code FROM arb_venues WHERE withdrawal_enabled = 1 LIMIT 1"
                ).fetchone()
            else:
                bad_venue = conn.execute(
                    """
                    SELECT v.venue_code
                    FROM arb_routes r
                    JOIN arb_markets m ON m.id IN (r.buy_market_id, r.sell_market_id)
                    JOIN arb_venues v ON v.id = m.venue_id
                    WHERE r.id = ?
                      AND v.withdrawal_enabled = 1
                    LIMIT 1
                    """,
                    (int(route_id),),
                ).fetchone()
            if bad_venue:
                return False, "cex_withdrawal_permission_must_be_disabled"
            row = conn.execute(
                """
                SELECT 1 FROM arb_wallets
                WHERE enabled = 1
                  AND wallet_type = 'HOT'
                  AND mode IN (?, 'live_full', 'all')
                LIMIT 1
                """,
                (str(mode),),
            ).fetchone()
            if not row:
                return False, "missing_hot_wallet"
            return True, ""

    def get_opportunity(self, opportunity_id: int) -> dict[str, Any] | None:
        with self.conn() as conn:
            return _row(conn.execute("SELECT * FROM arb_opportunities WHERE id = ?", (int(opportunity_id),)).fetchone())

    def get_route(self, route_id: int) -> dict[str, Any] | None:
        with self.conn() as conn:
            return _row(conn.execute("SELECT * FROM arb_routes WHERE id = ?", (int(route_id),)).fetchone())

    def get_execution_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.conn() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM arb_execution_runs WHERE idempotency_key = ?",
                    (str(idempotency_key),),
                ).fetchone()
            )

    def get_execution_run(self, run_id: int) -> dict[str, Any] | None:
        with self.conn() as conn:
            return _row(conn.execute("SELECT * FROM arb_execution_runs WHERE id = ?", (int(run_id),)).fetchone())

    def fetch_execution_runs(
        self,
        *,
        opportunity_id: int | None = None,
        route_id: int | None = None,
        mode: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            params.append(int(opportunity_id))
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(int(route_id))
        if mode is not None:
            clauses.append("mode = ?")
            params.append(str(mode))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self.conn() as conn:
            return [
                _row(r)
                for r in conn.execute(
                    f"""
                    SELECT * FROM arb_execution_runs
                    {where}
                    ORDER BY started_at_ms DESC, id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            ]

    def insert_execution_run(
        self,
        *,
        execution_key: str,
        idempotency_key: str,
        opportunity_id: int,
        route_id: int,
        mode: str,
        status: str,
        requested_by: str,
        error_code: str = "",
        error_msg: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = now_ms()
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO arb_execution_runs(
                    execution_key, idempotency_key, opportunity_id, route_id, mode, status,
                    requested_by, started_at_ms, error_code, error_msg, payload_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(execution_key),
                    str(idempotency_key),
                    int(opportunity_id),
                    int(route_id),
                    str(mode),
                    str(status),
                    str(requested_by or "system"),
                    started,
                    str(error_code or ""),
                    str(error_msg or ""),
                    _json(_redact_sensitive_payload(payload or {})),
                    started,
                ),
            )
            row = conn.execute("SELECT * FROM arb_execution_runs WHERE idempotency_key = ?", (str(idempotency_key),)).fetchone()
            out = _row(row) or {}
            out["created"] = cur.rowcount == 1
            return out

    def update_execution_run(self, run_id: int, *, status: str, error_code: str = "", error_msg: str = "") -> dict[str, Any]:
        done = now_ms() if status in {"SETTLED", "FAILED", "ABORTED", "BLOCKED", "MANUAL_REVIEW"} else None
        with self.conn() as conn:
            conn.execute(
                """
                UPDATE arb_execution_runs
                SET status = ?, error_code = ?, error_msg = ?,
                    completed_at_ms = COALESCE(?, completed_at_ms),
                    updated_at_ms = ?
                WHERE id = ?
                """,
                (str(status), str(error_code or ""), str(error_msg or ""), done, now_ms(), int(run_id)),
            )
            return _row(conn.execute("SELECT * FROM arb_execution_runs WHERE id = ?", (int(run_id),)).fetchone())

    def insert_execution_step(
        self,
        *,
        run_id: int,
        step_key: str,
        attempt_no: int = 1,
        status: str = "PENDING",
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO arb_execution_steps(run_id, step_key, attempt_no, status, payload_json, updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(run_id),
                    str(step_key),
                    int(attempt_no),
                    str(status),
                    _json(_redact_sensitive_payload(payload or {})),
                    now_ms(),
                ),
            )
            row = conn.execute(
                "SELECT id FROM arb_execution_steps WHERE run_id = ? AND step_key = ? AND attempt_no = ?",
                (int(run_id), str(step_key), int(attempt_no)),
            ).fetchone()
            return int(row["id"])

    def update_execution_step(
        self,
        *,
        run_id: int,
        step_key: str,
        status: str,
        external_ref: str = "",
        error_code: str = "",
        error_msg: str = "",
        started_at_ms: int | None = None,
        completed_at_ms: int | None = None,
        duration_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.conn() as conn:
            conn.execute(
                """
                UPDATE arb_execution_steps
                SET status = ?, external_ref = ?, error_code = ?, error_msg = ?,
                    started_at_ms = COALESCE(?, started_at_ms),
                    completed_at_ms = COALESCE(?, completed_at_ms),
                    duration_ms = COALESCE(?, duration_ms),
                    payload_json = COALESCE(?, payload_json),
                    updated_at_ms = ?
                WHERE run_id = ? AND step_key = ?
                """,
                (
                    str(status),
                    str(external_ref or ""),
                    str(error_code or ""),
                    str(error_msg or ""),
                    started_at_ms,
                    completed_at_ms,
                    duration_ms,
                    _json(_redact_sensitive_payload(payload)) if payload is not None else None,
                    now_ms(),
                    int(run_id),
                    str(step_key),
                ),
            )
            return _row(
                conn.execute(
                    "SELECT * FROM arb_execution_steps WHERE run_id = ? AND step_key = ?",
                    (int(run_id), str(step_key)),
                ).fetchone()
            )

    def fetch_execution_steps(self, run_id: int) -> list[dict[str, Any]]:
        with self.conn() as conn:
            return [
                _row(r)
                for r in conn.execute(
                    "SELECT * FROM arb_execution_steps WHERE run_id = ? ORDER BY id",
                    (int(run_id),),
                ).fetchall()
            ]

    def get_execution_step(self, *, run_id: int, step_key: str, attempt_no: int = 1) -> dict[str, Any] | None:
        with self.conn() as conn:
            return _row(
                conn.execute(
                    """
                    SELECT * FROM arb_execution_steps
                    WHERE run_id = ? AND step_key = ? AND attempt_no = ?
                    """,
                    (int(run_id), str(step_key), int(attempt_no)),
                ).fetchone()
            )

    def upsert_transaction(
        self,
        *,
        chain_id: str,
        tx_hash: str,
        run_id: int | None = None,
        step_id: int | None = None,
        tx_type: str = "",
        status: str,
        nonce: int | None = None,
        submitted_at_ms: int | None = None,
        confirmed_at_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_data = dict(payload or {})
        status_key = str(status or "").strip().upper()
        if not status_key:
            raise ValueError("transaction_status_required")
        if bool(payload_data.get("dry_run")):
            if status_key in {"SUBMITTED", "CONFIRMED", "MINED", "FINALIZED"}:
                raise ValueError("dry_run_transaction_cannot_claim_chain_state")
            payload_data["dry_run"] = True
            payload_data.setdefault("synthetic", True)
            payload_data.setdefault("real_chain_state", False)
            submitted_at_ms = None
            confirmed_at_ms = None

        payload_data = _redact_sensitive_payload(payload_data)
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO arb_transactions(
                    chain_id, tx_hash, run_id, step_id, tx_type, nonce, status,
                    submitted_at_ms, confirmed_at_ms, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chain_id),
                    str(tx_hash),
                    int(run_id) if run_id is not None else None,
                    int(step_id) if step_id is not None else None,
                    str(tx_type or ""),
                    int(nonce) if nonce is not None else None,
                    status_key,
                    submitted_at_ms,
                    confirmed_at_ms,
                    _json(payload_data),
                ),
            )
            row = conn.execute(
                "SELECT * FROM arb_transactions WHERE chain_id = ? AND tx_hash = ?",
                (str(chain_id), str(tx_hash)),
            ).fetchone()
            return _row(row)

    def record_dry_run_transaction(
        self,
        *,
        chain_id: str,
        tx_hash: str,
        run_id: int,
        step_id: int,
        tx_type: str,
        adapter_name: str,
        submit_ref: str = "",
        status: str = "DRY_RUN_SUCCESS",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_data = {
            **dict(payload or {}),
            "dry_run": True,
            "synthetic": True,
            "real_chain_state": False,
            "adapter_name": str(adapter_name),
            "submit_ref": str(submit_ref or ""),
        }
        status_key = str(status or "DRY_RUN_SUCCESS").strip().upper()
        if not status_key.startswith("DRY_RUN"):
            status_key = f"DRY_RUN_{status_key}"
        return self.upsert_transaction(
            chain_id=chain_id,
            tx_hash=tx_hash,
            run_id=run_id,
            step_id=step_id,
            tx_type=tx_type,
            status=status_key,
            submitted_at_ms=None,
            confirmed_at_ms=None,
            payload=payload_data,
        )

    def fetch_transactions_for_run_step(self, run_id: int, step_key: str | None = None) -> list[dict[str, Any]]:
        with self.conn() as conn:
            if step_key is None:
                rows = conn.execute(
                    """
                    SELECT * FROM arb_transactions
                    WHERE run_id = ?
                    ORDER BY id
                    """,
                    (int(run_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT t.*
                    FROM arb_transactions t
                    JOIN arb_execution_steps s ON s.id = t.step_id
                    WHERE t.run_id = ?
                      AND s.step_key = ?
                    ORDER BY t.id
                    """,
                    (int(run_id), str(step_key)),
                ).fetchall()
            return [_row(row) for row in rows]

    def upsert_order(
        self,
        *,
        order_key: str,
        run_id: int,
        step_id: int | None = None,
        venue_code: str,
        market_key: str,
        side: str,
        order_type: str,
        amount_raw: str = "",
        amount_value_krw: float | None = None,
        avg_price_krw: float | None = None,
        status: str,
        external_order_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_key = str(order_key or "").strip()
        if not normalized_key:
            raise ValueError("order_key_required")
        status_key = str(status or "").strip().upper()
        if not status_key:
            raise ValueError("order_status_required")
        stamp = now_ms()
        payload_data = _redact_sensitive_payload(payload or {})
        with self.conn() as conn:
            existing = conn.execute(
                "SELECT * FROM arb_orders WHERE order_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing:
                existing_step_id = existing["step_id"]
                existing_step_id = int(existing_step_id) if existing_step_id is not None else None
                normalized_step_id = int(step_id) if step_id is not None else None
                if int(existing["run_id"]) != int(run_id) or existing_step_id != normalized_step_id:
                    raise ValueError("order_key_conflict")
                conn.execute(
                    """
                    UPDATE arb_orders
                    SET venue_code = ?,
                        market_key = ?,
                        side = ?,
                        order_type = ?,
                        amount_raw = ?,
                        amount_value_krw = ?,
                        avg_price_krw = ?,
                        status = ?,
                        external_order_id = ?,
                        payload_json = ?,
                        updated_at_ms = ?
                    WHERE order_key = ?
                    """,
                    (
                        str(venue_code or "").upper(),
                        str(market_key or ""),
                        str(side or "").upper(),
                        str(order_type or "").upper(),
                        str(amount_raw or ""),
                        amount_value_krw,
                        avg_price_krw,
                        status_key,
                        str(external_order_id or ""),
                        _json(payload_data),
                        stamp,
                        normalized_key,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO arb_orders(
                        order_key, run_id, step_id, venue_code, market_key, side, order_type,
                        amount_raw, amount_value_krw, avg_price_krw, status, external_order_id,
                        payload_json, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_key,
                        int(run_id),
                        int(step_id) if step_id is not None else None,
                        str(venue_code or "").upper(),
                        str(market_key or ""),
                        str(side or "").upper(),
                        str(order_type or "").upper(),
                        str(amount_raw or ""),
                        amount_value_krw,
                        avg_price_krw,
                        status_key,
                        str(external_order_id or ""),
                        _json(payload_data),
                        stamp,
                    ),
                )
            return _row(
                conn.execute(
                    "SELECT * FROM arb_orders WHERE order_key = ?",
                    (normalized_key,),
                ).fetchone()
            )

    def fetch_orders_for_run_step(self, run_id: int, step_key: str | None = None) -> list[dict[str, Any]]:
        with self.conn() as conn:
            if step_key is None:
                rows = conn.execute(
                    """
                    SELECT * FROM arb_orders
                    WHERE run_id = ?
                    ORDER BY id
                    """,
                    (int(run_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT o.*
                    FROM arb_orders o
                    JOIN arb_execution_steps s ON s.id = o.step_id
                    WHERE o.run_id = ?
                      AND s.step_key = ?
                    ORDER BY o.id
                    """,
                    (int(run_id), str(step_key)),
                ).fetchall()
            return [_row(row) for row in rows]

    def upsert_transfer(
        self,
        *,
        transfer_key: str,
        run_id: int,
        step_id: int | None = None,
        from_location: str,
        to_location: str,
        status: str,
        amount_raw: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_key = str(transfer_key or "").strip()
        if not normalized_key:
            raise ValueError("transfer_key_required")
        status_key = str(status or "").strip().upper()
        if not status_key:
            raise ValueError("transfer_status_required")
        stamp = now_ms()
        payload_data = _redact_sensitive_payload(payload or {})
        with self.conn() as conn:
            existing = conn.execute(
                "SELECT * FROM arb_transfers WHERE transfer_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing:
                existing_step_id = existing["step_id"] if "step_id" in existing.keys() else None
                existing_step_id = int(existing_step_id) if existing_step_id is not None else None
                normalized_step_id = int(step_id) if step_id is not None else None
                if int(existing["run_id"]) != int(run_id) or existing_step_id != normalized_step_id:
                    raise ValueError("transfer_key_conflict")
                conn.execute(
                    """
                    UPDATE arb_transfers
                    SET from_location = ?,
                        to_location = ?,
                        status = ?,
                        amount_raw = ?,
                        payload_json = ?,
                        updated_at_ms = ?
                    WHERE transfer_key = ?
                    """,
                    (
                        str(from_location or ""),
                        str(to_location or ""),
                        status_key,
                        str(amount_raw or ""),
                        _json(payload_data),
                        stamp,
                        normalized_key,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO arb_transfers(
                        transfer_key, run_id, step_id, from_location, to_location, status,
                        amount_raw, payload_json, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_key,
                        int(run_id),
                        int(step_id) if step_id is not None else None,
                        str(from_location or ""),
                        str(to_location or ""),
                        status_key,
                        str(amount_raw or ""),
                        _json(payload_data),
                        stamp,
                    ),
                )
            return _row(
                conn.execute(
                    "SELECT * FROM arb_transfers WHERE transfer_key = ?",
                    (normalized_key,),
                ).fetchone()
            )

    def fetch_transfers_for_run_step(self, run_id: int, step_key: str | None = None) -> list[dict[str, Any]]:
        with self.conn() as conn:
            if step_key is None:
                rows = conn.execute(
                    """
                    SELECT * FROM arb_transfers
                    WHERE run_id = ?
                    ORDER BY id
                    """,
                    (int(run_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT t.*
                    FROM arb_transfers t
                    JOIN arb_execution_steps s ON s.id = t.step_id
                    WHERE t.run_id = ?
                      AND s.step_key = ?
                    ORDER BY t.id
                    """,
                    (int(run_id), str(step_key)),
                ).fetchall()
            return [_row(row) for row in rows]

    def get_latest_route_quote(self, route_id: int, *, leg_type: str | None = None) -> dict[str, Any] | None:
        params: list[Any] = [int(route_id)]
        leg_filter = ""
        if leg_type:
            leg_filter = "AND leg_type = ?"
            params.append(str(leg_type))
        with self.conn() as conn:
            return _row(
                conn.execute(
                    f"""
                    SELECT * FROM arb_route_quotes
                    WHERE route_id = ?
                    {leg_filter}
                    ORDER BY observed_at_ms DESC, id DESC
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
            )

    def insert_precheck_run(self, *, run_key: str, opportunity_id: int, route_id: int, status: str) -> int:
        stamp = now_ms()
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_precheck_runs(run_key, opportunity_id, route_id, status, started_at_ms, completed_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET status = excluded.status, completed_at_ms = excluded.completed_at_ms
                """,
                (str(run_key), int(opportunity_id), int(route_id), str(status), stamp, stamp),
            )
            row = conn.execute("SELECT id FROM arb_precheck_runs WHERE run_key = ?", (str(run_key),)).fetchone()
            return int(row["id"])

    def insert_precheck_result(
        self,
        *,
        precheck_run_id: int,
        check_name: str,
        status: str,
        error_code: str = "",
        error_msg: str = "",
        details: dict[str, Any] | None = None,
    ) -> int:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_precheck_results(precheck_run_id, check_name, status, error_code, error_msg, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(precheck_run_id, check_name) DO UPDATE SET
                    status = excluded.status,
                    error_code = excluded.error_code,
                    error_msg = excluded.error_msg,
                    details_json = excluded.details_json
                """,
                (int(precheck_run_id), str(check_name), str(status), str(error_code or ""), str(error_msg or ""), _json(details or {})),
            )
            row = conn.execute(
                "SELECT id FROM arb_precheck_results WHERE precheck_run_id = ? AND check_name = ?",
                (int(precheck_run_id), str(check_name)),
            ).fetchone()
            return int(row["id"])

    def set_route_precheck_status(self, route_id: int, *, safety_status: str, route_status: str, blockers: list[str], warnings: list[str]) -> None:
        stamp = now_ms()
        with self.conn() as conn:
            conn.execute(
                """
                UPDATE arb_routes
                SET safety_status = ?, route_status = ?, blocker_reasons_json = ?,
                    warning_reasons_json = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (str(safety_status), str(route_status), _json(blockers), _json(warnings), stamp, int(route_id)),
            )
            route = conn.execute("SELECT opportunity_id FROM arb_routes WHERE id = ?", (int(route_id),)).fetchone()
            if not route:
                return
            _refresh_opportunity_route_state(conn, int(route["opportunity_id"]), stamp=stamp)

    def append_dead_letter(
        self,
        *,
        reason: str,
        payload: dict[str, Any],
        deadletter_key: str | None = None,
        error_code: str = "",
        retryable: bool = False,
    ) -> int:
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO arb_dead_letters(
                    deadletter_key, reason, status, attempts, retryable, error_code, payload_json, created_at_ms
                ) VALUES (?, ?, 'OPEN', 0, ?, ?, ?, ?)
                ON CONFLICT(deadletter_key) DO UPDATE SET
                    status = 'OPEN',
                    attempts = arb_dead_letters.attempts + 1,
                    retryable = excluded.retryable,
                    error_code = excluded.error_code,
                    payload_json = excluded.payload_json,
                    resolved_at_ms = NULL
                """,
                (
                    deadletter_key or f"{reason}:{uuid.uuid4().hex}",
                    str(reason),
                    1 if retryable else 0,
                    str(error_code or ""),
                    _json(_redact_sensitive_payload(payload)),
                    now_ms(),
                ),
            )
            row = conn.execute("SELECT id FROM arb_dead_letters WHERE deadletter_key = ?", (deadletter_key,)).fetchone()
            if row:
                return int(row["id"])
            return int(cur.lastrowid) if cur.lastrowid else 0

    def fetch_dead_letters(self) -> list[dict[str, Any]]:
        with self.conn() as conn:
            return [_row(r) for r in conn.execute("SELECT * FROM arb_dead_letters ORDER BY id").fetchall()]

    def append_event(
        self,
        *,
        event_type: str,
        opportunity_id: int | None = None,
        route_id: int | None = None,
        run_id: int | None = None,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_id = f"evt_{uuid.uuid4().hex}"
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO arb_event_log(
                    event_id, event_type, opportunity_id, route_id, run_id, severity, payload_json, occurred_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    str(event_type),
                    opportunity_id,
                    route_id,
                    run_id,
                    str(severity),
                    _json(_redact_sensitive_payload(payload or {})),
                    now_ms(),
                ),
            )
            row = conn.execute("SELECT * FROM arb_event_log WHERE seq = ?", (int(cur.lastrowid),)).fetchone()
            return _row(row)

    def latest_event_seq(self) -> int:
        with self.conn() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM arb_event_log").fetchone()
            return int(row["seq"] or 0)

    def fetch_event_log(
        self,
        *,
        after_seq: int = 0,
        limit: int = 200,
        opportunity_id: int | None = None,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["seq > ?"]
        params: list[Any] = [int(after_seq)]
        if opportunity_id is not None:
            clauses.append("(opportunity_id IS NULL OR opportunity_id = ?)")
            params.append(int(opportunity_id))
        if run_id is not None:
            clauses.append("(run_id IS NULL OR run_id = ?)")
            params.append(int(run_id))
        params.append(int(limit))
        with self.conn() as conn:
            return [
                _row(r)
                for r in conn.execute(
                    f"SELECT * FROM arb_event_log WHERE {' AND '.join(clauses)} ORDER BY seq DESC LIMIT ?",
                    params,
                ).fetchall()
            ]

    def fetch_event_log_replay(self, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self.conn() as conn:
            return [
                _row(r)
                for r in conn.execute(
                    "SELECT * FROM arb_event_log WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                    (int(after_seq), int(limit)),
                ).fetchall()
            ]

    def count_event_log_after(self, after_seq: int) -> int:
        with self.conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM arb_event_log WHERE seq > ?", (int(after_seq),)).fetchone()
            return int(row["n"] or 0)

    def fetch_alerts(
        self,
        *,
        opportunity_id: int | None = None,
        channel: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            params.append(int(opportunity_id))
        if channel is not None and str(channel).strip():
            clauses.append("channel = ?")
            params.append(str(channel).strip())
        if status is not None and str(status).strip():
            clauses.append("status = ?")
            params.append(str(status).strip().upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self.conn() as conn:
            return [
                _row(r)
                for r in conn.execute(
                    f"""
                    SELECT * FROM arb_alerts
                    {where}
                    ORDER BY created_at_ms DESC, id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            ]

    def fetch_opportunities(self) -> list[dict[str, Any]]:
        with self.conn() as conn:
            return [
                _row(r)
                for r in conn.execute(
                    """
                    SELECT o.*, a.symbol
                    FROM arb_opportunities o
                    JOIN arb_assets a ON a.id = o.asset_id
                    ORDER BY o.last_seen_at_ms DESC, o.id DESC
                    """
                ).fetchall()
            ]

    def fetch_routes_for_opportunity(self, opportunity_id: int) -> list[dict[str, Any]]:
        with self.conn() as conn:
            return [
                _row(r)
                for r in conn.execute(
                    "SELECT * FROM arb_routes WHERE opportunity_id = ? ORDER BY selected DESC, id",
                    (int(opportunity_id),),
                ).fetchall()
            ]

    def fetch_provider_health(self) -> list[dict[str, Any]]:
        with self.conn() as conn:
            return [_row(r) for r in conn.execute("SELECT * FROM arb_provider_health ORDER BY provider_key").fetchall()]

    def set_provider_health(
        self,
        *,
        provider_key: str,
        status: str,
        reason: str = "",
        capability: str = "",
        scope_key: str = "",
        latency_ms: float | None = None,
        cooldown_until_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        normalized_status = str(status or "").strip().upper()
        if normalized_status not in {"ACTIVE", "DEGRADED", "DISABLED"}:
            raise ValueError(f"unsupported_provider_health_status:{status}")
        stamp = now_ms()
        reason_code = str(reason or "").strip()
        payload_data = _redact_sensitive_payload({
            "reason": reason_code,
            "capability": str(capability or ""),
            "scope_key": str(scope_key or ""),
            **dict(payload or {}),
        })
        if normalized_status == "ACTIVE":
            with self.conn() as conn:
                conn.execute(
                    """
                    INSERT INTO arb_provider_health(
                        provider_key, status, last_success_at_ms, latency_ms, consecutive_failures,
                        cooldown_until_ms, error_code, payload_json
                    ) VALUES (?, 'ACTIVE', ?, ?, 0, NULL, '', ?)
                    ON CONFLICT(provider_key) DO UPDATE SET
                        status = 'ACTIVE',
                        last_success_at_ms = excluded.last_success_at_ms,
                        latency_ms = excluded.latency_ms,
                        consecutive_failures = 0,
                        cooldown_until_ms = NULL,
                        error_code = '',
                        payload_json = excluded.payload_json
                    """,
                    (str(provider_key), stamp, latency_ms, _json(payload_data)),
                )
        elif normalized_status == "DISABLED":
            with self.conn() as conn:
                conn.execute(
                    """
                    INSERT INTO arb_provider_health(
                        provider_key, status, last_error_at_ms, latency_ms, consecutive_failures,
                        cooldown_until_ms, error_code, payload_json
                    ) VALUES (?, 'DISABLED', ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(provider_key) DO UPDATE SET
                        status = 'DISABLED',
                        last_error_at_ms = excluded.last_error_at_ms,
                        latency_ms = excluded.latency_ms,
                        consecutive_failures = 0,
                        cooldown_until_ms = excluded.cooldown_until_ms,
                        error_code = excluded.error_code,
                        payload_json = excluded.payload_json
                    """,
                    (str(provider_key), stamp, latency_ms, cooldown_until_ms, reason_code, _json(payload_data)),
                )
        else:
            with self.conn() as conn:
                conn.execute(
                    """
                    INSERT INTO arb_provider_health(
                        provider_key, status, last_error_at_ms, latency_ms, consecutive_failures,
                        cooldown_until_ms, error_code, payload_json
                    ) VALUES (?, 'DEGRADED', ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(provider_key) DO UPDATE SET
                        status = 'DEGRADED',
                        last_error_at_ms = excluded.last_error_at_ms,
                        latency_ms = excluded.latency_ms,
                        consecutive_failures = arb_provider_health.consecutive_failures + 1,
                        cooldown_until_ms = excluded.cooldown_until_ms,
                        error_code = excluded.error_code,
                        payload_json = excluded.payload_json
                    """,
                    (str(provider_key), stamp, latency_ms, cooldown_until_ms, reason_code, _json(payload_data)),
                )
        self.append_event(
            event_type="provider.health",
            severity="warning" if normalized_status != "ACTIVE" else "info",
            payload={
                "provider_key": provider_key,
                "scope_key": scope_key,
                "capability": capability,
                "status": normalized_status,
                "reason": reason_code,
                "error_code": reason_code,
            },
        )

    def record_collect_success(
        self,
        *,
        provider_key: str,
        scope_key: str,
        cursor_value: str,
        collected_count: int,
        inserted_count: int,
        latency_ms: float | None = None,
    ) -> None:
        stamp = now_ms()
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_collect_state(provider_key, scope_key, cursor_value, updated_at_ms)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider_key, scope_key) DO UPDATE SET
                    cursor_value = excluded.cursor_value,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (str(provider_key), str(scope_key), str(cursor_value), stamp),
            )
            conn.execute(
                """
                INSERT INTO arb_provider_health(
                    provider_key, status, last_success_at_ms, latency_ms, consecutive_failures, error_code, payload_json
                ) VALUES (?, 'OK', ?, ?, 0, '', ?)
                ON CONFLICT(provider_key) DO UPDATE SET
                    status = 'OK',
                    last_success_at_ms = excluded.last_success_at_ms,
                    latency_ms = excluded.latency_ms,
                    consecutive_failures = 0,
                    error_code = '',
                    payload_json = excluded.payload_json
                """,
                (
                    str(provider_key),
                    stamp,
                    latency_ms,
                    _json({"scope_key": scope_key, "collected_count": int(collected_count), "inserted_count": int(inserted_count)}),
                ),
            )
        self.append_event(
            event_type="provider.health",
            payload={"provider_key": provider_key, "scope_key": scope_key, "status": "OK", "cursor": cursor_value},
        )

    def record_collect_failure(
        self,
        *,
        provider_key: str,
        scope_key: str,
        cursor_before: str,
        error_code: str,
        retryable: bool,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        stamp = now_ms()
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_provider_health(
                    provider_key, status, last_error_at_ms, consecutive_failures, error_code, payload_json
                ) VALUES (?, 'DEGRADED', ?, 1, ?, ?)
                ON CONFLICT(provider_key) DO UPDATE SET
                    status = 'DEGRADED',
                    last_error_at_ms = excluded.last_error_at_ms,
                    consecutive_failures = arb_provider_health.consecutive_failures + 1,
                    error_code = excluded.error_code,
                    payload_json = excluded.payload_json
                """,
                (
                    str(provider_key),
                    stamp,
                    str(error_code),
                    _json({"scope_key": scope_key, "cursor_before": cursor_before, "raw_payload": raw_payload or {}}),
                ),
            )
        self.append_dead_letter(
            reason="collect_failure",
            deadletter_key=f"collect_failure:{provider_key}:{scope_key}:{error_code}:{stamp}",
            error_code=error_code,
            retryable=retryable,
            payload={"provider_key": provider_key, "scope_key": scope_key, "cursor_before": cursor_before, "raw_payload": raw_payload or {}},
        )
        self.append_event(
            event_type="provider.health",
            severity="warning",
            payload={"provider_key": provider_key, "scope_key": scope_key, "status": "DEGRADED", "error_code": error_code},
        )

    def get_collect_cursor(self, provider_key: str, scope_key: str) -> str:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT cursor_value FROM arb_collect_state WHERE provider_key = ? AND scope_key = ?",
                (str(provider_key), str(scope_key)),
            ).fetchone()
            return str(row["cursor_value"]) if row else ""

    def insert_simulation_run(
        self,
        *,
        simulation_key: str,
        status: str,
        requested_by: str = "system",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stamp = now_ms()
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO arb_simulation_runs(
                    simulation_key, status, requested_by, started_at_ms, payload_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(simulation_key) DO NOTHING
                """,
                (
                    str(simulation_key),
                    str(status),
                    str(requested_by or "system"),
                    stamp,
                    _json(_redact_sensitive_payload(payload or {})),
                    stamp,
                ),
            )
            row = _row(conn.execute("SELECT * FROM arb_simulation_runs WHERE simulation_key = ?", (str(simulation_key),)).fetchone())
            if row is not None:
                row["created"] = cur.rowcount > 0
            return row

    def update_simulation_run(
        self,
        simulation_id: int,
        *,
        status: str,
        opportunity_id: int | None = None,
        route_id: int | None = None,
        execution_run_id: int | None = None,
        error_code: str = "",
        error_msg: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        completed = now_ms() if str(status) in {"COMPLETED", "FAILED", "BLOCKED", "NO_OPPORTUNITY"} else None
        with self.conn() as conn:
            conn.execute(
                """
                UPDATE arb_simulation_runs
                SET status = ?,
                    opportunity_id = COALESCE(?, opportunity_id),
                    route_id = COALESCE(?, route_id),
                    execution_run_id = COALESCE(?, execution_run_id),
                    completed_at_ms = COALESCE(?, completed_at_ms),
                    error_code = ?,
                    error_msg = ?,
                    payload_json = COALESCE(?, payload_json),
                    updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    str(status),
                    opportunity_id,
                    route_id,
                    execution_run_id,
                    completed,
                    str(error_code or ""),
                    str(error_msg or ""),
                    _json(_redact_sensitive_payload(payload)) if payload is not None else None,
                    now_ms(),
                    int(simulation_id),
                ),
            )
            return _row(conn.execute("SELECT * FROM arb_simulation_runs WHERE id = ?", (int(simulation_id),)).fetchone())

    def get_simulation_run(self, simulation_id: int) -> dict[str, Any] | None:
        with self.conn() as conn:
            return _row(conn.execute("SELECT * FROM arb_simulation_runs WHERE id = ?", (int(simulation_id),)).fetchone())

    def list_simulation_runs(
        self,
        *,
        opportunity_id: int | None = None,
        route_id: int | None = None,
        execution_run_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            params.append(int(opportunity_id))
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(int(route_id))
        if execution_run_id is not None:
            clauses.append("execution_run_id = ?")
            params.append(int(execution_run_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self.conn() as conn:
            return [
                _row(row)
                for row in conn.execute(
                    f"""
                    SELECT * FROM arb_simulation_runs
                    {where}
                    ORDER BY started_at_ms DESC, id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            ]

    def fetch_positions(self, *, opportunity_id: int | None = None, run_id: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            params.append(int(opportunity_id))
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(int(run_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.conn() as conn:
            return [_row(r) for r in conn.execute(f"SELECT * FROM arb_positions {where} ORDER BY id DESC", params).fetchall()]

    def upsert_position(
        self,
        *,
        position_key: str,
        opportunity_id: int,
        run_id: int,
        asset_id: int,
        status: str,
        qty_raw: str = "",
        avg_buy_price_krw: float | None = None,
        realized_pnl_krw: float | None = None,
        opened_at_ms: int | None = None,
        closed_at_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_positions(
                    position_key, opportunity_id, run_id, asset_id, status, qty_raw,
                    avg_buy_price_krw, realized_pnl_krw, opened_at_ms, closed_at_ms, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_key) DO UPDATE SET
                    opportunity_id = excluded.opportunity_id,
                    run_id = excluded.run_id,
                    asset_id = excluded.asset_id,
                    status = excluded.status,
                    qty_raw = excluded.qty_raw,
                    avg_buy_price_krw = excluded.avg_buy_price_krw,
                    realized_pnl_krw = excluded.realized_pnl_krw,
                    opened_at_ms = COALESCE(arb_positions.opened_at_ms, excluded.opened_at_ms),
                    closed_at_ms = excluded.closed_at_ms,
                    payload_json = excluded.payload_json
                """,
                (
                    str(position_key),
                    int(opportunity_id),
                    int(run_id),
                    int(asset_id),
                    str(status),
                    str(qty_raw or ""),
                    avg_buy_price_krw,
                    realized_pnl_krw,
                    opened_at_ms,
                    closed_at_ms,
                    _json(payload or {}),
                ),
            )
            row = conn.execute("SELECT * FROM arb_positions WHERE position_key = ?", (str(position_key),)).fetchone()
            return _row(row)

    def insert_position_mark(
        self,
        *,
        position_id: int,
        observed_at_ms: int,
        mark_price_krw: float | None = None,
        unrealized_pnl_krw: float | None = None,
        route_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO arb_position_marks(
                    position_id, observed_at_ms, mark_price_krw, unrealized_pnl_krw, route_status_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(position_id, observed_at_ms) DO UPDATE SET
                    mark_price_krw = excluded.mark_price_krw,
                    unrealized_pnl_krw = excluded.unrealized_pnl_krw,
                    route_status_json = excluded.route_status_json
                """,
                (
                    int(position_id),
                    int(observed_at_ms),
                    mark_price_krw,
                    unrealized_pnl_krw,
                    _json(route_status or {}),
                ),
            )
            row = conn.execute(
                "SELECT * FROM arb_position_marks WHERE position_id = ? AND observed_at_ms = ?",
                (int(position_id), int(observed_at_ms)),
            ).fetchone()
            return _row(row)

    def fetch_position_marks(self, position_id: int) -> list[dict[str, Any]]:
        with self.conn() as conn:
            return [
                _row(row)
                for row in conn.execute(
                    "SELECT * FROM arb_position_marks WHERE position_id = ? ORDER BY observed_at_ms",
                    (int(position_id),),
                ).fetchall()
            ]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL DEFAULT '',
            canonical_source TEXT NOT NULL DEFAULT 'manual',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            chain_id TEXT NOT NULL,
            chain_code TEXT NOT NULL,
            contract_address TEXT NOT NULL,
            decimals INTEGER NOT NULL DEFAULT 18,
            wrapped_kind TEXT NOT NULL DEFAULT '',
            bridge_group TEXT NOT NULL DEFAULT '',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            UNIQUE(chain_id, contract_address)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_tokens_asset ON arb_tokens(asset_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_venues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            venue_code TEXT NOT NULL UNIQUE,
            venue_type TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            private_trading_enabled INTEGER NOT NULL DEFAULT 0,
            withdrawal_enabled INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_key TEXT NOT NULL UNIQUE,
            asset_id INTEGER NOT NULL,
            venue_id INTEGER NOT NULL,
            market_type TEXT NOT NULL,
            chain_code TEXT NOT NULL DEFAULT '',
            pool_address TEXT NOT NULL DEFAULT '',
            market_symbol TEXT NOT NULL DEFAULT '',
            quote_asset TEXT NOT NULL DEFAULT '',
            deposit_network TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_markets_asset ON arb_markets(asset_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_key TEXT NOT NULL UNIQUE,
            chain_code TEXT NOT NULL,
            address TEXT NOT NULL,
            wallet_type TEXT NOT NULL,
            mode TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            withdrawal_enabled INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_strategy_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_code TEXT NOT NULL UNIQUE,
            min_edge_worst_bps REAL NOT NULL DEFAULT 100,
            max_trade_krw REAL NOT NULL DEFAULT 0,
            max_daily_loss_krw REAL NOT NULL DEFAULT 0,
            paper_enabled INTEGER NOT NULL DEFAULT 1,
            one_click_enabled INTEGER NOT NULL DEFAULT 0,
            auto_small_enabled INTEGER NOT NULL DEFAULT 0,
            live_full_enabled INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_kill_switches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            switch_code TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            raw_price REAL,
            price_usd REAL,
            price_krw REAL,
            best_bid REAL,
            best_ask REAL,
            liquidity_usd REAL,
            volume_24h REAL,
            stale INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            UNIQUE(market_id, source, observed_at_ms)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_market_ticks_lookup ON arb_market_ticks(market_id, observed_at_ms DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_pool_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            reserve0_raw TEXT NOT NULL DEFAULT '',
            reserve1_raw TEXT NOT NULL DEFAULT '',
            liquidity_usd REAL,
            block_number INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(market_id, source, observed_at_ms)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            best_bid REAL,
            best_ask REAL,
            depth_json TEXT NOT NULL DEFAULT '[]',
            stale INTEGER NOT NULL DEFAULT 0,
            UNIQUE(market_id, source, observed_at_ms)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_fx_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            source TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            rate REAL NOT NULL,
            stale INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(pair, source, observed_at_ms)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_route_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL,
            leg_type TEXT NOT NULL,
            source TEXT NOT NULL,
            destination TEXT NOT NULL,
            amount_in_raw TEXT NOT NULL DEFAULT '',
            amount_out_expected_raw TEXT NOT NULL DEFAULT '',
            amount_out_min_raw TEXT NOT NULL DEFAULT '',
            amount_in_value_krw REAL,
            amount_out_expected_krw REAL,
            amount_out_min_krw REAL,
            gas_krw REAL,
            fee_krw REAL,
            price_impact_bps REAL,
            eta_seconds INTEGER,
            observed_at_ms INTEGER NOT NULL,
            expires_at_ms INTEGER,
            stale INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_route_quotes_route_obs ON arb_route_quotes(route_id, observed_at_ms DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_key TEXT NOT NULL UNIQUE,
            asset_id INTEGER NOT NULL,
            anomaly_type TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL,
            safety_status TEXT NOT NULL,
            buy_market_id INTEGER NOT NULL,
            sell_market_id INTEGER NOT NULL,
            spread_bps REAL NOT NULL DEFAULT 0,
            edge_expected_bps REAL NOT NULL DEFAULT 0,
            edge_worst_bps REAL NOT NULL DEFAULT 0,
            first_seen_at_ms INTEGER NOT NULL,
            last_seen_at_ms INTEGER NOT NULL,
            selected_route_id INTEGER,
            source_signalhub_event_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_opportunities_status ON arb_opportunities(lifecycle_status, safety_status)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_key TEXT NOT NULL UNIQUE,
            opportunity_id INTEGER NOT NULL,
            route_type TEXT NOT NULL,
            buy_market_id INTEGER NOT NULL,
            sell_market_id INTEGER NOT NULL,
            safety_status TEXT NOT NULL,
            route_status TEXT NOT NULL,
            edge_expected_bps REAL NOT NULL DEFAULT 0,
            edge_worst_bps REAL NOT NULL DEFAULT 0,
            blocker_reasons_json TEXT NOT NULL DEFAULT '[]',
            warning_reasons_json TEXT NOT NULL DEFAULT '[]',
            selected INTEGER NOT NULL DEFAULT 0,
            quote_fresh_until_ms INTEGER,
            edge_worst_verified INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_routes_opportunity ON arb_routes(opportunity_id)")
    _ensure_column(conn, "arb_routes", "edge_worst_verified", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_route_freshness (
            route_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            fresh_until_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY(route_id, source_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_precheck_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT NOT NULL UNIQUE,
            opportunity_id INTEGER NOT NULL,
            route_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at_ms INTEGER NOT NULL,
            completed_at_ms INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_precheck_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            precheck_run_id INTEGER NOT NULL,
            check_name TEXT NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT NOT NULL DEFAULT '',
            error_msg TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            UNIQUE(precheck_run_id, check_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            chat_id TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            UNIQUE(opportunity_id, channel, chat_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_execution_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_key TEXT NOT NULL UNIQUE,
            idempotency_key TEXT NOT NULL UNIQUE,
            opportunity_id INTEGER NOT NULL,
            route_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_by TEXT NOT NULL DEFAULT 'system',
            started_at_ms INTEGER NOT NULL,
            completed_at_ms INTEGER,
            error_code TEXT NOT NULL DEFAULT '',
            error_msg TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_execution_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            step_key TEXT NOT NULL,
            attempt_no INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            worker_id TEXT NOT NULL DEFAULT '',
            lease_until_ms INTEGER,
            external_ref TEXT NOT NULL DEFAULT '',
            started_at_ms INTEGER,
            completed_at_ms INTEGER,
            duration_ms INTEGER,
            error_code TEXT NOT NULL DEFAULT '',
            error_msg TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            UNIQUE(run_id, step_key, attempt_no)
        )
    """)
    _ensure_column(conn, "arb_execution_steps", "duration_ms", "INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_key TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL,
            step_id INTEGER,
            venue_code TEXT NOT NULL,
            market_key TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            amount_raw TEXT NOT NULL DEFAULT '',
            amount_value_krw REAL,
            avg_price_krw REAL,
            status TEXT NOT NULL,
            external_order_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_orders_run_step ON arb_orders(run_id, step_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            run_id INTEGER,
            step_id INTEGER,
            tx_type TEXT NOT NULL DEFAULT '',
            nonce INTEGER,
            status TEXT NOT NULL,
            submitted_at_ms INTEGER,
            confirmed_at_ms INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(chain_id, tx_hash)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_transactions_run_step ON arb_transactions(run_id, step_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_key TEXT NOT NULL UNIQUE,
            run_id INTEGER,
            step_id INTEGER,
            from_location TEXT NOT NULL DEFAULT '',
            to_location TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            amount_raw TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
            updated_at_ms INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
        )
    """)
    _ensure_column(conn, "arb_transfers", "step_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_transfers_run_step ON arb_transfers(run_id, step_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_key TEXT NOT NULL UNIQUE,
            opportunity_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            qty_raw TEXT NOT NULL DEFAULT '',
            avg_buy_price_krw REAL,
            realized_pnl_krw REAL,
            opened_at_ms INTEGER,
            closed_at_ms INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_position_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            mark_price_krw REAL,
            unrealized_pnl_krw REAL,
            route_status_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(position_id, observed_at_ms)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_provider_health (
            provider_key TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'UNKNOWN',
            last_success_at_ms INTEGER,
            last_error_at_ms INTEGER,
            latency_ms REAL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            cooldown_until_ms INTEGER,
            error_code TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_collect_state (
            provider_key TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            cursor_value TEXT NOT NULL DEFAULT '',
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY(provider_key, scope_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_simulation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            simulation_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            requested_by TEXT NOT NULL DEFAULT 'system',
            opportunity_id INTEGER,
            route_id INTEGER,
            execution_run_id INTEGER,
            started_at_ms INTEGER NOT NULL,
            completed_at_ms INTEGER,
            error_code TEXT NOT NULL DEFAULT '',
            error_msg TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            updated_at_ms INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_event_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            opportunity_id INTEGER,
            route_id INTEGER,
            run_id INTEGER,
            severity TEXT NOT NULL DEFAULT 'info',
            payload_json TEXT NOT NULL DEFAULT '{}',
            occurred_at_ms INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_event_log_opportunity ON arb_event_log(opportunity_id, seq DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_dead_letters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deadletter_key TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            attempts INTEGER NOT NULL DEFAULT 0,
            retryable INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL,
            resolved_at_ms INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_operator_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_key TEXT NOT NULL UNIQUE,
            run_id INTEGER,
            opportunity_id INTEGER,
            route_id INTEGER,
            mode TEXT NOT NULL DEFAULT '',
            requested_by TEXT NOT NULL DEFAULT 'system',
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING',
            requested_at_ms INTEGER NOT NULL,
            decided_at_ms INTEGER,
            operator TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            decision_payload_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    _ensure_column(conn, "arb_operator_approvals", "mode", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "arb_operator_approvals", "requested_by", "TEXT NOT NULL DEFAULT 'system'")
    _ensure_column(conn, "arb_operator_approvals", "reason", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "arb_operator_approvals", "decision_payload_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "arb_operator_approvals", "consumed_run_id", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "arb_operator_approvals", "consumed_at_ms", "INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arb_operator_approvals_lookup ON arb_operator_approvals(opportunity_id, route_id, status)"
    )


def _seed_defaults(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO arb_strategy_profiles(profile_code, min_edge_worst_bps, max_trade_krw, max_daily_loss_krw)
        VALUES ('default', 100, 0, 0)
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO arb_kill_switches(switch_code, enabled, reason)
        VALUES ('global', 0, '')
        """
    )
