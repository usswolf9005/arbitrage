from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arbitrage.normalizer import IdentityNormalizer, NormalizedIdentity
from arbitrage.store import ArbitrageStore, now_ms as store_now_ms


DEFAULT_DRAWDOWN_THRESHOLD_BPS = 500.0
DEFAULT_SPREAD_THRESHOLD_BPS = 0.0
DEFAULT_DEPEG_THRESHOLD_BPS = 100.0
DEFAULT_PRICE_SPIKE_THRESHOLD_BPS = 500.0
DEFAULT_LIQUIDITY_COLLAPSE_THRESHOLD_BPS = 3_000.0
DEFAULT_POOL_DIVERGENCE_THRESHOLD_BPS = 100.0
DEFAULT_DEX_TICK_TTL_MS = 30_000
DEFAULT_CEX_ORDERBOOK_TTL_MS = 15_000
DEFAULT_KRW_ORDERBOOK_TTL_MS = 15_000
DEFAULT_FX_TTL_MS = 60_000
DEFAULT_RPC_FRESHNESS_TTL_MS = 30_000
DEFAULT_POOL_SNAPSHOT_TTL_MS = 30_000
DEFAULT_PEG_REFERENCE_PRICE_USD = 1.0
USD_QUOTE_ASSETS = ("USD", "USDT", "USDC")
KRW_QUOTE_ASSETS = ("KRW",)
DEFAULT_STABLE_PEG_SYMBOLS = ("USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDD", "PYUSD")


@dataclass(frozen=True, slots=True)
class DetectorTTLConfig:
    dex_tick_ttl_ms: int = DEFAULT_DEX_TICK_TTL_MS
    cex_orderbook_ttl_ms: int = DEFAULT_CEX_ORDERBOOK_TTL_MS
    krw_orderbook_ttl_ms: int = DEFAULT_KRW_ORDERBOOK_TTL_MS
    fx_ttl_ms: int = DEFAULT_FX_TTL_MS
    rpc_freshness_ttl_ms: int = DEFAULT_RPC_FRESHNESS_TTL_MS
    pool_snapshot_ttl_ms: int = DEFAULT_POOL_SNAPSHOT_TTL_MS

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "DetectorTTLConfig":
        values = dict(data or {})
        return cls(
            dex_tick_ttl_ms=_positive_int(values.get("dex_tick_ttl_ms"), DEFAULT_DEX_TICK_TTL_MS),
            cex_orderbook_ttl_ms=_positive_int(
                values.get("cex_orderbook_ttl_ms"),
                DEFAULT_CEX_ORDERBOOK_TTL_MS,
            ),
            krw_orderbook_ttl_ms=_positive_int(
                values.get("krw_orderbook_ttl_ms"),
                DEFAULT_KRW_ORDERBOOK_TTL_MS,
            ),
            fx_ttl_ms=_positive_int(values.get("fx_ttl_ms"), DEFAULT_FX_TTL_MS),
            rpc_freshness_ttl_ms=_positive_int(
                values.get("rpc_freshness_ttl_ms"),
                DEFAULT_RPC_FRESHNESS_TTL_MS,
            ),
            pool_snapshot_ttl_ms=_positive_int(
                values.get("pool_snapshot_ttl_ms"),
                DEFAULT_POOL_SNAPSHOT_TTL_MS,
            ),
        )


@dataclass(frozen=True, slots=True)
class DetectorRunResult:
    opportunities_upserted: int = 0
    routes_upserted: int = 0
    blocked_identities: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "opportunities_upserted": self.opportunities_upserted,
            "routes_upserted": self.routes_upserted,
            "blocked_identities": self.blocked_identities,
            "skipped": self.skipped,
        }


@dataclass(frozen=True, slots=True)
class DexDrawdownCandidate:
    market: Mapping[str, Any]
    current_tick: Mapping[str, Any]
    baseline_tick: Mapping[str, Any]
    pool_snapshot: Mapping[str, Any] | None
    current_price: float
    baseline_price: float
    drawdown_bps: float


@dataclass(frozen=True, slots=True)
class DexPriceSpikeCandidate:
    market: Mapping[str, Any]
    current_tick: Mapping[str, Any]
    baseline_tick: Mapping[str, Any]
    pool_snapshot: Mapping[str, Any] | None
    current_price: float
    baseline_price: float
    spike_bps: float


@dataclass(frozen=True, slots=True)
class DepegCandidate:
    market: Mapping[str, Any]
    current_tick: Mapping[str, Any]
    pool_snapshot: Mapping[str, Any] | None
    identity: NormalizedIdentity
    price: float
    reference_price: float
    deviation_bps: float
    direction: str


@dataclass(frozen=True, slots=True)
class LiquidityCollapseCandidate:
    market: Mapping[str, Any]
    current_tick: Mapping[str, Any]
    current_pool_snapshot: Mapping[str, Any]
    baseline_pool_snapshot: Mapping[str, Any]
    identity: NormalizedIdentity
    current_liquidity: float
    baseline_liquidity: float
    collapse_bps: float


@dataclass(frozen=True, slots=True)
class SpreadCandidate:
    anomaly_type: str
    route_type: str
    buy_market: Mapping[str, Any]
    sell_market: Mapping[str, Any]
    buy_tick: Mapping[str, Any]
    sell_tick: Mapping[str, Any]
    pool_snapshot: Mapping[str, Any] | None
    dex_identity: NormalizedIdentity
    cex_identity: NormalizedIdentity
    buy_price: float
    sell_price: float
    spread_bps: float
    price_unit: str
    buy_price_source: str
    sell_price_source: str
    fx_rate: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class PoolDivergenceCandidate:
    buy_market: Mapping[str, Any]
    sell_market: Mapping[str, Any]
    buy_tick: Mapping[str, Any]
    sell_tick: Mapping[str, Any]
    buy_pool_snapshot: Mapping[str, Any] | None
    sell_pool_snapshot: Mapping[str, Any] | None
    buy_identity: NormalizedIdentity
    sell_identity: NormalizedIdentity
    buy_price: float
    sell_price: float
    spread_bps: float


@dataclass(frozen=True, slots=True)
class CrossChainSpreadCandidate:
    buy_market: Mapping[str, Any]
    sell_market: Mapping[str, Any]
    buy_tick: Mapping[str, Any]
    sell_tick: Mapping[str, Any]
    buy_pool_snapshot: Mapping[str, Any] | None
    sell_pool_snapshot: Mapping[str, Any] | None
    buy_identity: NormalizedIdentity
    sell_identity: NormalizedIdentity
    buy_price: float
    sell_price: float
    spread_bps: float
    bridge_group: str
    verification_evidence: Mapping[str, Any]


class ArbitrageDetector:
    """No-network detector foundation for normalized arbitrage observations."""

    def __init__(
        self,
        store: ArbitrageStore,
        *,
        drawdown_threshold_bps: float = DEFAULT_DRAWDOWN_THRESHOLD_BPS,
        spread_threshold_bps: float = DEFAULT_SPREAD_THRESHOLD_BPS,
        depeg_threshold_bps: float = DEFAULT_DEPEG_THRESHOLD_BPS,
        price_spike_threshold_bps: float = DEFAULT_PRICE_SPIKE_THRESHOLD_BPS,
        liquidity_collapse_threshold_bps: float = DEFAULT_LIQUIDITY_COLLAPSE_THRESHOLD_BPS,
        pool_divergence_threshold_bps: float = DEFAULT_POOL_DIVERGENCE_THRESHOLD_BPS,
        peg_reference_price_usd: float = DEFAULT_PEG_REFERENCE_PRICE_USD,
        stable_peg_symbols: tuple[str, ...] | list[str] = DEFAULT_STABLE_PEG_SYMBOLS,
        ttl_config: DetectorTTLConfig | Mapping[str, Any] | None = None,
        lookback_ms: int | None = None,
        normalizer: IdentityNormalizer | None = None,
    ):
        self.store = store
        self.drawdown_threshold_bps = float(drawdown_threshold_bps)
        self.spread_threshold_bps = float(spread_threshold_bps)
        self.depeg_threshold_bps = float(depeg_threshold_bps)
        self.price_spike_threshold_bps = float(price_spike_threshold_bps)
        self.liquidity_collapse_threshold_bps = float(liquidity_collapse_threshold_bps)
        self.pool_divergence_threshold_bps = float(pool_divergence_threshold_bps)
        self.peg_reference_price_usd = float(peg_reference_price_usd)
        self.stable_peg_symbols = tuple(str(symbol).strip().upper() for symbol in stable_peg_symbols if str(symbol).strip())
        self.ttl_config = (
            ttl_config
            if isinstance(ttl_config, DetectorTTLConfig)
            else DetectorTTLConfig.from_mapping(ttl_config)
        )
        self.lookback_ms = lookback_ms
        self.normalizer = normalizer or IdentityNormalizer(store)

    def run(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        return _combine_results(
            self.detect_dex_drawdowns(now_ms=stamp),
            self.detect_spreads(now_ms=stamp),
            self.detect_depegs(now_ms=stamp),
            self.detect_price_spikes(now_ms=stamp),
            self.detect_liquidity_collapses(now_ms=stamp),
            self.detect_pool_divergences(now_ms=stamp),
        )

    def detect_dex_drawdowns(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        opportunities = 0
        routes = 0
        blocked = 0
        skipped = 0

        for candidate in self._dex_drawdown_candidates(now_ms=stamp):
            if candidate.drawdown_bps < self.drawdown_threshold_bps:
                skipped += 1
                continue

            identity = self._normalize_dex_candidate(candidate)
            if not identity.executable or identity.asset_id is None:
                blocked += 1
                continue

            if self._rpc_freshness_blocks_chain(str(candidate.current_tick.get("chain_code") or ""), stamp):
                skipped += 1
                continue

            opportunity_id = self._upsert_drawdown_opportunity(candidate, identity, now_ms=stamp)
            self._upsert_same_dex_route(candidate, identity, opportunity_id, now_ms=stamp)
            opportunities += 1
            routes += 1

        return DetectorRunResult(
            opportunities_upserted=opportunities,
            routes_upserted=routes,
            blocked_identities=blocked,
            skipped=skipped,
        )

    def detect_spreads(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        return _combine_results(
            self.detect_dex_cex_spreads(now_ms=stamp),
            self.detect_dex_krw_spreads(now_ms=stamp),
            self.detect_cross_chain_spreads(now_ms=stamp),
        )

    def detect_dex_cex_spreads(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        return self._detect_cex_spreads(
            now_ms=stamp,
            anomaly_type="dex_cex_spread",
            quote_assets=USD_QUOTE_ASSETS,
            price_unit="USD",
        )

    def detect_dex_krw_spreads(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        return self._detect_cex_spreads(
            now_ms=stamp,
            anomaly_type="dex_krw_spread",
            quote_assets=KRW_QUOTE_ASSETS,
            price_unit="KRW",
        )

    def detect_cross_chain_spreads(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        opportunities = 0
        routes = 0
        blocked = 0
        skipped = 0

        entries: list[tuple[dict[str, Any], dict[str, Any] | None, NormalizedIdentity]] = []
        for dex_tick in self._fetch_latest_dex_ticks(now_ms=stamp):
            pool_snapshot = self._fetch_latest_pool_snapshot(
                market_id=int(dex_tick["market_id"]),
                at_or_before_ms=int(dex_tick["observed_at_ms"]),
            )
            identity = self._normalize_dex_market_row(dex_tick, pool_snapshot=pool_snapshot)
            if not identity.executable or identity.asset_id is None:
                blocked += 1
                continue
            entries.append((dex_tick, pool_snapshot, identity))

        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                candidate = self._cross_chain_candidate(left, right)
                if candidate is None:
                    skipped += 1
                    continue
                if not candidate.bridge_group:
                    self._append_cross_chain_identity_dead_letter(candidate)
                    blocked += 1
                    continue
                if candidate.spread_bps <= self.spread_threshold_bps:
                    skipped += 1
                    continue
                if self._rpc_freshness_blocks_chain(str(candidate.buy_market.get("chain_code") or ""), stamp):
                    skipped += 1
                    continue
                if self._rpc_freshness_blocks_chain(str(candidate.sell_market.get("chain_code") or ""), stamp):
                    skipped += 1
                    continue

                opportunity_id = self._upsert_cross_chain_opportunity(candidate, now_ms=stamp)
                self._upsert_cross_chain_route(candidate, opportunity_id, now_ms=stamp)
                opportunities += 1
                routes += 1

        return DetectorRunResult(
            opportunities_upserted=opportunities,
            routes_upserted=routes,
            blocked_identities=blocked,
            skipped=skipped,
        )

    def detect_depegs(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        opportunities = 0
        routes = 0
        blocked = 0
        skipped = 0

        for dex_tick in self._fetch_latest_dex_ticks(now_ms=stamp):
            reference_price = self._peg_reference_price(dex_tick)
            current_price = _positive_float(dex_tick.get("price_usd")) or _positive_float(dex_tick.get("raw_price"))
            if reference_price is None or current_price is None:
                skipped += 1
                continue
            deviation_bps = abs(current_price - reference_price) / reference_price * 10_000.0
            if deviation_bps < self.depeg_threshold_bps:
                skipped += 1
                continue

            pool_snapshot = self._fetch_latest_pool_snapshot(
                market_id=int(dex_tick["market_id"]),
                at_or_before_ms=int(dex_tick["observed_at_ms"]),
            )
            identity = self._normalize_dex_market_row(dex_tick, pool_snapshot=pool_snapshot)
            if not identity.executable or identity.asset_id is None:
                blocked += 1
                continue
            if self._rpc_freshness_blocks_chain(str(dex_tick.get("chain_code") or ""), stamp):
                skipped += 1
                continue

            candidate = DepegCandidate(
                market=dex_tick,
                current_tick=dex_tick,
                pool_snapshot=pool_snapshot,
                identity=identity,
                price=current_price,
                reference_price=reference_price,
                deviation_bps=deviation_bps,
                direction="below_peg" if current_price < reference_price else "above_peg",
            )
            opportunity_id = self._upsert_depeg_opportunity(candidate, now_ms=stamp)
            self._upsert_single_dex_route(
                anomaly_type="depeg",
                detection_reason=f"stable_{candidate.direction}",
                opportunity_key=_depeg_opportunity_key(candidate),
                opportunity_id=opportunity_id,
                market=candidate.market,
                identity=identity,
                metric_bps=deviation_bps,
                source_freshness=self._dex_source_freshness(
                    candidate.current_tick,
                    pool_snapshot=candidate.pool_snapshot,
                    now_ms=stamp,
                ),
            )
            opportunities += 1
            routes += 1

        return DetectorRunResult(
            opportunities_upserted=opportunities,
            routes_upserted=routes,
            blocked_identities=blocked,
            skipped=skipped,
        )

    def detect_price_spikes(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        opportunities = 0
        routes = 0
        blocked = 0
        skipped = 0

        for candidate in self._dex_price_spike_candidates(now_ms=stamp):
            if candidate.spike_bps < self.price_spike_threshold_bps:
                skipped += 1
                continue

            identity = self._normalize_dex_market_row(candidate.current_tick, pool_snapshot=candidate.pool_snapshot)
            if not identity.executable or identity.asset_id is None:
                blocked += 1
                continue
            if self._rpc_freshness_blocks_chain(str(candidate.current_tick.get("chain_code") or ""), stamp):
                skipped += 1
                continue

            opportunity_id = self._upsert_price_spike_opportunity(candidate, identity, now_ms=stamp)
            self._upsert_single_dex_route(
                anomaly_type="price_spike",
                detection_reason="dex_price_spike_upside",
                opportunity_key=_price_spike_opportunity_key(candidate),
                opportunity_id=opportunity_id,
                market=candidate.market,
                identity=identity,
                metric_bps=candidate.spike_bps,
                source_freshness=self._dex_source_freshness(
                    candidate.current_tick,
                    pool_snapshot=candidate.pool_snapshot,
                    now_ms=stamp,
                ),
            )
            opportunities += 1
            routes += 1

        return DetectorRunResult(
            opportunities_upserted=opportunities,
            routes_upserted=routes,
            blocked_identities=blocked,
            skipped=skipped,
        )

    def detect_liquidity_collapses(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        opportunities = 0
        routes = 0
        blocked = 0
        skipped = 0

        for dex_tick in self._fetch_latest_dex_ticks(now_ms=stamp):
            current_pool = self._fetch_latest_pool_snapshot(
                market_id=int(dex_tick["market_id"]),
                at_or_before_ms=int(dex_tick["observed_at_ms"]),
            )
            if current_pool is None or not self._fresh_enough(
                current_pool.get("observed_at_ms"),
                ttl_ms=self.ttl_config.pool_snapshot_ttl_ms,
                now_ms=stamp,
            ):
                skipped += 1
                continue
            baseline_pool = self._fetch_prior_pool_snapshot(
                market_id=int(dex_tick["market_id"]),
                before_observed_at_ms=int(current_pool["observed_at_ms"]),
            )
            if baseline_pool is None:
                skipped += 1
                continue
            current_liquidity = _pool_liquidity_value(current_pool)
            baseline_liquidity = _pool_liquidity_value(baseline_pool)
            if current_liquidity is None or baseline_liquidity is None or baseline_liquidity <= 0:
                skipped += 1
                continue
            collapse_bps = ((baseline_liquidity - current_liquidity) / baseline_liquidity) * 10_000.0
            if collapse_bps < self.liquidity_collapse_threshold_bps:
                skipped += 1
                continue

            identity = self._normalize_dex_market_row(dex_tick, pool_snapshot=current_pool)
            if not identity.executable or identity.asset_id is None:
                blocked += 1
                continue
            if self._rpc_freshness_blocks_chain(str(dex_tick.get("chain_code") or ""), stamp):
                skipped += 1
                continue

            candidate = LiquidityCollapseCandidate(
                market=dex_tick,
                current_tick=dex_tick,
                current_pool_snapshot=current_pool,
                baseline_pool_snapshot=baseline_pool,
                identity=identity,
                current_liquidity=current_liquidity,
                baseline_liquidity=baseline_liquidity,
                collapse_bps=collapse_bps,
            )
            opportunity_id = self._upsert_liquidity_collapse_opportunity(candidate, now_ms=stamp)
            self._upsert_single_dex_route(
                anomaly_type="liquidity_collapse",
                detection_reason="dex_pool_liquidity_collapse",
                opportunity_key=_liquidity_collapse_opportunity_key(candidate),
                opportunity_id=opportunity_id,
                market=candidate.market,
                identity=identity,
                metric_bps=collapse_bps,
                source_freshness=self._dex_source_freshness(
                    candidate.current_tick,
                    pool_snapshot=candidate.current_pool_snapshot,
                    now_ms=stamp,
                ),
            )
            opportunities += 1
            routes += 1

        return DetectorRunResult(
            opportunities_upserted=opportunities,
            routes_upserted=routes,
            blocked_identities=blocked,
            skipped=skipped,
        )

    def detect_pool_divergences(self, *, now_ms: int | None = None) -> DetectorRunResult:
        stamp = self._resolve_now_ms(now_ms)
        opportunities = 0
        routes = 0
        blocked = 0
        skipped = 0

        entries: list[tuple[dict[str, Any], dict[str, Any] | None, NormalizedIdentity]] = []
        for dex_tick in self._fetch_latest_dex_ticks(now_ms=stamp):
            pool_snapshot = self._fetch_latest_pool_snapshot(
                market_id=int(dex_tick["market_id"]),
                at_or_before_ms=int(dex_tick["observed_at_ms"]),
            )
            identity = self._normalize_dex_market_row(dex_tick, pool_snapshot=pool_snapshot)
            if not identity.executable or identity.asset_id is None:
                blocked += 1
                continue
            entries.append((dex_tick, pool_snapshot, identity))

        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                candidate = self._pool_divergence_candidate(left, right)
                if candidate is None:
                    skipped += 1
                    continue
                if candidate.spread_bps <= self.pool_divergence_threshold_bps:
                    skipped += 1
                    continue
                if self._rpc_freshness_blocks_chain(str(candidate.buy_market.get("chain_code") or ""), stamp):
                    skipped += 1
                    continue

                opportunity_id = self._upsert_pool_divergence_opportunity(candidate, now_ms=stamp)
                self._upsert_pool_divergence_route(candidate, opportunity_id, now_ms=stamp)
                opportunities += 1
                routes += 1

        return DetectorRunResult(
            opportunities_upserted=opportunities,
            routes_upserted=routes,
            blocked_identities=blocked,
            skipped=skipped,
        )

    def _detect_cex_spreads(
        self,
        *,
        now_ms: int | None,
        anomaly_type: str,
        quote_assets: tuple[str, ...],
        price_unit: str,
    ) -> DetectorRunResult:
        opportunities = 0
        routes = 0
        blocked = 0
        skipped = 0

        stamp = self._resolve_now_ms(now_ms)
        orderbook_ttl_ms = (
            self.ttl_config.krw_orderbook_ttl_ms
            if price_unit == "KRW"
            else self.ttl_config.cex_orderbook_ttl_ms
        )
        cex_ticks = self._fetch_latest_cex_orderbook_ticks(
            now_ms=stamp,
            quote_assets=quote_assets,
            ttl_ms=orderbook_ttl_ms,
        )
        cex_by_asset: dict[int, list[tuple[dict[str, Any], NormalizedIdentity]]] = {}
        for cex_tick in cex_ticks:
            cex_identity = self.normalizer.normalize_market(int(cex_tick["market_id"]))
            if not cex_identity.executable or cex_identity.asset_id is None:
                blocked += 1
                continue
            cex_by_asset.setdefault(int(cex_identity.asset_id), []).append((cex_tick, cex_identity))

        fx_rate = self._fetch_latest_usdt_krw_rate(now_ms=stamp) if price_unit == "KRW" else None
        if price_unit == "KRW" and fx_rate is None:
            return DetectorRunResult(skipped=len(cex_ticks))

        for dex_tick in self._fetch_latest_dex_ticks(now_ms=stamp):
            pool_snapshot = self._fetch_latest_pool_snapshot(
                market_id=int(dex_tick["market_id"]),
                at_or_before_ms=int(dex_tick["observed_at_ms"]),
            )
            dex_identity = self._normalize_dex_market_row(dex_tick, pool_snapshot=pool_snapshot)
            if not dex_identity.executable or dex_identity.asset_id is None:
                blocked += 1
                continue
            if self._rpc_freshness_blocks_chain(str(dex_tick.get("chain_code") or ""), stamp):
                skipped += 1
                continue

            sell_candidates = cex_by_asset.get(int(dex_identity.asset_id), [])
            if not sell_candidates:
                continue

            buy_price, buy_price_source = _dex_buy_price(dex_tick, price_unit=price_unit, fx_rate=fx_rate)
            if buy_price is None:
                skipped += len(sell_candidates)
                continue

            for cex_tick, cex_identity in sell_candidates:
                sell_price, sell_price_source = _cex_sell_price(cex_tick, price_unit=price_unit)
                if sell_price is None:
                    skipped += 1
                    continue

                spread_bps = ((sell_price - buy_price) / buy_price) * 10_000.0
                if spread_bps <= self.spread_threshold_bps:
                    skipped += 1
                    continue

                candidate = SpreadCandidate(
                    anomaly_type=anomaly_type,
                    route_type="direct_cex_sell",
                    buy_market=dex_tick,
                    sell_market=cex_tick,
                    buy_tick=dex_tick,
                    sell_tick=cex_tick,
                    pool_snapshot=pool_snapshot,
                    dex_identity=dex_identity,
                    cex_identity=cex_identity,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    spread_bps=spread_bps,
                    price_unit=price_unit,
                    buy_price_source=buy_price_source,
                    sell_price_source=sell_price_source,
                    fx_rate=fx_rate,
                )
                opportunity_id = self._upsert_spread_opportunity(candidate, now_ms=stamp)
                self._upsert_spread_route(candidate, opportunity_id, now_ms=stamp)
                opportunities += 1
                routes += 1

        return DetectorRunResult(
            opportunities_upserted=opportunities,
            routes_upserted=routes,
            blocked_identities=blocked,
            skipped=skipped,
        )

    def _dex_drawdown_candidates(self, *, now_ms: int | None) -> list[DexDrawdownCandidate]:
        candidates: list[DexDrawdownCandidate] = []
        for current_tick in self._fetch_latest_dex_ticks(now_ms=now_ms):
            baseline_tick = self._fetch_prior_tick(
                market_id=int(current_tick["market_id"]),
                before_observed_at_ms=int(current_tick["observed_at_ms"]),
            )
            if baseline_tick is None:
                continue

            current_price = _tick_price(current_tick)
            baseline_price = _tick_price(baseline_tick)
            if current_price is None or baseline_price is None or baseline_price <= 0:
                continue

            drawdown_bps = ((baseline_price - current_price) / baseline_price) * 10_000.0
            if drawdown_bps <= 0:
                continue

            pool_snapshot = self._fetch_latest_pool_snapshot(
                market_id=int(current_tick["market_id"]),
                at_or_before_ms=int(current_tick["observed_at_ms"]),
            )
            candidates.append(
                DexDrawdownCandidate(
                    market=current_tick,
                    current_tick=current_tick,
                    baseline_tick=baseline_tick,
                    pool_snapshot=pool_snapshot,
                    current_price=current_price,
                    baseline_price=baseline_price,
                    drawdown_bps=drawdown_bps,
                )
            )
        return candidates

    def _dex_price_spike_candidates(self, *, now_ms: int | None) -> list[DexPriceSpikeCandidate]:
        candidates: list[DexPriceSpikeCandidate] = []
        for current_tick in self._fetch_latest_dex_ticks(now_ms=now_ms):
            baseline_tick = self._fetch_prior_tick(
                market_id=int(current_tick["market_id"]),
                before_observed_at_ms=int(current_tick["observed_at_ms"]),
            )
            if baseline_tick is None:
                continue

            current_price = _tick_price(current_tick)
            baseline_price = _tick_price(baseline_tick)
            if current_price is None or baseline_price is None or baseline_price <= 0:
                continue

            spike_bps = ((current_price - baseline_price) / baseline_price) * 10_000.0
            if spike_bps <= 0:
                continue

            pool_snapshot = self._fetch_latest_pool_snapshot(
                market_id=int(current_tick["market_id"]),
                at_or_before_ms=int(current_tick["observed_at_ms"]),
            )
            candidates.append(
                DexPriceSpikeCandidate(
                    market=current_tick,
                    current_tick=current_tick,
                    baseline_tick=baseline_tick,
                    pool_snapshot=pool_snapshot,
                    current_price=current_price,
                    baseline_price=baseline_price,
                    spike_bps=spike_bps,
                )
            )
        return candidates

    def _normalize_dex_candidate(self, candidate: DexDrawdownCandidate) -> NormalizedIdentity:
        hints = _identity_hints(candidate)
        return self.normalizer.normalize_market(
            int(candidate.market["market_id"]),
            token_chain_id=hints.get("chain_id"),
            token_contract_address=hints.get("token_contract_address"),
        )

    def _normalize_dex_market_row(
        self,
        market_row: Mapping[str, Any],
        *,
        pool_snapshot: Mapping[str, Any] | None,
    ) -> NormalizedIdentity:
        hints = _dex_identity_hints(
            market_row=market_row,
            current_tick=market_row,
            pool_snapshot=pool_snapshot,
        )
        return self.normalizer.normalize_market(
            int(market_row["market_id"]),
            token_chain_id=hints.get("chain_id"),
            token_contract_address=hints.get("token_contract_address"),
        )

    def _upsert_drawdown_opportunity(
        self,
        candidate: DexDrawdownCandidate,
        identity: NormalizedIdentity,
        *,
        now_ms: int,
    ) -> int:
        market_id = int(candidate.market["market_id"])
        opportunity_key = _drawdown_opportunity_key(candidate)
        source_freshness = self._dex_source_freshness(
            candidate.current_tick,
            pool_snapshot=candidate.pool_snapshot,
            now_ms=now_ms,
        )
        payload = _drawdown_payload(
            candidate,
            identity,
            threshold_bps=self.drawdown_threshold_bps,
            source_freshness=source_freshness,
        )
        return self.store.upsert_opportunity(
            opportunity_key=opportunity_key,
            asset_id=int(identity.asset_id),
            anomaly_type="dex_drawdown",
            lifecycle_status="DETECTED",
            safety_status="WARN",
            buy_market_id=market_id,
            sell_market_id=market_id,
            spread_bps=candidate.drawdown_bps,
            edge_expected_bps=candidate.drawdown_bps,
            edge_worst_bps=0.0,
            first_seen_at_ms=int(candidate.current_tick["observed_at_ms"]),
            last_seen_at_ms=int(candidate.current_tick["observed_at_ms"]),
            payload=payload,
        )

    def _upsert_same_dex_route(
        self,
        candidate: DexDrawdownCandidate,
        identity: NormalizedIdentity,
        opportunity_id: int,
        *,
        now_ms: int,
    ) -> int:
        market_id = int(candidate.market["market_id"])
        opportunity_key = _drawdown_opportunity_key(candidate)
        source_freshness = self._dex_source_freshness(
            candidate.current_tick,
            pool_snapshot=candidate.pool_snapshot,
            now_ms=now_ms,
        )
        return self.store.upsert_route(
            route_key=f"{opportunity_key}:same_dex_sell",
            opportunity_id=opportunity_id,
            route_type="same_dex_sell",
            buy_market_id=market_id,
            sell_market_id=market_id,
            safety_status="WARN",
            route_status="WAIT",
            edge_expected_bps=candidate.drawdown_bps,
            edge_worst_bps=0.0,
            selected=True,
            edge_worst_verified=False,
            warning_reasons=["candidate_only", "edge_worst_unverified"],
            payload={
                "detector": "dex_drawdown",
                "anomaly_type": "dex_drawdown",
                "detection_reason": "dex_price_drawdown_from_prior_tick",
                "asset_id": identity.asset_id,
                "market_id": market_id,
                "current_tick_id": int(candidate.current_tick["id"]),
                "baseline_tick_id": int(candidate.baseline_tick["id"]),
                "drawdown_bps": candidate.drawdown_bps,
                "source_freshness": source_freshness,
                "candidate_only": True,
                "edge_worst_verified": False,
            },
        )

    def _upsert_price_spike_opportunity(
        self,
        candidate: DexPriceSpikeCandidate,
        identity: NormalizedIdentity,
        *,
        now_ms: int,
    ) -> int:
        market_id = int(candidate.market["market_id"])
        source_freshness = self._dex_source_freshness(
            candidate.current_tick,
            pool_snapshot=candidate.pool_snapshot,
            now_ms=now_ms,
        )
        return self.store.upsert_opportunity(
            opportunity_key=_price_spike_opportunity_key(candidate),
            asset_id=int(identity.asset_id),
            anomaly_type="price_spike",
            lifecycle_status="DETECTED",
            safety_status="WARN",
            buy_market_id=market_id,
            sell_market_id=market_id,
            spread_bps=candidate.spike_bps,
            edge_expected_bps=candidate.spike_bps,
            edge_worst_bps=0.0,
            first_seen_at_ms=int(candidate.current_tick["observed_at_ms"]),
            last_seen_at_ms=int(candidate.current_tick["observed_at_ms"]),
            payload=_price_spike_payload(candidate, identity, source_freshness=source_freshness),
        )

    def _upsert_depeg_opportunity(self, candidate: DepegCandidate, *, now_ms: int) -> int:
        market_id = int(candidate.market["market_id"])
        source_freshness = self._dex_source_freshness(
            candidate.current_tick,
            pool_snapshot=candidate.pool_snapshot,
            now_ms=now_ms,
        )
        return self.store.upsert_opportunity(
            opportunity_key=_depeg_opportunity_key(candidate),
            asset_id=int(candidate.identity.asset_id),
            anomaly_type="depeg",
            lifecycle_status="DETECTED",
            safety_status="WARN",
            buy_market_id=market_id,
            sell_market_id=market_id,
            spread_bps=candidate.deviation_bps,
            edge_expected_bps=candidate.deviation_bps,
            edge_worst_bps=0.0,
            first_seen_at_ms=int(candidate.current_tick["observed_at_ms"]),
            last_seen_at_ms=int(candidate.current_tick["observed_at_ms"]),
            payload=_depeg_payload(candidate, source_freshness=source_freshness),
        )

    def _upsert_liquidity_collapse_opportunity(
        self,
        candidate: LiquidityCollapseCandidate,
        *,
        now_ms: int,
    ) -> int:
        market_id = int(candidate.market["market_id"])
        source_freshness = self._dex_source_freshness(
            candidate.current_tick,
            pool_snapshot=candidate.current_pool_snapshot,
            now_ms=now_ms,
        )
        return self.store.upsert_opportunity(
            opportunity_key=_liquidity_collapse_opportunity_key(candidate),
            asset_id=int(candidate.identity.asset_id),
            anomaly_type="liquidity_collapse",
            lifecycle_status="DETECTED",
            safety_status="WARN",
            buy_market_id=market_id,
            sell_market_id=market_id,
            spread_bps=candidate.collapse_bps,
            edge_expected_bps=candidate.collapse_bps,
            edge_worst_bps=0.0,
            first_seen_at_ms=int(candidate.current_pool_snapshot["observed_at_ms"]),
            last_seen_at_ms=int(candidate.current_pool_snapshot["observed_at_ms"]),
            payload=_liquidity_collapse_payload(candidate, source_freshness=source_freshness),
        )

    def _upsert_single_dex_route(
        self,
        *,
        anomaly_type: str,
        detection_reason: str,
        opportunity_key: str,
        opportunity_id: int,
        market: Mapping[str, Any],
        identity: NormalizedIdentity,
        metric_bps: float,
        source_freshness: Mapping[str, Any],
    ) -> int:
        market_id = int(market["market_id"])
        return self.store.upsert_route(
            route_key=f"{opportunity_key}:same_dex_sell",
            opportunity_id=opportunity_id,
            route_type="same_dex_sell",
            buy_market_id=market_id,
            sell_market_id=market_id,
            safety_status="WARN",
            route_status="WAIT",
            edge_expected_bps=metric_bps,
            edge_worst_bps=0.0,
            selected=True,
            edge_worst_verified=False,
            warning_reasons=["candidate_only", "edge_worst_unverified"],
            payload={
                "detector": anomaly_type,
                "anomaly_type": anomaly_type,
                "detection_reason": detection_reason,
                "asset_id": identity.asset_id,
                "market_id": market_id,
                "route_type": "same_dex_sell",
                "spread_bps": metric_bps,
                "edge_worst_bps": 0.0,
                "source_freshness": dict(source_freshness),
                "candidate_only": True,
                "edge_worst_verified": False,
            },
        )

    def _upsert_spread_opportunity(self, candidate: SpreadCandidate, *, now_ms: int) -> int:
        return self.store.upsert_opportunity(
            opportunity_key=_spread_opportunity_key(candidate),
            asset_id=int(candidate.dex_identity.asset_id),
            anomaly_type=candidate.anomaly_type,
            lifecycle_status="DETECTED",
            safety_status="WARN",
            buy_market_id=int(candidate.buy_market["market_id"]),
            sell_market_id=int(candidate.sell_market["market_id"]),
            spread_bps=candidate.spread_bps,
            edge_expected_bps=candidate.spread_bps,
            edge_worst_bps=0.0,
            first_seen_at_ms=int(candidate.buy_tick["observed_at_ms"]),
            last_seen_at_ms=max(
                int(candidate.buy_tick["observed_at_ms"]),
                int(candidate.sell_tick["observed_at_ms"]),
            ),
            payload=_spread_payload(
                candidate,
                source_freshness=self._spread_source_freshness(candidate, now_ms=now_ms),
            ),
        )

    def _upsert_spread_route(self, candidate: SpreadCandidate, opportunity_id: int, *, now_ms: int) -> int:
        warnings = _spread_route_warnings(candidate)
        source_freshness = self._spread_source_freshness(candidate, now_ms=now_ms)
        return self.store.upsert_route(
            route_key=f"{_spread_opportunity_key(candidate)}:{candidate.route_type}",
            opportunity_id=opportunity_id,
            route_type=candidate.route_type,
            buy_market_id=int(candidate.buy_market["market_id"]),
            sell_market_id=int(candidate.sell_market["market_id"]),
            safety_status="WARN",
            route_status="WAIT",
            edge_expected_bps=candidate.spread_bps,
            edge_worst_bps=0.0,
            selected=True,
            edge_worst_verified=False,
            blocker_reasons=[],
            warning_reasons=warnings,
            payload={
                "detector": candidate.anomaly_type,
                "asset_id": candidate.dex_identity.asset_id,
                "buy_market_id": int(candidate.buy_market["market_id"]),
                "sell_market_id": int(candidate.sell_market["market_id"]),
                "route_type": candidate.route_type,
                "spread_bps": candidate.spread_bps,
                "edge_worst_bps": 0.0,
                "candidate_only": True,
                "edge_worst_verified": False,
                "deposit_network": str(candidate.sell_market.get("deposit_network") or ""),
                "warning_reasons": warnings,
                "source_freshness": source_freshness,
            },
        )

    def _cross_chain_candidate(
        self,
        left: tuple[dict[str, Any], dict[str, Any] | None, NormalizedIdentity],
        right: tuple[dict[str, Any], dict[str, Any] | None, NormalizedIdentity],
    ) -> CrossChainSpreadCandidate | None:
        left_tick, left_pool_snapshot, left_identity = left
        right_tick, right_pool_snapshot, right_identity = right
        if left_identity.asset_id != right_identity.asset_id:
            return None
        if _identity_chain_key(left_identity) == _identity_chain_key(right_identity):
            return None

        left_price, _ = _dex_buy_price(left_tick, price_unit="USD", fx_rate=None)
        right_price, _ = _dex_buy_price(right_tick, price_unit="USD", fx_rate=None)
        if left_price is None or right_price is None:
            return None
        if left_price == right_price:
            return None

        bridge_group, evidence = _cross_chain_verification(left_identity, right_identity)
        if left_price < right_price:
            buy_tick = left_tick
            sell_tick = right_tick
            buy_pool_snapshot = left_pool_snapshot
            sell_pool_snapshot = right_pool_snapshot
            buy_identity = left_identity
            sell_identity = right_identity
            buy_price = left_price
            sell_price = right_price
        else:
            buy_tick = right_tick
            sell_tick = left_tick
            buy_pool_snapshot = right_pool_snapshot
            sell_pool_snapshot = left_pool_snapshot
            buy_identity = right_identity
            sell_identity = left_identity
            buy_price = right_price
            sell_price = left_price

        return CrossChainSpreadCandidate(
            buy_market=buy_tick,
            sell_market=sell_tick,
            buy_tick=buy_tick,
            sell_tick=sell_tick,
            buy_pool_snapshot=buy_pool_snapshot,
            sell_pool_snapshot=sell_pool_snapshot,
            buy_identity=buy_identity,
            sell_identity=sell_identity,
            buy_price=buy_price,
            sell_price=sell_price,
            spread_bps=((sell_price - buy_price) / buy_price) * 10_000.0,
            bridge_group=bridge_group,
            verification_evidence=evidence,
        )

    def _pool_divergence_candidate(
        self,
        left: tuple[dict[str, Any], dict[str, Any] | None, NormalizedIdentity],
        right: tuple[dict[str, Any], dict[str, Any] | None, NormalizedIdentity],
    ) -> PoolDivergenceCandidate | None:
        left_tick, left_pool_snapshot, left_identity = left
        right_tick, right_pool_snapshot, right_identity = right
        if left_identity.asset_id != right_identity.asset_id:
            return None
        if _identity_chain_key(left_identity) != _identity_chain_key(right_identity):
            return None
        if not _same_contract_identity(left_identity, right_identity):
            return None

        left_price, _ = _dex_buy_price(left_tick, price_unit="USD", fx_rate=None)
        right_price, _ = _dex_buy_price(right_tick, price_unit="USD", fx_rate=None)
        if left_price is None or right_price is None:
            return None
        if left_price == right_price:
            return None

        if left_price < right_price:
            buy_tick = left_tick
            sell_tick = right_tick
            buy_pool_snapshot = left_pool_snapshot
            sell_pool_snapshot = right_pool_snapshot
            buy_identity = left_identity
            sell_identity = right_identity
            buy_price = left_price
            sell_price = right_price
        else:
            buy_tick = right_tick
            sell_tick = left_tick
            buy_pool_snapshot = right_pool_snapshot
            sell_pool_snapshot = left_pool_snapshot
            buy_identity = right_identity
            sell_identity = left_identity
            buy_price = right_price
            sell_price = left_price

        return PoolDivergenceCandidate(
            buy_market=buy_tick,
            sell_market=sell_tick,
            buy_tick=buy_tick,
            sell_tick=sell_tick,
            buy_pool_snapshot=buy_pool_snapshot,
            sell_pool_snapshot=sell_pool_snapshot,
            buy_identity=buy_identity,
            sell_identity=sell_identity,
            buy_price=buy_price,
            sell_price=sell_price,
            spread_bps=((sell_price - buy_price) / buy_price) * 10_000.0,
        )

    def _upsert_pool_divergence_opportunity(
        self,
        candidate: PoolDivergenceCandidate,
        *,
        now_ms: int,
    ) -> int:
        return self.store.upsert_opportunity(
            opportunity_key=_pool_divergence_opportunity_key(candidate),
            asset_id=int(candidate.buy_identity.asset_id),
            anomaly_type="pool_divergence",
            lifecycle_status="DETECTED",
            safety_status="WARN",
            buy_market_id=int(candidate.buy_market["market_id"]),
            sell_market_id=int(candidate.sell_market["market_id"]),
            spread_bps=candidate.spread_bps,
            edge_expected_bps=candidate.spread_bps,
            edge_worst_bps=0.0,
            first_seen_at_ms=int(candidate.buy_tick["observed_at_ms"]),
            last_seen_at_ms=max(
                int(candidate.buy_tick["observed_at_ms"]),
                int(candidate.sell_tick["observed_at_ms"]),
            ),
            payload=_pool_divergence_payload(
                candidate,
                source_freshness=self._pool_divergence_source_freshness(candidate, now_ms=now_ms),
            ),
        )

    def _upsert_pool_divergence_route(
        self,
        candidate: PoolDivergenceCandidate,
        opportunity_id: int,
        *,
        now_ms: int,
    ) -> int:
        warnings = ["candidate_only", "edge_worst_unverified"]
        return self.store.upsert_route(
            route_key=f"{_pool_divergence_opportunity_key(candidate)}:same_dex_sell",
            opportunity_id=opportunity_id,
            route_type="same_dex_sell",
            buy_market_id=int(candidate.buy_market["market_id"]),
            sell_market_id=int(candidate.sell_market["market_id"]),
            safety_status="WARN",
            route_status="WAIT",
            edge_expected_bps=candidate.spread_bps,
            edge_worst_bps=0.0,
            selected=True,
            edge_worst_verified=False,
            blocker_reasons=[],
            warning_reasons=warnings,
            payload={
                "detector": "pool_divergence",
                "anomaly_type": "pool_divergence",
                "detection_reason": "same_asset_pool_price_divergence",
                "asset_id": candidate.buy_identity.asset_id,
                "buy_market_id": int(candidate.buy_market["market_id"]),
                "sell_market_id": int(candidate.sell_market["market_id"]),
                "route_type": "same_dex_sell",
                "spread_bps": candidate.spread_bps,
                "edge_worst_bps": 0.0,
                "source_freshness": self._pool_divergence_source_freshness(candidate, now_ms=now_ms),
                "candidate_only": True,
                "edge_worst_verified": False,
                "warning_reasons": warnings,
            },
        )

    def _upsert_cross_chain_opportunity(
        self,
        candidate: CrossChainSpreadCandidate,
        *,
        now_ms: int,
    ) -> int:
        return self.store.upsert_opportunity(
            opportunity_key=_cross_chain_opportunity_key(candidate),
            asset_id=int(candidate.buy_identity.asset_id),
            anomaly_type="cross_chain_spread",
            lifecycle_status="DETECTED",
            safety_status="WARN",
            buy_market_id=int(candidate.buy_market["market_id"]),
            sell_market_id=int(candidate.sell_market["market_id"]),
            spread_bps=candidate.spread_bps,
            edge_expected_bps=candidate.spread_bps,
            edge_worst_bps=0.0,
            first_seen_at_ms=int(candidate.buy_tick["observed_at_ms"]),
            last_seen_at_ms=max(
                int(candidate.buy_tick["observed_at_ms"]),
                int(candidate.sell_tick["observed_at_ms"]),
            ),
            payload=_cross_chain_payload(
                candidate,
                source_freshness=self._cross_chain_source_freshness(candidate, now_ms=now_ms),
            ),
        )

    def _upsert_cross_chain_route(
        self,
        candidate: CrossChainSpreadCandidate,
        opportunity_id: int,
        *,
        now_ms: int,
    ) -> int:
        warnings = _cross_chain_route_warnings()
        return self.store.upsert_route(
            route_key=f"{_cross_chain_opportunity_key(candidate)}:bridge_dex_sell",
            opportunity_id=opportunity_id,
            route_type="bridge_dex_sell",
            buy_market_id=int(candidate.buy_market["market_id"]),
            sell_market_id=int(candidate.sell_market["market_id"]),
            safety_status="WARN",
            route_status="WAIT",
            edge_expected_bps=candidate.spread_bps,
            edge_worst_bps=0.0,
            selected=True,
            edge_worst_verified=False,
            blocker_reasons=[],
            warning_reasons=warnings,
            payload={
                "detector": "cross_chain_spread",
                "asset_id": candidate.buy_identity.asset_id,
                "buy_market_id": int(candidate.buy_market["market_id"]),
                "sell_market_id": int(candidate.sell_market["market_id"]),
                "route_type": "bridge_dex_sell",
                "spread_bps": candidate.spread_bps,
                "edge_worst_bps": 0.0,
                "bridge_group": candidate.bridge_group,
                "candidate_only": True,
                "edge_worst_verified": False,
                "bridge_quote_evaluated": False,
                "warning_reasons": warnings,
                "source_freshness": self._cross_chain_source_freshness(candidate, now_ms=now_ms),
            },
        )

    def _append_cross_chain_identity_dead_letter(self, candidate: CrossChainSpreadCandidate) -> None:
        buy_market_id = int(candidate.buy_market["market_id"])
        sell_market_id = int(candidate.sell_market["market_id"])
        market_ids = sorted([buy_market_id, sell_market_id])
        payload = {
            "error_code": "symbol_only_cross_chain_identity",
            "identity_status": "UNKNOWN",
            "buy_market_id": buy_market_id,
            "sell_market_id": sell_market_id,
            "buy_identity": candidate.buy_identity.to_dict(),
            "sell_identity": candidate.sell_identity.to_dict(),
            "verification_evidence": dict(candidate.verification_evidence),
        }
        self.store.append_dead_letter(
            reason="cross_chain_identity",
            deadletter_key=f"cross_chain_identity:symbol_only:{market_ids[0]}:{market_ids[1]}",
            error_code="symbol_only_cross_chain_identity",
            retryable=False,
            payload=payload,
        )

    def _fetch_latest_dex_ticks(self, *, now_ms: int | None) -> list[dict[str, Any]]:
        stamp = self._resolve_now_ms(now_ms)
        params: list[Any] = []
        filters = [
            "COALESCE(t.stale, 0) = 0",
            "(UPPER(m.market_type) = 'DEX_POOL' OR UPPER(v.venue_type) = 'DEX')",
        ]
        filters.append("t.observed_at_ms <= ?")
        params.append(stamp)
        filters.append("t.observed_at_ms >= ?")
        params.append(self._lower_bound_ms(stamp, self.ttl_config.dex_tick_ttl_ms))

        with self.store.conn() as conn:
            rows = [
                _row_to_dict(row)
                for row in conn.execute(
                    f"""
                    SELECT
                        t.*,
                        m.id AS market_id,
                        m.market_key,
                        m.asset_id,
                        m.market_type,
                        m.chain_code,
                        m.pool_address,
                        m.market_symbol,
                        m.quote_asset,
                        m.payload_json AS market_payload_json,
                        a.symbol AS asset_symbol,
                        v.id AS venue_id,
                        v.venue_code,
                        v.venue_type
                    FROM arb_market_ticks t
                    JOIN arb_markets m ON m.id = t.market_id
                    JOIN arb_assets a ON a.id = m.asset_id
                    JOIN arb_venues v ON v.id = m.venue_id
                    WHERE {" AND ".join(filters)}
                    ORDER BY t.market_id, t.observed_at_ms DESC, t.id DESC
                    """,
                    params,
                ).fetchall()
            ]

        latest_by_market: dict[int, dict[str, Any]] = {}
        for row in rows:
            latest_by_market.setdefault(int(row["market_id"]), row)
        return list(latest_by_market.values())

    def _fetch_latest_cex_orderbook_ticks(
        self,
        *,
        now_ms: int | None,
        quote_assets: tuple[str, ...],
        ttl_ms: int,
    ) -> list[dict[str, Any]]:
        stamp = self._resolve_now_ms(now_ms)
        params: list[Any] = []
        filters = [
            "COALESCE(t.stale, 0) = 0",
            "(UPPER(m.market_type) LIKE 'CEX%' OR UPPER(v.venue_type) = 'CEX')",
        ]
        if quote_assets:
            placeholders = ", ".join("?" for _ in quote_assets)
            filters.append(f"UPPER(m.quote_asset) IN ({placeholders})")
            params.extend(asset.upper() for asset in quote_assets)
        filters.append("t.observed_at_ms <= ?")
        params.append(stamp)
        filters.append("t.observed_at_ms >= ?")
        params.append(self._lower_bound_ms(stamp, ttl_ms))

        with self.store.conn() as conn:
            rows = [
                _row_to_dict(row)
                for row in conn.execute(
                    f"""
                    SELECT
                        t.*,
                        m.id AS market_id,
                        m.market_key,
                        m.asset_id,
                        m.market_type,
                        m.chain_code,
                        m.pool_address,
                        m.market_symbol,
                        m.quote_asset,
                        m.deposit_network,
                        m.payload_json AS market_payload_json,
                        a.symbol AS asset_symbol,
                        v.id AS venue_id,
                        v.venue_code,
                        v.venue_type
                    FROM arb_market_ticks t
                    JOIN arb_markets m ON m.id = t.market_id
                    JOIN arb_assets a ON a.id = m.asset_id
                    JOIN arb_venues v ON v.id = m.venue_id
                    WHERE {" AND ".join(filters)}
                    ORDER BY t.market_id, t.observed_at_ms DESC, t.id DESC
                    """,
                    params,
                ).fetchall()
            ]

        latest_by_market: dict[int, dict[str, Any]] = {}
        for row in rows:
            latest_by_market.setdefault(int(row["market_id"]), row)
        return list(latest_by_market.values())

    def _fetch_latest_usdt_krw_rate(self, *, now_ms: int | None) -> dict[str, Any] | None:
        stamp = self._resolve_now_ms(now_ms)
        params: list[Any] = []
        filters = ["COALESCE(stale, 0) = 0"]
        filters.append("observed_at_ms <= ?")
        params.append(stamp)
        filters.append("observed_at_ms >= ?")
        params.append(self._lower_bound_ms(stamp, self.ttl_config.fx_ttl_ms))

        with self.store.conn() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM arb_fx_rates
                    WHERE {" AND ".join(filters)}
                    ORDER BY observed_at_ms DESC, id DESC
                    LIMIT 50
                    """,
                    params,
                ).fetchall()
            ]

        for row in rows:
            normalized_pair = _normalize_pair(row.get("pair"))
            rate = _positive_float(row.get("rate"))
            if rate is None:
                continue
            if normalized_pair == "USDTKRW":
                return {**row, "effective_rate": rate, "conversion": "USDT/KRW"}
            if normalized_pair == "KRWUSDT":
                return {**row, "effective_rate": 1.0 / rate, "conversion": "KRW/USDT_INVERTED"}
        return None

    def _fetch_prior_tick(self, *, market_id: int, before_observed_at_ms: int) -> dict[str, Any] | None:
        with self.store.conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM arb_market_ticks
                WHERE market_id = ?
                  AND COALESCE(stale, 0) = 0
                  AND observed_at_ms < ?
                ORDER BY observed_at_ms DESC, id DESC
                LIMIT 1
                """,
                (int(market_id), int(before_observed_at_ms)),
            ).fetchone()
            return _row_to_dict(row) if row else None

    def _fetch_latest_pool_snapshot(
        self,
        *,
        market_id: int,
        at_or_before_ms: int,
    ) -> dict[str, Any] | None:
        with self.store.conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM arb_pool_snapshots
                WHERE market_id = ?
                  AND observed_at_ms <= ?
                ORDER BY observed_at_ms DESC, id DESC
                LIMIT 1
                """,
                (int(market_id), int(at_or_before_ms)),
            ).fetchone()
            return _row_to_dict(row) if row else None

    def _fetch_prior_pool_snapshot(
        self,
        *,
        market_id: int,
        before_observed_at_ms: int,
    ) -> dict[str, Any] | None:
        with self.store.conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM arb_pool_snapshots
                WHERE market_id = ?
                  AND observed_at_ms < ?
                ORDER BY observed_at_ms DESC, id DESC
                LIMIT 1
                """,
                (int(market_id), int(before_observed_at_ms)),
            ).fetchone()
            return _row_to_dict(row) if row else None

    def _resolve_now_ms(self, value: int | None) -> int:
        return int(store_now_ms() if value is None else value)

    def _lower_bound_ms(self, now_ms: int, ttl_ms: int) -> int:
        bounds = [int(now_ms) - int(ttl_ms)]
        if self.lookback_ms is not None:
            bounds.append(int(now_ms) - int(self.lookback_ms))
        return max(bounds)

    def _fresh_enough(self, observed_at_ms: object, *, ttl_ms: int, now_ms: int) -> bool:
        observed = _optional_int(observed_at_ms)
        if observed is None:
            return False
        return observed + int(ttl_ms) >= int(now_ms)

    def _peg_reference_price(self, market_row: Mapping[str, Any]) -> float | None:
        payload = _loads_json_object(market_row.get("market_payload_json") or market_row.get("market_payload"))
        for key in ("peg_reference_price_usd", "peg_price_usd", "reference_price_usd"):
            reference = _positive_float(payload.get(key))
            if reference is not None:
                return reference
        symbol = str(market_row.get("asset_symbol") or "").strip().upper()
        if symbol in self.stable_peg_symbols:
            return self.peg_reference_price_usd
        return None

    def _dex_source_freshness(
        self,
        tick: Mapping[str, Any],
        *,
        pool_snapshot: Mapping[str, Any] | None,
        now_ms: int,
    ) -> dict[str, Any]:
        chain = str(tick.get("chain_code") or "")
        source = {
            "dex_tick": _freshness_record(
                "dex_tick",
                observed_at_ms=tick.get("observed_at_ms"),
                ttl_ms=self.ttl_config.dex_tick_ttl_ms,
                now_ms=now_ms,
                stale=bool(tick.get("stale") or False),
                details={
                    "market_id": tick.get("market_id"),
                    "source": tick.get("source"),
                },
            ),
            "rpc_freshness": self._rpc_freshness_for_chain(chain, now_ms=now_ms),
        }
        if pool_snapshot:
            source["pool_snapshot"] = _freshness_record(
                "pool_snapshot",
                observed_at_ms=pool_snapshot.get("observed_at_ms"),
                ttl_ms=self.ttl_config.pool_snapshot_ttl_ms,
                now_ms=now_ms,
                details={
                    "market_id": pool_snapshot.get("market_id"),
                    "source": pool_snapshot.get("source"),
                },
            )
        return source

    def _spread_source_freshness(self, candidate: SpreadCandidate, *, now_ms: int) -> dict[str, Any]:
        orderbook_key = "krw_orderbook" if candidate.price_unit == "KRW" else "cex_orderbook"
        orderbook_ttl_ms = (
            self.ttl_config.krw_orderbook_ttl_ms
            if candidate.price_unit == "KRW"
            else self.ttl_config.cex_orderbook_ttl_ms
        )
        out = self._dex_source_freshness(
            candidate.buy_tick,
            pool_snapshot=candidate.pool_snapshot,
            now_ms=now_ms,
        )
        out[orderbook_key] = _freshness_record(
            orderbook_key,
            observed_at_ms=candidate.sell_tick.get("observed_at_ms"),
            ttl_ms=orderbook_ttl_ms,
            now_ms=now_ms,
            stale=bool(candidate.sell_tick.get("stale") or False),
            details={
                "market_id": candidate.sell_tick.get("market_id"),
                "source": candidate.sell_tick.get("source"),
            },
        )
        if candidate.price_unit == "KRW":
            out["fx_rate"] = _freshness_record(
                "fx_rate",
                observed_at_ms=(candidate.fx_rate or {}).get("observed_at_ms"),
                ttl_ms=self.ttl_config.fx_ttl_ms,
                now_ms=now_ms,
                stale=bool((candidate.fx_rate or {}).get("stale") or False),
                details={
                    "source": (candidate.fx_rate or {}).get("source"),
                    "pair": (candidate.fx_rate or {}).get("pair"),
                },
            )
        return out

    def _cross_chain_source_freshness(
        self,
        candidate: CrossChainSpreadCandidate,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        return {
            "buy_dex_tick": _freshness_record(
                "buy_dex_tick",
                observed_at_ms=candidate.buy_tick.get("observed_at_ms"),
                ttl_ms=self.ttl_config.dex_tick_ttl_ms,
                now_ms=now_ms,
                details={"market_id": candidate.buy_tick.get("market_id")},
            ),
            "sell_dex_tick": _freshness_record(
                "sell_dex_tick",
                observed_at_ms=candidate.sell_tick.get("observed_at_ms"),
                ttl_ms=self.ttl_config.dex_tick_ttl_ms,
                now_ms=now_ms,
                details={"market_id": candidate.sell_tick.get("market_id")},
            ),
            "buy_rpc_freshness": self._rpc_freshness_for_chain(
                str(candidate.buy_market.get("chain_code") or ""),
                now_ms=now_ms,
            ),
            "sell_rpc_freshness": self._rpc_freshness_for_chain(
                str(candidate.sell_market.get("chain_code") or ""),
                now_ms=now_ms,
            ),
        }

    def _pool_divergence_source_freshness(
        self,
        candidate: PoolDivergenceCandidate,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        return {
            "buy_dex_tick": _freshness_record(
                "buy_dex_tick",
                observed_at_ms=candidate.buy_tick.get("observed_at_ms"),
                ttl_ms=self.ttl_config.dex_tick_ttl_ms,
                now_ms=now_ms,
                details={"market_id": candidate.buy_tick.get("market_id")},
            ),
            "sell_dex_tick": _freshness_record(
                "sell_dex_tick",
                observed_at_ms=candidate.sell_tick.get("observed_at_ms"),
                ttl_ms=self.ttl_config.dex_tick_ttl_ms,
                now_ms=now_ms,
                details={"market_id": candidate.sell_tick.get("market_id")},
            ),
            "rpc_freshness": self._rpc_freshness_for_chain(
                str(candidate.buy_market.get("chain_code") or ""),
                now_ms=now_ms,
            ),
        }

    def _rpc_freshness_blocks_chain(self, chain_code: str, now_ms: int) -> bool:
        status = str(self._rpc_freshness_for_chain(chain_code, now_ms=now_ms).get("status") or "")
        return status in {"stale", "degraded", "disabled"}

    def _rpc_freshness_for_chain(self, chain_code: str, *, now_ms: int) -> dict[str, Any]:
        chain = str(chain_code or "").strip()
        chain_norm = _normalize_scope(chain)
        stale_records: list[dict[str, Any]] = []
        fresh_records: list[dict[str, Any]] = []
        with self.store.conn() as conn:
            collect_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT
                        c.provider_key,
                        c.scope_key,
                        c.updated_at_ms,
                        h.status,
                        h.last_success_at_ms,
                        h.last_error_at_ms,
                        h.error_code,
                        h.payload_json
                    FROM arb_collect_state c
                    LEFT JOIN arb_provider_health h ON h.provider_key = c.provider_key
                    """
                ).fetchall()
            ]
            health_rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM arb_provider_health").fetchall()
            ]

        for row in collect_rows:
            if not _scope_matches_chain(row.get("scope_key"), chain_norm):
                continue
            if not _provider_or_payload_looks_rpc(row.get("provider_key"), row.get("payload_json")):
                continue
            record = _freshness_record(
                "rpc_freshness",
                observed_at_ms=row.get("updated_at_ms"),
                ttl_ms=self.ttl_config.rpc_freshness_ttl_ms,
                now_ms=now_ms,
                stale=str(row.get("status") or "").upper() not in {"", "OK", "ACTIVE"},
                details={
                    "provider_key": row.get("provider_key"),
                    "scope_key": row.get("scope_key"),
                    "provider_status": row.get("status"),
                    "error_code": row.get("error_code"),
                },
            )
            if record["status"] == "fresh":
                fresh_records.append(record)
            else:
                stale_records.append(record)

        for row in health_rows:
            payload = _loads_json_object(row.get("payload_json"))
            if not _provider_or_payload_looks_rpc(row.get("provider_key"), row.get("payload_json")):
                continue
            if not _scope_matches_chain(payload.get("scope_key"), chain_norm):
                continue
            status = str(row.get("status") or "").upper()
            if status in {"OK", "ACTIVE"}:
                continue
            stale_records.append(
                {
                    "source_key": "rpc_freshness",
                    "observed_at_ms": row.get("last_error_at_ms"),
                    "ttl_ms": self.ttl_config.rpc_freshness_ttl_ms,
                    "fresh_until_ms": row.get("last_error_at_ms"),
                    "status": "disabled" if status == "DISABLED" else "degraded",
                    "stale": True,
                    "age_ms": None,
                    "details": {
                        "provider_key": row.get("provider_key"),
                        "scope_key": payload.get("scope_key"),
                        "provider_status": row.get("status"),
                        "error_code": row.get("error_code"),
                    },
                }
            )

        if fresh_records or stale_records:
            return sorted(
                [*fresh_records, *stale_records],
                key=lambda item: int(item.get("observed_at_ms") or 0),
                reverse=True,
            )[0]
        return {
            "source_key": "rpc_freshness",
            "observed_at_ms": None,
            "ttl_ms": self.ttl_config.rpc_freshness_ttl_ms,
            "fresh_until_ms": None,
            "status": "missing",
            "stale": False,
            "age_ms": None,
            "details": {"chain_code": chain},
        }


def detect_dex_drawdowns(
    store: ArbitrageStore,
    *,
    drawdown_threshold_bps: float = DEFAULT_DRAWDOWN_THRESHOLD_BPS,
    lookback_ms: int | None = None,
    now_ms: int | None = None,
) -> DetectorRunResult:
    return ArbitrageDetector(
        store,
        drawdown_threshold_bps=drawdown_threshold_bps,
        lookback_ms=lookback_ms,
    ).detect_dex_drawdowns(now_ms=now_ms)


def detect_spreads(
    store: ArbitrageStore,
    *,
    spread_threshold_bps: float = DEFAULT_SPREAD_THRESHOLD_BPS,
    lookback_ms: int | None = None,
    now_ms: int | None = None,
) -> DetectorRunResult:
    return ArbitrageDetector(
        store,
        spread_threshold_bps=spread_threshold_bps,
        lookback_ms=lookback_ms,
    ).detect_spreads(now_ms=now_ms)


def detect_dex_cex_spreads(
    store: ArbitrageStore,
    *,
    spread_threshold_bps: float = DEFAULT_SPREAD_THRESHOLD_BPS,
    lookback_ms: int | None = None,
    now_ms: int | None = None,
) -> DetectorRunResult:
    return ArbitrageDetector(
        store,
        spread_threshold_bps=spread_threshold_bps,
        lookback_ms=lookback_ms,
    ).detect_dex_cex_spreads(now_ms=now_ms)


def detect_dex_krw_spreads(
    store: ArbitrageStore,
    *,
    spread_threshold_bps: float = DEFAULT_SPREAD_THRESHOLD_BPS,
    lookback_ms: int | None = None,
    now_ms: int | None = None,
) -> DetectorRunResult:
    return ArbitrageDetector(
        store,
        spread_threshold_bps=spread_threshold_bps,
        lookback_ms=lookback_ms,
    ).detect_dex_krw_spreads(now_ms=now_ms)


def detect_cross_chain_spreads(
    store: ArbitrageStore,
    *,
    spread_threshold_bps: float = DEFAULT_SPREAD_THRESHOLD_BPS,
    lookback_ms: int | None = None,
    now_ms: int | None = None,
) -> DetectorRunResult:
    return ArbitrageDetector(
        store,
        spread_threshold_bps=spread_threshold_bps,
        lookback_ms=lookback_ms,
    ).detect_cross_chain_spreads(now_ms=now_ms)


def detect_depegs(
    store: ArbitrageStore,
    *,
    depeg_threshold_bps: float = DEFAULT_DEPEG_THRESHOLD_BPS,
    lookback_ms: int | None = None,
    now_ms: int | None = None,
) -> DetectorRunResult:
    return ArbitrageDetector(
        store,
        depeg_threshold_bps=depeg_threshold_bps,
        lookback_ms=lookback_ms,
    ).detect_depegs(now_ms=now_ms)


def detect_price_spikes(
    store: ArbitrageStore,
    *,
    price_spike_threshold_bps: float = DEFAULT_PRICE_SPIKE_THRESHOLD_BPS,
    lookback_ms: int | None = None,
    now_ms: int | None = None,
) -> DetectorRunResult:
    return ArbitrageDetector(
        store,
        price_spike_threshold_bps=price_spike_threshold_bps,
        lookback_ms=lookback_ms,
    ).detect_price_spikes(now_ms=now_ms)


def detect_liquidity_collapses(
    store: ArbitrageStore,
    *,
    liquidity_collapse_threshold_bps: float = DEFAULT_LIQUIDITY_COLLAPSE_THRESHOLD_BPS,
    lookback_ms: int | None = None,
    now_ms: int | None = None,
) -> DetectorRunResult:
    return ArbitrageDetector(
        store,
        liquidity_collapse_threshold_bps=liquidity_collapse_threshold_bps,
        lookback_ms=lookback_ms,
    ).detect_liquidity_collapses(now_ms=now_ms)


def detect_pool_divergences(
    store: ArbitrageStore,
    *,
    pool_divergence_threshold_bps: float = DEFAULT_POOL_DIVERGENCE_THRESHOLD_BPS,
    lookback_ms: int | None = None,
    now_ms: int | None = None,
) -> DetectorRunResult:
    return ArbitrageDetector(
        store,
        pool_divergence_threshold_bps=pool_divergence_threshold_bps,
        lookback_ms=lookback_ms,
    ).detect_pool_divergences(now_ms=now_ms)


def _drawdown_opportunity_key(candidate: DexDrawdownCandidate) -> str:
    return (
        "dex_drawdown:"
        f"{candidate.market['market_id']}:"
        f"{candidate.baseline_tick['observed_at_ms']}:"
        f"{candidate.current_tick['observed_at_ms']}"
    )


def _price_spike_opportunity_key(candidate: DexPriceSpikeCandidate) -> str:
    return (
        "price_spike:"
        f"{candidate.market['market_id']}:"
        f"{candidate.baseline_tick['observed_at_ms']}:"
        f"{candidate.current_tick['observed_at_ms']}"
    )


def _depeg_opportunity_key(candidate: DepegCandidate) -> str:
    return (
        "depeg:"
        f"{candidate.identity.asset_id}:"
        f"{candidate.market['market_id']}:"
        f"{candidate.current_tick['observed_at_ms']}"
    )


def _liquidity_collapse_opportunity_key(candidate: LiquidityCollapseCandidate) -> str:
    return (
        "liquidity_collapse:"
        f"{candidate.market['market_id']}:"
        f"{candidate.baseline_pool_snapshot['observed_at_ms']}:"
        f"{candidate.current_pool_snapshot['observed_at_ms']}"
    )


def _drawdown_payload(
    candidate: DexDrawdownCandidate,
    identity: NormalizedIdentity,
    *,
    threshold_bps: float,
    source_freshness: Mapping[str, Any],
) -> dict[str, Any]:
    market_payload = _loads_json_object(candidate.market.get("market_payload_json"))
    pool_snapshot = candidate.pool_snapshot or {}
    return {
        "detector": "dex_drawdown",
        "anomaly_type": "dex_drawdown",
        "detection_reason": "dex_price_drawdown_from_prior_tick",
        "threshold_bps": threshold_bps,
        "drawdown_bps": candidate.drawdown_bps,
        "buy_venue": str(candidate.market.get("venue_code") or ""),
        "sell_venue": str(candidate.market.get("venue_code") or ""),
        "chain": str(candidate.market.get("chain_code") or ""),
        "token_ca": identity.contract_address,
        "pool_ca": str(candidate.market.get("pool_address") or ""),
        "cex_market": "",
        "deposit_network": "",
        "spread_bps": candidate.drawdown_bps,
        "edge_worst_bps": 0.0,
        "baseline_tick_id": int(candidate.baseline_tick["id"]),
        "baseline_observed_at_ms": int(candidate.baseline_tick["observed_at_ms"]),
        "baseline_price": candidate.baseline_price,
        "current_tick_id": int(candidate.current_tick["id"]),
        "current_observed_at_ms": int(candidate.current_tick["observed_at_ms"]),
        "current_price": candidate.current_price,
        "market": {
            "market_id": int(candidate.market["market_id"]),
            "market_key": str(candidate.market.get("market_key") or ""),
            "venue_code": str(candidate.market.get("venue_code") or ""),
            "chain_code": str(candidate.market.get("chain_code") or ""),
            "pool_address": str(candidate.market.get("pool_address") or ""),
            "market_symbol": str(candidate.market.get("market_symbol") or ""),
            "quote_asset": str(candidate.market.get("quote_asset") or ""),
            "payload": market_payload,
        },
        "pool_snapshot": {
            "id": pool_snapshot.get("id"),
            "observed_at_ms": pool_snapshot.get("observed_at_ms"),
            "liquidity_usd": pool_snapshot.get("liquidity_usd"),
            "block_number": pool_snapshot.get("block_number"),
        },
        "identity": identity.to_dict(),
        "source_freshness": dict(source_freshness),
        "candidate_only": True,
        "edge_worst_verified": False,
    }


def _price_spike_payload(
    candidate: DexPriceSpikeCandidate,
    identity: NormalizedIdentity,
    *,
    source_freshness: Mapping[str, Any],
) -> dict[str, Any]:
    pool_snapshot = candidate.pool_snapshot or {}
    return {
        "detector": "price_spike",
        "anomaly_type": "price_spike",
        "detection_reason": "dex_price_spike_upside",
        "buy_venue": str(candidate.market.get("venue_code") or ""),
        "sell_venue": str(candidate.market.get("venue_code") or ""),
        "chain": str(candidate.market.get("chain_code") or ""),
        "token_ca": identity.contract_address,
        "pool_ca": str(candidate.market.get("pool_address") or ""),
        "cex_market": "",
        "deposit_network": "",
        "spread_bps": candidate.spike_bps,
        "edge_worst_bps": 0.0,
        "baseline_tick_id": int(candidate.baseline_tick["id"]),
        "baseline_observed_at_ms": int(candidate.baseline_tick["observed_at_ms"]),
        "baseline_price": candidate.baseline_price,
        "current_tick_id": int(candidate.current_tick["id"]),
        "current_observed_at_ms": int(candidate.current_tick["observed_at_ms"]),
        "current_price": candidate.current_price,
        "spike_bps": candidate.spike_bps,
        "pool_snapshot": {
            "id": pool_snapshot.get("id"),
            "observed_at_ms": pool_snapshot.get("observed_at_ms"),
            "liquidity_usd": pool_snapshot.get("liquidity_usd"),
            "block_number": pool_snapshot.get("block_number"),
        },
        "identity": identity.to_dict(),
        "source_freshness": dict(source_freshness),
        "candidate_only": True,
        "edge_worst_verified": False,
    }


def _depeg_payload(candidate: DepegCandidate, *, source_freshness: Mapping[str, Any]) -> dict[str, Any]:
    pool_snapshot = candidate.pool_snapshot or {}
    return {
        "detector": "depeg",
        "anomaly_type": "depeg",
        "detection_reason": f"stable_{candidate.direction}",
        "buy_venue": str(candidate.market.get("venue_code") or ""),
        "sell_venue": str(candidate.market.get("venue_code") or ""),
        "chain": str(candidate.market.get("chain_code") or ""),
        "token_ca": candidate.identity.contract_address,
        "pool_ca": str(candidate.market.get("pool_address") or ""),
        "cex_market": "",
        "deposit_network": "",
        "spread_bps": candidate.deviation_bps,
        "edge_worst_bps": 0.0,
        "current_tick_id": int(candidate.current_tick["id"]),
        "current_observed_at_ms": int(candidate.current_tick["observed_at_ms"]),
        "current_price": candidate.price,
        "reference_price": candidate.reference_price,
        "deviation_bps": candidate.deviation_bps,
        "direction": candidate.direction,
        "pool_snapshot": {
            "id": pool_snapshot.get("id"),
            "observed_at_ms": pool_snapshot.get("observed_at_ms"),
            "liquidity_usd": pool_snapshot.get("liquidity_usd"),
            "block_number": pool_snapshot.get("block_number"),
        },
        "identity": candidate.identity.to_dict(),
        "source_freshness": dict(source_freshness),
        "candidate_only": True,
        "edge_worst_verified": False,
    }


def _liquidity_collapse_payload(
    candidate: LiquidityCollapseCandidate,
    *,
    source_freshness: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "detector": "liquidity_collapse",
        "anomaly_type": "liquidity_collapse",
        "detection_reason": "dex_pool_liquidity_collapse",
        "buy_venue": str(candidate.market.get("venue_code") or ""),
        "sell_venue": str(candidate.market.get("venue_code") or ""),
        "chain": str(candidate.market.get("chain_code") or ""),
        "token_ca": candidate.identity.contract_address,
        "pool_ca": str(candidate.market.get("pool_address") or ""),
        "cex_market": "",
        "deposit_network": "",
        "spread_bps": candidate.collapse_bps,
        "edge_worst_bps": 0.0,
        "current_tick_id": int(candidate.current_tick["id"]),
        "current_observed_at_ms": int(candidate.current_tick["observed_at_ms"]),
        "baseline_pool_snapshot_id": int(candidate.baseline_pool_snapshot["id"]),
        "current_pool_snapshot_id": int(candidate.current_pool_snapshot["id"]),
        "baseline_liquidity": candidate.baseline_liquidity,
        "current_liquidity": candidate.current_liquidity,
        "collapse_bps": candidate.collapse_bps,
        "reserve_collapse_evidence": {
            "baseline": _pool_reserve_payload(candidate.baseline_pool_snapshot),
            "current": _pool_reserve_payload(candidate.current_pool_snapshot),
        },
        "depth_collapse_evidence": {
            "source": "pool_reserves",
            "baseline_liquidity": candidate.baseline_liquidity,
            "current_liquidity": candidate.current_liquidity,
        },
        "identity": candidate.identity.to_dict(),
        "source_freshness": dict(source_freshness),
        "candidate_only": True,
        "edge_worst_verified": False,
    }


def _spread_opportunity_key(candidate: SpreadCandidate) -> str:
    return (
        f"{candidate.anomaly_type}:"
        f"{candidate.dex_identity.asset_id}:"
        f"{candidate.buy_market['market_id']}:"
        f"{candidate.sell_market['market_id']}"
    )


def _spread_payload(candidate: SpreadCandidate, *, source_freshness: Mapping[str, Any]) -> dict[str, Any]:
    pool_snapshot = candidate.pool_snapshot or {}
    route_warnings = _spread_route_warnings(candidate)
    return {
        "detector": candidate.anomaly_type,
        "anomaly_type": candidate.anomaly_type,
        "detection_reason": f"{candidate.anomaly_type}_positive_spread",
        "buy_venue": str(candidate.buy_market.get("venue_code") or ""),
        "sell_venue": str(candidate.sell_market.get("venue_code") or ""),
        "chain": str(candidate.buy_market.get("chain_code") or ""),
        "token_ca": candidate.dex_identity.contract_address,
        "pool_ca": str(candidate.buy_market.get("pool_address") or ""),
        "cex_market": str(candidate.sell_market.get("market_symbol") or ""),
        "deposit_network": str(candidate.sell_market.get("deposit_network") or ""),
        "spread_bps": candidate.spread_bps,
        "edge_worst_bps": 0.0,
        "status": "DETECTED",
        "selected_route": {
            "route_type": candidate.route_type,
            "route_status": "WAIT",
            "safety_status": "WARN",
            "warning_reasons": route_warnings,
            "edge_worst_verified": False,
        },
        "buy": {
            "market_id": int(candidate.buy_market["market_id"]),
            "market_key": str(candidate.buy_market.get("market_key") or ""),
            "venue_code": str(candidate.buy_market.get("venue_code") or ""),
            "chain_code": str(candidate.buy_market.get("chain_code") or ""),
            "pool_address": str(candidate.buy_market.get("pool_address") or ""),
            "market_symbol": str(candidate.buy_market.get("market_symbol") or ""),
            "quote_asset": str(candidate.buy_market.get("quote_asset") or ""),
            "price": candidate.buy_price,
            "price_unit": candidate.price_unit,
            "price_source": candidate.buy_price_source,
            "tick_id": int(candidate.buy_tick["id"]),
        },
        "sell": {
            "market_id": int(candidate.sell_market["market_id"]),
            "market_key": str(candidate.sell_market.get("market_key") or ""),
            "venue_code": str(candidate.sell_market.get("venue_code") or ""),
            "market_symbol": str(candidate.sell_market.get("market_symbol") or ""),
            "quote_asset": str(candidate.sell_market.get("quote_asset") or ""),
            "deposit_network": str(candidate.sell_market.get("deposit_network") or ""),
            "price": candidate.sell_price,
            "price_unit": candidate.price_unit,
            "price_source": candidate.sell_price_source,
            "tick_id": int(candidate.sell_tick["id"]),
        },
        "pool_snapshot": {
            "id": pool_snapshot.get("id"),
            "observed_at_ms": pool_snapshot.get("observed_at_ms"),
            "liquidity_usd": pool_snapshot.get("liquidity_usd"),
            "block_number": pool_snapshot.get("block_number"),
        },
        "fx_rate": _fx_rate_payload(candidate.fx_rate),
        "identity": {
            "buy": candidate.dex_identity.to_dict(),
            "sell": candidate.cex_identity.to_dict(),
        },
        "source_freshness": dict(source_freshness),
        "candidate_only": True,
        "edge_worst_verified": False,
    }


def _spread_route_warnings(candidate: SpreadCandidate) -> list[str]:
    warnings = ["candidate_only", "edge_worst_unverified"]
    deposit_network = str(candidate.sell_market.get("deposit_network") or "").strip().upper()
    buy_chain = str(candidate.buy_market.get("chain_code") or "").strip().upper()
    if not deposit_network:
        warnings.append("unknown_cex_deposit_network")
    elif buy_chain and deposit_network != buy_chain:
        warnings.append("cex_deposit_network_differs_from_buy_chain")
    return warnings


def _cross_chain_opportunity_key(candidate: CrossChainSpreadCandidate) -> str:
    return (
        "cross_chain_spread:"
        f"{candidate.buy_identity.asset_id}:"
        f"{candidate.bridge_group}:"
        f"{candidate.buy_market['market_id']}:"
        f"{candidate.sell_market['market_id']}"
    )


def _pool_divergence_opportunity_key(candidate: PoolDivergenceCandidate) -> str:
    return (
        "pool_divergence:"
        f"{candidate.buy_identity.asset_id}:"
        f"{candidate.buy_market['market_id']}:"
        f"{candidate.sell_market['market_id']}"
    )


def _pool_divergence_payload(
    candidate: PoolDivergenceCandidate,
    *,
    source_freshness: Mapping[str, Any],
) -> dict[str, Any]:
    buy_pool = candidate.buy_pool_snapshot or {}
    sell_pool = candidate.sell_pool_snapshot or {}
    route_warnings = ["candidate_only", "edge_worst_unverified"]
    return {
        "detector": "pool_divergence",
        "anomaly_type": "pool_divergence",
        "detection_reason": "same_asset_pool_price_divergence",
        "buy_venue": str(candidate.buy_market.get("venue_code") or ""),
        "sell_venue": str(candidate.sell_market.get("venue_code") or ""),
        "chain": str(candidate.buy_market.get("chain_code") or ""),
        "buy_chain": str(candidate.buy_market.get("chain_code") or ""),
        "sell_chain": str(candidate.sell_market.get("chain_code") or ""),
        "token_ca": candidate.buy_identity.contract_address,
        "sell_token_ca": candidate.sell_identity.contract_address,
        "pool_ca": str(candidate.buy_market.get("pool_address") or ""),
        "sell_pool_ca": str(candidate.sell_market.get("pool_address") or ""),
        "cex_market": "",
        "deposit_network": "",
        "spread_bps": candidate.spread_bps,
        "edge_worst_bps": 0.0,
        "status": "DETECTED",
        "selected_route": {
            "route_type": "same_dex_sell",
            "route_status": "WAIT",
            "safety_status": "WARN",
            "warning_reasons": route_warnings,
            "edge_worst_verified": False,
        },
        "buy": {
            "market_id": int(candidate.buy_market["market_id"]),
            "market_key": str(candidate.buy_market.get("market_key") or ""),
            "venue_code": str(candidate.buy_market.get("venue_code") or ""),
            "chain_code": str(candidate.buy_market.get("chain_code") or ""),
            "pool_address": str(candidate.buy_market.get("pool_address") or ""),
            "market_symbol": str(candidate.buy_market.get("market_symbol") or ""),
            "quote_asset": str(candidate.buy_market.get("quote_asset") or ""),
            "price": candidate.buy_price,
            "price_unit": "USD",
            "price_source": "tick_price_usd",
            "tick_id": int(candidate.buy_tick["id"]),
            "token_ca": candidate.buy_identity.contract_address,
        },
        "sell": {
            "market_id": int(candidate.sell_market["market_id"]),
            "market_key": str(candidate.sell_market.get("market_key") or ""),
            "venue_code": str(candidate.sell_market.get("venue_code") or ""),
            "chain_code": str(candidate.sell_market.get("chain_code") or ""),
            "pool_address": str(candidate.sell_market.get("pool_address") or ""),
            "market_symbol": str(candidate.sell_market.get("market_symbol") or ""),
            "quote_asset": str(candidate.sell_market.get("quote_asset") or ""),
            "price": candidate.sell_price,
            "price_unit": "USD",
            "price_source": "tick_price_usd",
            "tick_id": int(candidate.sell_tick["id"]),
            "token_ca": candidate.sell_identity.contract_address,
        },
        "pool_snapshot": {
            "buy": {
                "id": buy_pool.get("id"),
                "observed_at_ms": buy_pool.get("observed_at_ms"),
                "liquidity_usd": buy_pool.get("liquidity_usd"),
                "block_number": buy_pool.get("block_number"),
            },
            "sell": {
                "id": sell_pool.get("id"),
                "observed_at_ms": sell_pool.get("observed_at_ms"),
                "liquidity_usd": sell_pool.get("liquidity_usd"),
                "block_number": sell_pool.get("block_number"),
            },
        },
        "identity": {
            "buy": candidate.buy_identity.to_dict(),
            "sell": candidate.sell_identity.to_dict(),
            "verification": {
                "reason": "matching_chain_and_contract",
                "symbol_only": False,
            },
        },
        "source_freshness": dict(source_freshness),
        "candidate_only": True,
        "edge_worst_verified": False,
    }


def _cross_chain_payload(
    candidate: CrossChainSpreadCandidate,
    *,
    source_freshness: Mapping[str, Any],
) -> dict[str, Any]:
    buy_pool = candidate.buy_pool_snapshot or {}
    sell_pool = candidate.sell_pool_snapshot or {}
    route_warnings = _cross_chain_route_warnings()
    return {
        "detector": "cross_chain_spread",
        "anomaly_type": "cross_chain_spread",
        "detection_reason": "cross_chain_price_spread",
        "buy_venue": str(candidate.buy_market.get("venue_code") or ""),
        "sell_venue": str(candidate.sell_market.get("venue_code") or ""),
        "chain": str(candidate.buy_market.get("chain_code") or ""),
        "buy_chain": str(candidate.buy_market.get("chain_code") or ""),
        "sell_chain": str(candidate.sell_market.get("chain_code") or ""),
        "token_ca": candidate.buy_identity.contract_address,
        "sell_token_ca": candidate.sell_identity.contract_address,
        "pool_ca": str(candidate.buy_market.get("pool_address") or ""),
        "sell_pool_ca": str(candidate.sell_market.get("pool_address") or ""),
        "cex_market": "",
        "deposit_network": "",
        "bridge_group": candidate.bridge_group,
        "spread_bps": candidate.spread_bps,
        "edge_worst_bps": 0.0,
        "status": "DETECTED",
        "selected_route": {
            "route_type": "bridge_dex_sell",
            "route_status": "WAIT",
            "safety_status": "WARN",
            "warning_reasons": route_warnings,
            "edge_worst_verified": False,
            "bridge_quote_evaluated": False,
        },
        "buy": {
            "market_id": int(candidate.buy_market["market_id"]),
            "market_key": str(candidate.buy_market.get("market_key") or ""),
            "venue_code": str(candidate.buy_market.get("venue_code") or ""),
            "chain_code": str(candidate.buy_market.get("chain_code") or ""),
            "pool_address": str(candidate.buy_market.get("pool_address") or ""),
            "market_symbol": str(candidate.buy_market.get("market_symbol") or ""),
            "quote_asset": str(candidate.buy_market.get("quote_asset") or ""),
            "price": candidate.buy_price,
            "price_unit": "USD",
            "price_source": "tick_price_usd",
            "tick_id": int(candidate.buy_tick["id"]),
            "token_ca": candidate.buy_identity.contract_address,
        },
        "sell": {
            "market_id": int(candidate.sell_market["market_id"]),
            "market_key": str(candidate.sell_market.get("market_key") or ""),
            "venue_code": str(candidate.sell_market.get("venue_code") or ""),
            "chain_code": str(candidate.sell_market.get("chain_code") or ""),
            "pool_address": str(candidate.sell_market.get("pool_address") or ""),
            "market_symbol": str(candidate.sell_market.get("market_symbol") or ""),
            "quote_asset": str(candidate.sell_market.get("quote_asset") or ""),
            "price": candidate.sell_price,
            "price_unit": "USD",
            "price_source": "tick_price_usd",
            "tick_id": int(candidate.sell_tick["id"]),
            "token_ca": candidate.sell_identity.contract_address,
        },
        "pool_snapshot": {
            "buy": {
                "id": buy_pool.get("id"),
                "observed_at_ms": buy_pool.get("observed_at_ms"),
                "liquidity_usd": buy_pool.get("liquidity_usd"),
                "block_number": buy_pool.get("block_number"),
            },
            "sell": {
                "id": sell_pool.get("id"),
                "observed_at_ms": sell_pool.get("observed_at_ms"),
                "liquidity_usd": sell_pool.get("liquidity_usd"),
                "block_number": sell_pool.get("block_number"),
            },
        },
        "identity": {
            "buy": candidate.buy_identity.to_dict(),
            "sell": candidate.sell_identity.to_dict(),
            "verification": dict(candidate.verification_evidence),
        },
        "source_freshness": dict(source_freshness),
        "candidate_only": True,
        "edge_worst_verified": False,
        "bridge_quote_evaluated": False,
    }


def _cross_chain_route_warnings() -> list[str]:
    return ["candidate_only", "edge_worst_unverified", "bridge_quote_not_evaluated"]


def _cross_chain_verification(
    left: NormalizedIdentity,
    right: NormalizedIdentity,
) -> tuple[str, dict[str, Any]]:
    evidence = {
        "left": {
            "token_id": left.token_id,
            "chain_id": left.chain_id,
            "chain_code": left.chain_code,
            "contract_address": left.contract_address,
            "bridge_group": left.bridge_group,
        },
        "right": {
            "token_id": right.token_id,
            "chain_id": right.chain_id,
            "chain_code": right.chain_code,
            "contract_address": right.contract_address,
            "bridge_group": right.bridge_group,
        },
        "verified": False,
        "reason": "missing_bridge_group",
    }
    left_bridge = str(left.bridge_group or "").strip()
    right_bridge = str(right.bridge_group or "").strip()
    if left_bridge and left_bridge == right_bridge:
        return left_bridge, {**evidence, "verified": True, "reason": "matching_bridge_group"}

    left_contract = str(left.contract_address or "").strip().lower()
    right_contract = str(right.contract_address or "").strip().lower()
    if left_contract and left_contract == right_contract:
        contract_group = f"contract:{left_contract}"
        return contract_group, {**evidence, "verified": True, "reason": "matching_contract_address"}

    return "", evidence


def _identity_chain_key(identity: NormalizedIdentity) -> str:
    return str(identity.chain_id or identity.chain_code or "").strip().upper()


def _fx_rate_payload(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row.get("id"),
        "pair": row.get("pair"),
        "source": row.get("source"),
        "observed_at_ms": row.get("observed_at_ms"),
        "rate": row.get("rate"),
        "effective_rate": row.get("effective_rate"),
        "conversion": row.get("conversion"),
    }


def _identity_hints(candidate: DexDrawdownCandidate) -> dict[str, str]:
    return _dex_identity_hints(
        market_row=candidate.market,
        current_tick=candidate.current_tick,
        pool_snapshot=candidate.pool_snapshot,
    )


def _dex_identity_hints(
    *,
    market_row: Mapping[str, Any],
    current_tick: Mapping[str, Any],
    pool_snapshot: Mapping[str, Any] | None,
) -> dict[str, str]:
    market_payload = _loads_json_object(market_row.get("market_payload_json") or market_row.get("market_payload"))
    tick_payload = _loads_json_object(current_tick.get("payload_json") or current_tick.get("payload"))
    pool_payload = _loads_json_object((pool_snapshot or {}).get("payload_json") or (pool_snapshot or {}).get("payload"))
    chain_id = _first_text(
        market_payload.get("chain_id"),
        tick_payload.get("chain_id"),
        tick_payload.get("chainId"),
        pool_payload.get("chain_id"),
        pool_payload.get("chainId"),
    )
    token_contract = _first_text(
        market_payload.get("token_contract_address"),
        market_payload.get("base_token_address"),
        market_payload.get("contract_address"),
        _nested_text(tick_payload, ("baseToken", "address")),
        _nested_text(tick_payload, ("baseToken", "contract_address")),
        tick_payload.get("base_token_address"),
        tick_payload.get("token_contract_address"),
        _nested_text(pool_payload, ("baseToken", "address")),
        _nested_text(pool_payload, ("baseToken", "contract_address")),
        pool_payload.get("base_token_address"),
        pool_payload.get("token_contract_address"),
        _resource_address_from_relationship(tick_payload, "base_token"),
        _resource_address_from_relationship(pool_payload, "base_token"),
    )
    return {
        "chain_id": chain_id,
        "token_contract_address": token_contract.lower(),
    }


def _dex_buy_price(
    row: Mapping[str, Any],
    *,
    price_unit: str,
    fx_rate: Mapping[str, Any] | None,
) -> tuple[float | None, str]:
    if price_unit == "KRW":
        usd_price = _positive_float(row.get("price_usd"))
        fx = _positive_float((fx_rate or {}).get("effective_rate"))
        if usd_price is not None and fx is not None:
            return usd_price * fx, "usdt_krw_implied"
        krw_price = _positive_float(row.get("price_krw"))
        if krw_price is not None:
            return krw_price, "tick_price_krw"
        return None, ""

    usd_price = _positive_float(row.get("price_usd"))
    if usd_price is not None:
        return usd_price, "tick_price_usd"
    raw_price = _positive_float(row.get("raw_price"))
    if raw_price is not None:
        return raw_price, "tick_raw_price"
    return None, ""


def _cex_sell_price(row: Mapping[str, Any], *, price_unit: str) -> tuple[float | None, str]:
    bid = _positive_float(row.get("best_bid"))
    if bid is not None:
        return bid, "best_bid"

    if price_unit == "KRW":
        price = _positive_float(row.get("price_krw")) or _positive_float(row.get("raw_price"))
        return (price, "midpoint") if price is not None else (None, "")

    price = _positive_float(row.get("price_usd")) or _positive_float(row.get("raw_price"))
    return (price, "midpoint") if price is not None else (None, "")


def _tick_price(row: Mapping[str, Any]) -> float | None:
    for key in ("price_usd", "price_krw", "raw_price", "best_ask", "best_bid"):
        value = row.get(key)
        if value is None:
            continue
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def _optional_int(value: object) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: object, fallback: int) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed > 0 else int(fallback)


def _positive_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _freshness_record(
    source_key: str,
    *,
    observed_at_ms: object,
    ttl_ms: int,
    now_ms: int,
    stale: bool = False,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    observed = _optional_int(observed_at_ms)
    ttl = int(ttl_ms)
    if observed is None:
        return {
            "source_key": source_key,
            "observed_at_ms": None,
            "ttl_ms": ttl,
            "fresh_until_ms": None,
            "status": "missing",
            "stale": bool(stale),
            "age_ms": None,
            "details": dict(details or {}),
        }
    fresh_until = observed + ttl
    is_stale = bool(stale) or fresh_until < int(now_ms)
    return {
        "source_key": source_key,
        "observed_at_ms": observed,
        "ttl_ms": ttl,
        "fresh_until_ms": fresh_until,
        "status": "stale" if is_stale else "fresh",
        "stale": is_stale,
        "age_ms": int(now_ms) - observed,
        "details": dict(details or {}),
    }


def _pool_liquidity_value(row: Mapping[str, Any]) -> float | None:
    liquidity = _positive_float(row.get("liquidity_usd"))
    if liquidity is not None:
        return liquidity
    reserve0 = _positive_float(row.get("reserve0_raw"))
    reserve1 = _positive_float(row.get("reserve1_raw"))
    reserves = [value for value in (reserve0, reserve1) if value is not None]
    return min(reserves) if reserves else None


def _pool_reserve_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "observed_at_ms": row.get("observed_at_ms"),
        "reserve0_raw": row.get("reserve0_raw"),
        "reserve1_raw": row.get("reserve1_raw"),
        "liquidity_usd": row.get("liquidity_usd"),
        "block_number": row.get("block_number"),
    }


def _same_contract_identity(left: NormalizedIdentity, right: NormalizedIdentity) -> bool:
    left_contract = str(left.contract_address or "").strip().lower()
    right_contract = str(right.contract_address or "").strip().lower()
    return bool(left_contract and left_contract == right_contract)


def _normalize_pair(value: object) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _normalize_scope(value: object) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _scope_matches_chain(scope_key: object, chain_norm: str) -> bool:
    scope_norm = _normalize_scope(scope_key)
    if not chain_norm or not scope_norm:
        return False
    return chain_norm == scope_norm or chain_norm in scope_norm or scope_norm in chain_norm


def _provider_or_payload_looks_rpc(provider_key: object, payload_json: object) -> bool:
    provider = str(provider_key or "").lower()
    if any(part in provider for part in ("rpc", "alchemy", "etherscan")):
        return True
    payload = _loads_json_object(payload_json)
    capability = str(payload.get("capability") or "").lower()
    scope_key = str(payload.get("scope_key") or "").lower()
    return "rpc" in capability or "rpc" in scope_key


def _combine_results(*results: DetectorRunResult) -> DetectorRunResult:
    return DetectorRunResult(
        opportunities_upserted=sum(result.opportunities_upserted for result in results),
        routes_upserted=sum(result.routes_upserted for result in results),
        blocked_identities=sum(result.blocked_identities for result in results),
        skipped=sum(result.skipped for result in results),
    )


def _row_to_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("payload_json", "market_payload_json"):
        if key in out:
            out[key.removesuffix("_json")] = _loads_json_object(out.get(key))
    return out


def _loads_json_object(raw: object) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        loaded = json.loads(str(raw))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _nested_text(payload: Mapping[str, Any], path: tuple[str, ...]) -> str:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(key)
    return str(current or "").strip()


def _resource_address_from_relationship(payload: Mapping[str, Any], relationship_name: str) -> str:
    relationships = payload.get("relationships")
    if not isinstance(relationships, Mapping):
        return ""
    relation = relationships.get(relationship_name)
    if not isinstance(relation, Mapping):
        return ""
    data = relation.get("data")
    if not isinstance(data, Mapping):
        return ""
    resource_id = str(data.get("id") or "")
    if "_" not in resource_id:
        return ""
    return resource_id.rsplit("_", 1)[-1].strip()
