from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from arbitrage.store import ArbitrageStore


BUY_QUOTE = "buy_quote"
SELL_QUOTE_OR_ORDERBOOK = "sell_quote_or_orderbook"
GAS = "gas"
SWAP_FEE = "swap_fee"
BRIDGE_FEE = "bridge_fee"
SLIPPAGE = "slippage"
FX = "fx"
LATENCY_HAIRCUT = "latency_haircut"
RPC_FRESHNESS = "rpc_freshness"
DEPOSIT_OR_BRIDGE_STATUS = "deposit_or_bridge_status"

COMPONENT_NAMES: tuple[str, ...] = (
    BUY_QUOTE,
    SELL_QUOTE_OR_ORDERBOOK,
    GAS,
    SWAP_FEE,
    BRIDGE_FEE,
    SLIPPAGE,
    FX,
    LATENCY_HAIRCUT,
    RPC_FRESHNESS,
    DEPOSIT_OR_BRIDGE_STATUS,
)

DEFAULT_ROUTE_QUOTE_TTL_MS = 30_000
DEFAULT_MARKET_TICK_TTL_MS = 30_000
DEFAULT_ORDERBOOK_TTL_MS = 15_000
DEFAULT_FX_TTL_MS = 60_000
DEFAULT_LATENCY_TTL_MS = 30_000

SAME_DEX_REQUIRED_COMPONENTS: tuple[str, ...] = (
    BUY_QUOTE,
    SELL_QUOTE_OR_ORDERBOOK,
    GAS,
    SWAP_FEE,
    SLIPPAGE,
    LATENCY_HAIRCUT,
    RPC_FRESHNESS,
)

DIRECT_CEX_REQUIRED_COMPONENTS: tuple[str, ...] = (
    BUY_QUOTE,
    SELL_QUOTE_OR_ORDERBOOK,
    GAS,
    SWAP_FEE,
    SLIPPAGE,
    LATENCY_HAIRCUT,
    RPC_FRESHNESS,
    DEPOSIT_OR_BRIDGE_STATUS,
)

BRIDGE_DEX_REQUIRED_COMPONENTS: tuple[str, ...] = (
    BUY_QUOTE,
    SELL_QUOTE_OR_ORDERBOOK,
    GAS,
    SWAP_FEE,
    BRIDGE_FEE,
    SLIPPAGE,
    LATENCY_HAIRCUT,
    RPC_FRESHNESS,
    DEPOSIT_OR_BRIDGE_STATUS,
)

BRIDGE_CEX_REQUIRED_COMPONENTS: tuple[str, ...] = (
    BUY_QUOTE,
    SELL_QUOTE_OR_ORDERBOOK,
    GAS,
    SWAP_FEE,
    BRIDGE_FEE,
    SLIPPAGE,
    LATENCY_HAIRCUT,
    RPC_FRESHNESS,
    DEPOSIT_OR_BRIDGE_STATUS,
)


@dataclass(frozen=True, slots=True)
class EdgeComponentEvidence:
    name: str
    cost_bps: float = 0.0
    observed_at_ms: int | None = None
    fresh_until_ms: int | None = None
    stale: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _component_name(self.name))
        object.__setattr__(self, "cost_bps", float(self.cost_bps or 0.0))
        object.__setattr__(self, "observed_at_ms", _optional_int(self.observed_at_ms))
        object.__setattr__(self, "fresh_until_ms", _optional_int(self.fresh_until_ms))
        object.__setattr__(self, "stale", bool(self.stale))
        object.__setattr__(self, "details", dict(self.details or {}))

    @classmethod
    def from_mapping(cls, name: str, data: Mapping[str, Any]) -> "EdgeComponentEvidence":
        return cls(
            name=str(data.get("name") or name),
            cost_bps=float(data.get("cost_bps") or 0.0),
            observed_at_ms=_optional_int(data.get("observed_at_ms")),
            fresh_until_ms=_optional_int(data.get("fresh_until_ms")),
            stale=bool(data.get("stale") or False),
            details=_details_from_mapping(data),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cost_bps": self.cost_bps,
            "observed_at_ms": self.observed_at_ms,
            "fresh_until_ms": self.fresh_until_ms,
            "stale": self.stale,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class RouteEvaluationResult:
    route_id: int
    route_type: str
    edge_expected_bps: float
    edge_worst_bps: float
    edge_worst_verified: bool
    missing_components: list[str]
    warning_reasons: list[str]
    blocker_reasons: list[str]
    freshness: dict[str, dict[str, Any]]
    component_evidence: dict[str, dict[str, Any]]
    stale_components: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "route_type": self.route_type,
            "edge_expected_bps": self.edge_expected_bps,
            "edge_worst_bps": self.edge_worst_bps,
            "edge_worst_verified": self.edge_worst_verified,
            "missing_components": list(self.missing_components),
            "stale_components": list(self.stale_components),
            "warning_reasons": list(self.warning_reasons),
            "blocker_reasons": list(self.blocker_reasons),
            "freshness": {key: dict(value) for key, value in self.freshness.items()},
            "component_evidence": {
                key: dict(value) for key, value in self.component_evidence.items()
            },
        }


class RouteEvaluator:
    """No-network evaluator for worst-case edge component evidence."""

    def __init__(
        self,
        *,
        route_quote_ttl_ms: int = DEFAULT_ROUTE_QUOTE_TTL_MS,
        market_tick_ttl_ms: int = DEFAULT_MARKET_TICK_TTL_MS,
        orderbook_ttl_ms: int = DEFAULT_ORDERBOOK_TTL_MS,
        fx_ttl_ms: int = DEFAULT_FX_TTL_MS,
        latency_ttl_ms: int = DEFAULT_LATENCY_TTL_MS,
    ):
        self.route_quote_ttl_ms = int(route_quote_ttl_ms)
        self.market_tick_ttl_ms = int(market_tick_ttl_ms)
        self.orderbook_ttl_ms = int(orderbook_ttl_ms)
        self.fx_ttl_ms = int(fx_ttl_ms)
        self.latency_ttl_ms = int(latency_ttl_ms)

    def evaluate(
        self,
        *,
        route_id: int,
        route_type: str,
        edge_expected_bps: float,
        components: Mapping[str, EdgeComponentEvidence | Mapping[str, Any]],
        required_components: tuple[str, ...] | list[str] | None = None,
        quote_asset: str | None = None,
        as_of_ms: int | None = None,
    ) -> RouteEvaluationResult:
        stamp = _now_ms() if as_of_ms is None else int(as_of_ms)
        required = _dedupe_components(
            required_components
            if required_components is not None
            else required_components_for_route(route_type, quote_asset=quote_asset)
        )
        evidence = _coerce_components(components)

        missing_components: list[str] = []
        stale_components: list[str] = []
        unknown_freshness_components: list[str] = []
        freshness: dict[str, dict[str, Any]] = {}

        for name in required:
            component = evidence.get(name)
            freshness[name] = _freshness_record(name, component, stamp)
            status = freshness[name]["status"]
            if status == "missing":
                missing_components.append(name)
            elif status == "stale":
                stale_components.append(name)
            elif status == "unknown":
                unknown_freshness_components.append(name)

        for name in evidence:
            if name not in freshness:
                freshness[name] = _freshness_record(name, evidence[name], stamp)

        known_cost_bps = sum(
            max(0.0, component.cost_bps)
            for name, component in evidence.items()
            if name in required
        )
        edge_worst_bps = float(edge_expected_bps) - known_cost_bps
        blocker_reasons = [
            *(f"edge_component_missing:{name}" for name in missing_components),
            *(f"edge_component_stale:{name}" for name in stale_components),
            *(
                f"edge_component_freshness_unknown:{name}"
                for name in unknown_freshness_components
            ),
        ]
        edge_worst_verified = not blocker_reasons
        warning_reasons = [] if edge_worst_verified else ["edge_worst_unverified"]

        return RouteEvaluationResult(
            route_id=int(route_id),
            route_type=str(route_type),
            edge_expected_bps=float(edge_expected_bps),
            edge_worst_bps=edge_worst_bps,
            edge_worst_verified=edge_worst_verified,
            missing_components=missing_components,
            stale_components=stale_components,
            warning_reasons=warning_reasons,
            blocker_reasons=blocker_reasons,
            freshness=freshness,
            component_evidence={
                name: component.to_dict() for name, component in evidence.items()
            },
        )

    def evaluate_stored_route(
        self,
        store: ArbitrageStore,
        route_id: int,
        *,
        as_of_ms: int | None = None,
    ) -> RouteEvaluationResult:
        stamp = _now_ms() if as_of_ms is None else int(as_of_ms)
        context = _fetch_route_context(store, route_id)
        if context is None:
            raise ValueError(f"route_not_found:{route_id}")

        components = _stored_components_for_route(
            store=store,
            route=context,
            as_of_ms=stamp,
            route_quote_ttl_ms=self.route_quote_ttl_ms,
            market_tick_ttl_ms=self.market_tick_ttl_ms,
            orderbook_ttl_ms=self.orderbook_ttl_ms,
            fx_ttl_ms=self.fx_ttl_ms,
            latency_ttl_ms=self.latency_ttl_ms,
        )
        result = self.evaluate(
            route_id=int(route_id),
            route_type=str(context["route_type"]),
            edge_expected_bps=float(context.get("edge_expected_bps") or 0.0),
            quote_asset=str(context.get("sell_quote_asset") or ""),
            components=components,
            as_of_ms=stamp,
        )
        _persist_route_evaluation(store, context, result, evaluated_at_ms=stamp)
        return result


def evaluate_route_components(
    *,
    route_id: int,
    route_type: str,
    edge_expected_bps: float,
    components: Mapping[str, EdgeComponentEvidence | Mapping[str, Any]],
    required_components: tuple[str, ...] | list[str] | None = None,
    quote_asset: str | None = None,
    as_of_ms: int | None = None,
) -> RouteEvaluationResult:
    return RouteEvaluator().evaluate(
        route_id=route_id,
        route_type=route_type,
        edge_expected_bps=edge_expected_bps,
        components=components,
        required_components=required_components,
        quote_asset=quote_asset,
        as_of_ms=as_of_ms,
    )


def evaluate_stored_route(
    store: ArbitrageStore,
    route_id: int,
    *,
    as_of_ms: int | None = None,
) -> RouteEvaluationResult:
    return RouteEvaluator().evaluate_stored_route(
        store,
        route_id,
        as_of_ms=as_of_ms,
    )


def required_components_for_route(
    route_type: str,
    *,
    quote_asset: str | None = None,
) -> tuple[str, ...]:
    route_key = str(route_type).strip()
    if route_key == "same_dex_sell":
        required = SAME_DEX_REQUIRED_COMPONENTS
    elif route_key == "direct_cex_sell":
        required = DIRECT_CEX_REQUIRED_COMPONENTS
    elif route_key == "bridge_dex_sell":
        required = BRIDGE_DEX_REQUIRED_COMPONENTS
    elif route_key == "bridge_cex_sell":
        required = BRIDGE_CEX_REQUIRED_COMPONENTS
    else:
        raise ValueError(f"unsupported_route_type:{route_type}")

    if route_key in {"direct_cex_sell", "bridge_cex_sell"} and str(quote_asset or "").upper() == "KRW":
        return _dedupe_components((*required, FX))
    return required


def _coerce_components(
    components: Mapping[str, EdgeComponentEvidence | Mapping[str, Any]],
) -> dict[str, EdgeComponentEvidence]:
    evidence: dict[str, EdgeComponentEvidence] = {}
    for raw_name, raw_component in components.items():
        name = _component_name(str(raw_name))
        if isinstance(raw_component, EdgeComponentEvidence):
            component = raw_component
            if component.name != name:
                raise ValueError(f"component_name_mismatch:{name}:{component.name}")
        elif isinstance(raw_component, Mapping):
            component = EdgeComponentEvidence.from_mapping(name, raw_component)
        else:
            raise TypeError(f"unsupported_component_evidence:{name}")
        evidence[name] = component
    return evidence


def _freshness_record(
    name: str,
    component: EdgeComponentEvidence | None,
    as_of_ms: int,
) -> dict[str, Any]:
    if component is None:
        return {
            "component": name,
            "status": "missing",
            "observed_at_ms": None,
            "fresh_until_ms": None,
            "as_of_ms": as_of_ms,
        }
    if component.stale:
        status = "stale"
    elif component.fresh_until_ms is None:
        status = "unknown"
    elif component.fresh_until_ms <= as_of_ms:
        status = "stale"
    else:
        status = "fresh"
    return {
        "component": name,
        "status": status,
        "observed_at_ms": component.observed_at_ms,
        "fresh_until_ms": component.fresh_until_ms,
        "as_of_ms": as_of_ms,
    }


def _dedupe_components(raw_names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    names: list[str] = []
    for raw_name in raw_names:
        name = _component_name(raw_name)
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return tuple(names)


def _component_name(raw_name: str) -> str:
    name = str(raw_name).strip()
    if name not in COMPONENT_NAMES:
        raise ValueError(f"unsupported_edge_component:{raw_name}")
    return name


def _details_from_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("details"), Mapping):
        return dict(data["details"])
    return {
        key: value
        for key, value in data.items()
        if key not in {"name", "cost_bps", "observed_at_ms", "fresh_until_ms", "stale"}
    }


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stored_components_for_route(
    *,
    store: ArbitrageStore,
    route: Mapping[str, Any],
    as_of_ms: int,
    route_quote_ttl_ms: int,
    market_tick_ttl_ms: int,
    orderbook_ttl_ms: int,
    fx_ttl_ms: int,
    latency_ttl_ms: int,
) -> dict[str, EdgeComponentEvidence]:
    route_id = int(route["id"])
    route_type = str(route["route_type"])
    quotes = _fetch_route_quotes(store, route_id)
    freshness = _fetch_route_freshness_rows(store, route_id)
    payload = _loads_json(route.get("payload_json"), {})
    if payload.get("simulation_only"):
        simulation_quotes = [quote for quote in quotes if str(quote.get("source") or "") == "no_funds_simulation"]
        if simulation_quotes:
            quotes = simulation_quotes

    components: dict[str, EdgeComponentEvidence] = {}
    _set_component(
        components,
        _buy_quote_component(
            store=store,
            route=route,
            quotes=quotes,
            as_of_ms=as_of_ms,
            route_quote_ttl_ms=route_quote_ttl_ms,
            market_tick_ttl_ms=market_tick_ttl_ms,
        ),
    )
    _set_component(
        components,
        _sell_component(
            store=store,
            route=route,
            quotes=quotes,
            as_of_ms=as_of_ms,
            route_quote_ttl_ms=route_quote_ttl_ms,
            market_tick_ttl_ms=market_tick_ttl_ms,
            orderbook_ttl_ms=orderbook_ttl_ms,
        ),
    )
    notional_krw = _notional_krw(quotes=quotes, payload=payload)
    _set_component(
        components,
        _krw_cost_component(
            GAS,
            quotes=quotes,
            amount_fields=("gas_krw",),
            notional_krw=notional_krw,
            route_quote_ttl_ms=route_quote_ttl_ms,
            payload=payload,
            payload_bps_keys=("gas_bps", "gas_cost_bps"),
        ),
    )
    _set_component(
        components,
        _krw_cost_component(
            SWAP_FEE,
            quotes=[
                quote
                for quote in quotes
                if str(quote.get("leg_type") or "").lower() != "bridge"
            ],
            amount_fields=("fee_krw",),
            notional_krw=notional_krw,
            route_quote_ttl_ms=route_quote_ttl_ms,
            payload=payload,
            payload_bps_keys=("swap_fee_bps", "cex_fee_bps", "fee_bps"),
        ),
    )
    if route_type in {"bridge_dex_sell", "bridge_cex_sell"}:
        _set_component(
            components,
            _bridge_fee_component(
                quotes=quotes,
                notional_krw=notional_krw,
                route_quote_ttl_ms=route_quote_ttl_ms,
                payload=payload,
            ),
        )
    _set_component(
        components,
        _slippage_component(
            quotes=quotes,
            payload=payload,
            route_quote_ttl_ms=route_quote_ttl_ms,
        ),
    )
    if route_type in {"direct_cex_sell", "bridge_cex_sell"} and str(route.get("sell_quote_asset") or "").upper() == "KRW":
        _set_component(
            components,
            _fx_component(
                store=store,
                as_of_ms=as_of_ms,
                fx_ttl_ms=fx_ttl_ms,
            ),
        )
    _set_component(
        components,
        _latency_haircut_component(
            payload=payload,
            as_of_ms=as_of_ms,
            latency_ttl_ms=latency_ttl_ms,
        ),
    )
    _set_component(
        components,
        _freshness_component(
            RPC_FRESHNESS,
            freshness=freshness,
            source_keys=("rpc_freshness", "rpc_block"),
        ),
    )
    if route_type in {"direct_cex_sell", "bridge_dex_sell", "bridge_cex_sell"}:
        _set_component(
            components,
            _deposit_or_bridge_status_component(
                route=route,
                payload=payload,
                freshness=freshness,
                as_of_ms=as_of_ms,
                latency_ttl_ms=latency_ttl_ms,
            ),
        )
    return components


def _fetch_route_context(store: ArbitrageStore, route_id: int) -> dict[str, Any] | None:
    with store.conn() as conn:
        row = conn.execute(
            """
            SELECT
                r.*,
                bm.quote_asset AS buy_quote_asset,
                bm.chain_code AS buy_chain_code,
                bm.market_type AS buy_market_type,
                bv.venue_type AS buy_venue_type,
                sm.quote_asset AS sell_quote_asset,
                sm.chain_code AS sell_chain_code,
                sm.market_type AS sell_market_type,
                sm.deposit_network AS sell_deposit_network,
                sv.venue_type AS sell_venue_type
            FROM arb_routes r
            JOIN arb_markets bm ON bm.id = r.buy_market_id
            JOIN arb_venues bv ON bv.id = bm.venue_id
            JOIN arb_markets sm ON sm.id = r.sell_market_id
            JOIN arb_venues sv ON sv.id = sm.venue_id
            WHERE r.id = ?
            """,
            (int(route_id),),
        ).fetchone()
        return dict(row) if row else None


def _fetch_route_quotes(store: ArbitrageStore, route_id: int) -> list[dict[str, Any]]:
    with store.conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM arb_route_quotes
            WHERE route_id = ?
            ORDER BY observed_at_ms DESC, id DESC
            """,
            (int(route_id),),
        ).fetchall()
    return [_row_with_payload(row) for row in rows]


def _fetch_route_freshness_rows(
    store: ArbitrageStore,
    route_id: int,
) -> dict[str, dict[str, Any]]:
    with store.conn() as conn:
        rows = conn.execute(
            """
            SELECT source_key, fresh_until_ms, updated_at_ms
            FROM arb_route_freshness
            WHERE route_id = ?
            """,
            (int(route_id),),
        ).fetchall()
    return {str(row["source_key"]): dict(row) for row in rows}


def _fetch_latest_tick(
    store: ArbitrageStore,
    *,
    market_id: int,
    as_of_ms: int,
) -> dict[str, Any] | None:
    with store.conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM arb_market_ticks
            WHERE market_id = ?
              AND observed_at_ms <= ?
            ORDER BY observed_at_ms DESC, id DESC
            LIMIT 1
            """,
            (int(market_id), int(as_of_ms)),
        ).fetchone()
    return _row_with_payload(row) if row else None


def _fetch_latest_orderbook(
    store: ArbitrageStore,
    *,
    market_id: int,
    as_of_ms: int,
) -> dict[str, Any] | None:
    with store.conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM arb_orderbook_snapshots
            WHERE market_id = ?
              AND observed_at_ms <= ?
            ORDER BY observed_at_ms DESC, id DESC
            LIMIT 1
            """,
            (int(market_id), int(as_of_ms)),
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["depth"] = _loads_json(out.get("depth_json"), [])
    return out


def _fetch_latest_fx_rate(
    store: ArbitrageStore,
    *,
    as_of_ms: int,
) -> dict[str, Any] | None:
    with store.conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM arb_fx_rates
            WHERE observed_at_ms <= ?
            ORDER BY observed_at_ms DESC, id DESC
            LIMIT 50
            """,
            (int(as_of_ms),),
        ).fetchall()

    for row in rows:
        item = _row_with_payload(row)
        normalized_pair = _normalized_pair(item.get("pair"))
        rate = _optional_float(item.get("rate"))
        if rate is None or rate <= 0:
            continue
        if normalized_pair == "USDTKRW":
            item["effective_rate"] = rate
            item["conversion"] = "USDT/KRW"
            return item
        if normalized_pair == "KRWUSDT":
            item["effective_rate"] = 1.0 / rate
            item["conversion"] = "KRW/USDT_INVERTED"
            return item
    return None


def _buy_quote_component(
    *,
    store: ArbitrageStore,
    route: Mapping[str, Any],
    quotes: list[dict[str, Any]],
    as_of_ms: int,
    route_quote_ttl_ms: int,
    market_tick_ttl_ms: int,
) -> EdgeComponentEvidence | None:
    quote = _latest_quote(quotes, ("buy", "entry", "dex_buy", "buy_quote"))
    if quote:
        return _component_from_quote(
            BUY_QUOTE,
            quote,
            route_quote_ttl_ms=route_quote_ttl_ms,
            evidence_type="route_quote",
        )
    tick = _fetch_latest_tick(store, market_id=int(route["buy_market_id"]), as_of_ms=as_of_ms)
    if tick is None:
        return None
    return _component_from_tick(
        BUY_QUOTE,
        tick,
        ttl_ms=market_tick_ttl_ms,
        evidence_type="buy_tick",
    )


def _sell_component(
    *,
    store: ArbitrageStore,
    route: Mapping[str, Any],
    quotes: list[dict[str, Any]],
    as_of_ms: int,
    route_quote_ttl_ms: int,
    market_tick_ttl_ms: int,
    orderbook_ttl_ms: int,
) -> EdgeComponentEvidence | None:
    route_type = str(route["route_type"])
    if route_type in {"direct_cex_sell", "bridge_cex_sell"}:
        orderbook = _fetch_latest_orderbook(
            store,
            market_id=int(route["sell_market_id"]),
            as_of_ms=as_of_ms,
        )
        if orderbook is not None:
            return _component_from_orderbook(orderbook, ttl_ms=orderbook_ttl_ms)
        tick = _fetch_latest_tick(
            store,
            market_id=int(route["sell_market_id"]),
            as_of_ms=as_of_ms,
        )
        if tick is not None and (_optional_float(tick.get("best_bid")) or _optional_float(tick.get("price_krw")) or _optional_float(tick.get("price_usd"))):
            return _component_from_tick(
                SELL_QUOTE_OR_ORDERBOOK,
                tick,
                ttl_ms=market_tick_ttl_ms,
                evidence_type="sell_tick",
            )
        return None

    quote = _latest_quote(
        quotes,
        ("sell", "exit", "same_dex_sell", "bridge_dex_sell", "dex_sell", "sell_quote"),
    )
    if quote is None:
        return None
    return _component_from_quote(
        SELL_QUOTE_OR_ORDERBOOK,
        quote,
        route_quote_ttl_ms=route_quote_ttl_ms,
        evidence_type="sell_quote",
    )


def _krw_cost_component(
    name: str,
    *,
    quotes: list[dict[str, Any]],
    amount_fields: tuple[str, ...],
    notional_krw: float | None,
    route_quote_ttl_ms: int,
    payload: Mapping[str, Any],
    payload_bps_keys: tuple[str, ...],
) -> EdgeComponentEvidence | None:
    matching: list[dict[str, Any]] = []
    amount_krw = 0.0
    for quote in quotes:
        values = [_optional_float(quote.get(field)) for field in amount_fields]
        present_values = [value for value in values if value is not None]
        if not present_values:
            continue
        matching.append(quote)
        amount_krw += sum(max(0.0, value) for value in present_values)

    if matching and notional_krw and notional_krw > 0:
        fresh_until = _min_known(
            _quote_fresh_until_ms(quote, route_quote_ttl_ms=route_quote_ttl_ms)
            for quote in matching
        )
        return EdgeComponentEvidence(
            name=name,
            cost_bps=(amount_krw / notional_krw) * 10_000.0,
            observed_at_ms=_max_known(_optional_int(quote.get("observed_at_ms")) for quote in matching),
            fresh_until_ms=fresh_until,
            stale=any(_quote_is_stale(quote, route_quote_ttl_ms=route_quote_ttl_ms) for quote in matching),
            details={
                "source": "arb_route_quotes",
                "quote_ids": [int(quote["id"]) for quote in matching],
                "amount_fields": list(amount_fields),
                "amount_krw": amount_krw,
                "notional_krw": notional_krw,
            },
        )

    payload_bps = _first_optional_float(payload, payload_bps_keys)
    if payload_bps is None:
        return None
    return EdgeComponentEvidence(
        name=name,
        cost_bps=max(0.0, payload_bps),
        observed_at_ms=_optional_int(payload.get("evaluated_at_ms")),
        fresh_until_ms=_optional_int(payload.get(f"{name}_fresh_until_ms")),
        details={
            "source": "route_payload",
            "payload_keys": list(payload_bps_keys),
        },
    )


def _bridge_fee_component(
    *,
    quotes: list[dict[str, Any]],
    notional_krw: float | None,
    route_quote_ttl_ms: int,
    payload: Mapping[str, Any],
) -> EdgeComponentEvidence | None:
    bridge_quotes = [
        quote
        for quote in quotes
        if str(quote.get("leg_type") or "").lower() == "bridge"
    ]
    component = _krw_cost_component(
        BRIDGE_FEE,
        quotes=bridge_quotes,
        amount_fields=("fee_krw",),
        notional_krw=notional_krw,
        route_quote_ttl_ms=route_quote_ttl_ms,
        payload=payload,
        payload_bps_keys=("bridge_fee_bps",),
    )
    if component is None:
        return None

    eta_values = [
        eta
        for eta in (_optional_int(quote.get("eta_seconds")) for quote in bridge_quotes)
        if eta is not None and eta > 0
    ]
    payload_eta = _first_optional_int(payload, ("bridge_eta_seconds", "eta_seconds"))
    if payload_eta is not None and payload_eta > 0:
        eta_values.append(payload_eta)
    if not eta_values:
        return None

    details = dict(component.details)
    details["eta_seconds"] = max(eta_values)
    details["eta_seconds_values"] = eta_values
    return EdgeComponentEvidence(
        name=component.name,
        cost_bps=component.cost_bps,
        observed_at_ms=component.observed_at_ms,
        fresh_until_ms=component.fresh_until_ms,
        stale=component.stale,
        details=details,
    )


def _slippage_component(
    *,
    quotes: list[dict[str, Any]],
    payload: Mapping[str, Any],
    route_quote_ttl_ms: int,
) -> EdgeComponentEvidence | None:
    matching: list[dict[str, Any]] = []
    cost_bps = 0.0
    for quote in quotes:
        impact_bps = _optional_float(quote.get("price_impact_bps"))
        if impact_bps is None:
            expected = _optional_float(quote.get("amount_out_expected_krw"))
            minimum = _optional_float(quote.get("amount_out_min_krw"))
            if expected is not None and minimum is not None and expected > 0:
                impact_bps = max(0.0, ((expected - minimum) / expected) * 10_000.0)
        if impact_bps is None:
            continue
        matching.append(quote)
        cost_bps += max(0.0, impact_bps)

    if matching:
        fresh_until = _min_known(
            _quote_fresh_until_ms(quote, route_quote_ttl_ms=route_quote_ttl_ms)
            for quote in matching
        )
        return EdgeComponentEvidence(
            name=SLIPPAGE,
            cost_bps=cost_bps,
            observed_at_ms=_max_known(_optional_int(quote.get("observed_at_ms")) for quote in matching),
            fresh_until_ms=fresh_until,
            stale=any(_quote_is_stale(quote, route_quote_ttl_ms=route_quote_ttl_ms) for quote in matching),
            details={
                "source": "arb_route_quotes",
                "quote_ids": [int(quote["id"]) for quote in matching],
            },
        )

    payload_bps = _first_optional_float(payload, ("slippage_bps", "depth_haircut_bps"))
    if payload_bps is None:
        return None
    return EdgeComponentEvidence(
        name=SLIPPAGE,
        cost_bps=max(0.0, payload_bps),
        observed_at_ms=_optional_int(payload.get("evaluated_at_ms")),
        fresh_until_ms=_optional_int(payload.get("slippage_fresh_until_ms")),
        details={"source": "route_payload"},
    )


def _fx_component(
    *,
    store: ArbitrageStore,
    as_of_ms: int,
    fx_ttl_ms: int,
) -> EdgeComponentEvidence | None:
    fx = _fetch_latest_fx_rate(store, as_of_ms=as_of_ms)
    if fx is None:
        return None
    observed_at_ms = _optional_int(fx.get("observed_at_ms"))
    fresh_until_ms = observed_at_ms + int(fx_ttl_ms) if observed_at_ms is not None else None
    return EdgeComponentEvidence(
        name=FX,
        observed_at_ms=observed_at_ms,
        fresh_until_ms=fresh_until_ms,
        stale=bool(fx.get("stale") or False),
        details={
            "source": "arb_fx_rates",
            "fx_rate_id": int(fx["id"]),
            "pair": str(fx.get("pair") or ""),
            "rate": _optional_float(fx.get("rate")),
            "effective_rate": _optional_float(fx.get("effective_rate")),
            "conversion": str(fx.get("conversion") or ""),
        },
    )


def _latency_haircut_component(
    *,
    payload: Mapping[str, Any],
    as_of_ms: int,
    latency_ttl_ms: int,
) -> EdgeComponentEvidence | None:
    cost_bps = _first_optional_float(
        payload,
        ("latency_haircut_bps", "latency_buffer_bps", "latency_bps"),
    )
    if cost_bps is None:
        return None
    observed_at_ms = _optional_int(payload.get("latency_observed_at_ms")) or as_of_ms
    fresh_until_ms = (
        _optional_int(payload.get("latency_fresh_until_ms"))
        or observed_at_ms + int(latency_ttl_ms)
    )
    return EdgeComponentEvidence(
        name=LATENCY_HAIRCUT,
        cost_bps=max(0.0, cost_bps),
        observed_at_ms=observed_at_ms,
        fresh_until_ms=fresh_until_ms,
        details={"source": "route_payload"},
    )


def _freshness_component(
    name: str,
    *,
    freshness: Mapping[str, Mapping[str, Any]],
    source_keys: tuple[str, ...],
) -> EdgeComponentEvidence | None:
    for source_key in source_keys:
        row = freshness.get(source_key)
        if not row:
            continue
        return EdgeComponentEvidence(
            name=name,
            observed_at_ms=_optional_int(row.get("updated_at_ms")),
            fresh_until_ms=_optional_int(row.get("fresh_until_ms")),
            details={
                "source": "arb_route_freshness",
                "source_key": source_key,
            },
        )
    return None


def _deposit_or_bridge_status_component(
    *,
    route: Mapping[str, Any],
    payload: Mapping[str, Any],
    freshness: Mapping[str, Mapping[str, Any]],
    as_of_ms: int,
    latency_ttl_ms: int,
) -> EdgeComponentEvidence | None:
    route_type = str(route.get("route_type") or "")
    if route_type == "bridge_cex_sell":
        source_groups = (
            ("bridge_status", "bridge_availability"),
            ("deposit_status", "cex_deposit"),
        )
    elif route_type == "bridge_dex_sell":
        source_groups = (("bridge_status", "bridge_availability", "deposit_or_bridge_status"),)
    else:
        source_groups = (("deposit_status", "cex_deposit", "deposit_or_bridge_status"),)

    component = _freshness_component_from_groups(
        DEPOSIT_OR_BRIDGE_STATUS,
        freshness=freshness,
        source_groups=source_groups,
    )
    if component is not None:
        details = dict(component.details)
        details["deposit_network"] = str(route.get("sell_deposit_network") or "")
        return EdgeComponentEvidence(
            name=component.name,
            cost_bps=component.cost_bps,
            observed_at_ms=component.observed_at_ms,
            fresh_until_ms=component.fresh_until_ms,
            stale=component.stale,
            details=details,
        )

    payload_component = _payload_status_component(
        route_type=route_type,
        payload=payload,
        as_of_ms=as_of_ms,
        latency_ttl_ms=latency_ttl_ms,
    )
    if payload_component is None:
        return None

    details = dict(payload_component.details)
    details["deposit_network"] = str(route.get("sell_deposit_network") or "")
    return EdgeComponentEvidence(
        name=payload_component.name,
        observed_at_ms=payload_component.observed_at_ms,
        fresh_until_ms=payload_component.fresh_until_ms,
        details=details,
    )


def _freshness_component_from_groups(
    name: str,
    *,
    freshness: Mapping[str, Mapping[str, Any]],
    source_groups: tuple[tuple[str, ...], ...],
) -> EdgeComponentEvidence | None:
    components: list[EdgeComponentEvidence] = []
    for source_group in source_groups:
        component = _freshness_component(
            name,
            freshness=freshness,
            source_keys=source_group,
        )
        if component is None:
            return None
        components.append(component)

    if not components:
        return None
    source_fresh_until_ms: dict[str, int] = {}
    source_keys: list[str] = []
    for component in components:
        source_key = str(component.details.get("source_key") or "")
        if not source_key:
            continue
        source_keys.append(source_key)
        fresh_until_ms = _optional_int(component.fresh_until_ms)
        if fresh_until_ms is not None:
            source_fresh_until_ms[source_key] = fresh_until_ms
    return EdgeComponentEvidence(
        name=name,
        observed_at_ms=_max_known(component.observed_at_ms for component in components),
        fresh_until_ms=_min_known(component.fresh_until_ms for component in components),
        stale=any(component.stale for component in components),
        details={
            "source": "arb_route_freshness",
            "source_keys": source_keys,
            "source_fresh_until_ms": source_fresh_until_ms,
        },
    )


def _payload_status_component(
    *,
    route_type: str,
    payload: Mapping[str, Any],
    as_of_ms: int,
    latency_ttl_ms: int,
) -> EdgeComponentEvidence | None:
    deposit_status = _payload_ok_status(payload, ("deposit_status", "cex_deposit_status"))
    bridge_status = _payload_ok_status(payload, ("bridge_status",))
    if route_type == "bridge_cex_sell":
        if deposit_status is None or bridge_status is None:
            return None
        status_details = {"deposit_status": deposit_status, "bridge_status": bridge_status}
    elif route_type == "bridge_dex_sell":
        if bridge_status is None:
            return None
        status_details = {"bridge_status": bridge_status}
    else:
        if deposit_status is None:
            return None
        status_details = {"deposit_status": deposit_status}

    observed_at_ms = _optional_int(payload.get("status_observed_at_ms")) or as_of_ms
    return EdgeComponentEvidence(
        name=DEPOSIT_OR_BRIDGE_STATUS,
        observed_at_ms=observed_at_ms,
        fresh_until_ms=_optional_int(payload.get("status_fresh_until_ms"))
        or observed_at_ms + int(latency_ttl_ms),
        details={"source": "route_payload", **status_details},
    )


def _payload_ok_status(
    payload: Mapping[str, Any],
    keys: tuple[str, ...],
) -> str | None:
    ok_statuses = {"OK", "OPEN", "PASS", "ENABLED", "AVAILABLE"}
    for key in keys:
        status = str(payload.get(key) or "").strip().upper()
        if status in ok_statuses:
            return status
    return None


def _component_from_quote(
    name: str,
    quote: Mapping[str, Any],
    *,
    route_quote_ttl_ms: int,
    evidence_type: str,
) -> EdgeComponentEvidence:
    return EdgeComponentEvidence(
        name=name,
        observed_at_ms=_optional_int(quote.get("observed_at_ms")),
        fresh_until_ms=_quote_fresh_until_ms(quote, route_quote_ttl_ms=route_quote_ttl_ms),
        stale=_quote_is_stale(quote, route_quote_ttl_ms=route_quote_ttl_ms),
        details={
            "source": "arb_route_quotes",
            "quote_id": int(quote["id"]),
            "leg_type": str(quote.get("leg_type") or ""),
            "source_name": str(quote.get("source") or ""),
            "evidence_type": evidence_type,
        },
    )


def _component_from_tick(
    name: str,
    tick: Mapping[str, Any],
    *,
    ttl_ms: int,
    evidence_type: str,
) -> EdgeComponentEvidence:
    observed_at_ms = _optional_int(tick.get("observed_at_ms"))
    return EdgeComponentEvidence(
        name=name,
        observed_at_ms=observed_at_ms,
        fresh_until_ms=observed_at_ms + int(ttl_ms) if observed_at_ms is not None else None,
        stale=bool(tick.get("stale") or False),
        details={
            "source": "arb_market_ticks",
            "tick_id": int(tick["id"]),
            "source_name": str(tick.get("source") or ""),
            "evidence_type": evidence_type,
        },
    )


def _component_from_orderbook(
    orderbook: Mapping[str, Any],
    *,
    ttl_ms: int,
) -> EdgeComponentEvidence:
    observed_at_ms = _optional_int(orderbook.get("observed_at_ms"))
    return EdgeComponentEvidence(
        name=SELL_QUOTE_OR_ORDERBOOK,
        observed_at_ms=observed_at_ms,
        fresh_until_ms=observed_at_ms + int(ttl_ms) if observed_at_ms is not None else None,
        stale=bool(orderbook.get("stale") or False),
        details={
            "source": "arb_orderbook_snapshots",
            "orderbook_id": int(orderbook["id"]),
            "source_name": str(orderbook.get("source") or ""),
            "evidence_type": "orderbook",
            "best_bid": _optional_float(orderbook.get("best_bid")),
            "best_ask": _optional_float(orderbook.get("best_ask")),
        },
    )


def _persist_route_evaluation(
    store: ArbitrageStore,
    route: Mapping[str, Any],
    result: RouteEvaluationResult,
    *,
    evaluated_at_ms: int,
) -> None:
    payload = _loads_json(route.get("payload_json"), {})
    payload.update(
        {
            "edge_worst_bps": result.edge_worst_bps,
            "edge_worst_verified": result.edge_worst_verified,
            "edge_evaluation": result.to_dict(),
            "route_evaluator": {
                "version": "part4_store_v1",
                "evaluated_at_ms": int(evaluated_at_ms),
            },
        }
    )
    if result.edge_worst_verified:
        payload["candidate_only"] = False

    quote_fresh_until_ms = _result_quote_fresh_until_ms(result)
    if result.edge_worst_verified:
        safety_status = str(route.get("safety_status") or "WARN")
        route_status = "OPEN" if safety_status == "PASS" else "WARN"
    elif result.stale_components:
        safety_status = "BLOCK"
        route_status = "BLOCKED"
    else:
        safety_status = str(route.get("safety_status") or "WARN")
        if safety_status == "PASS":
            safety_status = "WARN"
        route_status = "WARN"

    with store.conn() as conn:
        conn.execute(
            """
            UPDATE arb_routes
            SET edge_expected_bps = ?,
                edge_worst_bps = ?,
                edge_worst_verified = ?,
                quote_fresh_until_ms = ?,
                route_status = ?,
                safety_status = ?,
                blocker_reasons_json = ?,
                warning_reasons_json = ?,
                payload_json = ?,
                updated_at_ms = ?
            WHERE id = ?
            """,
            (
                result.edge_expected_bps,
                result.edge_worst_bps,
                1 if result.edge_worst_verified else 0,
                quote_fresh_until_ms,
                route_status,
                safety_status,
                json.dumps(result.blocker_reasons, ensure_ascii=False, sort_keys=True),
                json.dumps(result.warning_reasons, ensure_ascii=False, sort_keys=True),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                int(evaluated_at_ms),
                int(result.route_id),
            ),
        )

    freshness = _route_freshness_updates(result)
    if freshness:
        store.set_route_freshness(result.route_id, freshness)


def _route_freshness_updates(result: RouteEvaluationResult) -> dict[str, int]:
    updates: dict[str, int] = {}
    for component_name, record in result.freshness.items():
        fresh_until_ms = _optional_int(record.get("fresh_until_ms"))
        if fresh_until_ms is None:
            continue
        updates[component_name] = fresh_until_ms
        evidence = result.component_evidence.get(component_name, {})
        details = evidence.get("details") if isinstance(evidence.get("details"), Mapping) else {}
        if component_name == SELL_QUOTE_OR_ORDERBOOK:
            evidence_type = str((details or {}).get("evidence_type") or "")
            updates["orderbook" if evidence_type == "orderbook" else "sell_quote"] = fresh_until_ms
        elif component_name == RPC_FRESHNESS:
            updates["rpc_block"] = fresh_until_ms
        elif component_name == FX:
            updates["fx"] = fresh_until_ms
        elif component_name == BUY_QUOTE:
            updates["buy_quote"] = fresh_until_ms
        elif component_name == DEPOSIT_OR_BRIDGE_STATUS:
            source_fresh_until_ms = (
                details.get("source_fresh_until_ms")
                if isinstance(details.get("source_fresh_until_ms"), Mapping)
                else {}
            )
            for source_key, source_fresh_until in source_fresh_until_ms.items():
                parsed_fresh_until = _optional_int(source_fresh_until)
                if parsed_fresh_until is not None:
                    updates[str(source_key)] = parsed_fresh_until
            source_key = str((details or {}).get("source_key") or "")
            if source_key:
                updates[source_key] = fresh_until_ms
        elif component_name == BRIDGE_FEE:
            updates["bridge_quote"] = fresh_until_ms
    return updates


def _result_quote_fresh_until_ms(result: RouteEvaluationResult) -> int | None:
    fresh_until_values = [
        _optional_int(record.get("fresh_until_ms"))
        for record in result.freshness.values()
        if record.get("status") != "missing"
    ]
    return _min_known(value for value in fresh_until_values)


def _latest_quote(
    quotes: list[dict[str, Any]],
    leg_types: tuple[str, ...],
) -> dict[str, Any] | None:
    normalized = {leg_type.lower() for leg_type in leg_types}
    for quote in quotes:
        if str(quote.get("leg_type") or "").lower() in normalized:
            return quote
    return None


def _quote_fresh_until_ms(
    quote: Mapping[str, Any],
    *,
    route_quote_ttl_ms: int,
) -> int | None:
    explicit = _optional_int(quote.get("expires_at_ms"))
    if explicit is not None:
        return explicit
    observed_at_ms = _optional_int(quote.get("observed_at_ms"))
    return observed_at_ms + int(route_quote_ttl_ms) if observed_at_ms is not None else None


def _quote_is_stale(
    quote: Mapping[str, Any],
    *,
    route_quote_ttl_ms: int,
) -> bool:
    return bool(quote.get("stale") or False) or _quote_fresh_until_ms(
        quote,
        route_quote_ttl_ms=route_quote_ttl_ms,
    ) is None


def _notional_krw(
    *,
    quotes: list[dict[str, Any]],
    payload: Mapping[str, Any],
) -> float | None:
    payload_notional = _first_optional_float(
        payload,
        ("notional_krw", "trade_amount_krw", "position_value_krw"),
    )
    if payload_notional is not None and payload_notional > 0:
        return payload_notional
    for quote in quotes:
        for key in ("amount_in_value_krw", "amount_out_expected_krw", "amount_out_min_krw"):
            value = _optional_float(quote.get(key))
            if value is not None and value > 0:
                return value
    return None


def _row_with_payload(row: Any) -> dict[str, Any]:
    out = dict(row)
    if "payload_json" in out:
        out["payload"] = _loads_json(out.get("payload_json"), {})
    return out


def _loads_json(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


def _set_component(
    components: dict[str, EdgeComponentEvidence],
    component: EdgeComponentEvidence | None,
) -> None:
    if component is not None:
        components[component.name] = component


def _optional_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    return float(value)


def _first_optional_float(
    data: Mapping[str, Any],
    keys: tuple[str, ...],
) -> float | None:
    for key in keys:
        value = _optional_float(data.get(key))
        if value is not None:
            return value
    return None


def _first_optional_int(
    data: Mapping[str, Any],
    keys: tuple[str, ...],
) -> int | None:
    for key in keys:
        value = _optional_int(data.get(key))
        if value is not None:
            return value
    return None


def _min_known(values: Any) -> int | None:
    known = [int(value) for value in values if value is not None]
    return min(known) if known else None


def _max_known(values: Any) -> int | None:
    known = [int(value) for value in values if value is not None]
    return max(known) if known else None


def _normalized_pair(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())
