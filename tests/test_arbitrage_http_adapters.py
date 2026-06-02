from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arbitrage.collectors.base import ProviderPayloadError
from arbitrage.live_collectors import LiveProviderJobRunner
from arbitrage.providers.base import READ_ONLY_HTTP_V1_CAPABILITIES, ProviderSpec
from arbitrage.providers.http_adapters import ProviderHttpTimeout, ReadOnlyHttpAdapterCatalog
from arbitrage.providers.registry import ProviderRegistry
from arbitrage.providers.secrets import EnvSecretResolver
from arbitrage.store import ArbitrageStore


NOW_MS = 1_779_539_700_000
RAW_SECRET = "redaction_fixture_secret_http_adapter_123"
TOKEN_CA = "0x1111111111111111111111111111111111111111"
POOL_CA = "0x2222222222222222222222222222222222222222"


class FakeHttpClient:
    def __init__(self, responses: list[Any] | None = None, exc: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.exc = exc
        self.requests: list[dict[str, Any]] = []

    def get_json(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout_s: float = 5.0) -> Any:
        self.requests.append({"method": "GET", "url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if self.exc:
            raise self.exc
        return self.responses.pop(0)

    def post_json(self, url: str, *, json_body: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout_s: float = 5.0) -> Any:
        self.requests.append({"method": "POST", "url": url, "json_body": dict(json_body or {}), "headers": dict(headers or {})})
        if self.exc:
            raise self.exc
        return self.responses.pop(0)


def _spec(
    provider_key: str,
    *,
    kind: str,
    capabilities: tuple[str, ...],
    priority: int = 10,
    auth_type: str = "public",
    required_env: tuple[str, ...] = (),
) -> ProviderSpec:
    return ProviderSpec(
        provider_key=provider_key,
        kind=kind,
        capabilities=capabilities,
        auth_type=auth_type,
        required_env=required_env,
        priority=priority,
        enabled_by_default=True,
    )


def _registry(environ: dict[str, str]) -> ProviderRegistry:
    return ProviderRegistry(
        (
            _spec("dexscreener", kind="dex", capabilities=("dex_pool",)),
            _spec("binance_public", kind="cex", capabilities=("cex_orderbook",)),
            _spec("upbit_public", kind="cex", capabilities=("krw_orderbook", "fx_rate")),
            _spec(
                "alchemy",
                kind="rpc",
                capabilities=("rpc_freshness",),
                auth_type="api_key",
                required_env=("ALCHEMY_API_KEY",),
            ),
        ),
        secret_resolver=EnvSecretResolver(environ),
    )


def _catalog(fake: FakeHttpClient, environ: dict[str, str]) -> ReadOnlyHttpAdapterCatalog:
    return ReadOnlyHttpAdapterCatalog(
        registry=_registry(environ),
        http_client=fake,
        environ=environ,
        timeout_s=0.25,
    )


def _dex_payload() -> dict[str, Any]:
    return {
        "pairs": [
            {
                "chainId": "polygon",
                "dexId": "quickswap",
                "pairAddress": POOL_CA,
                "baseToken": {"address": TOKEN_CA, "symbol": "SOL", "name": "Solana"},
                "quoteToken": {"address": "0x3333333333333333333333333333333333333333", "symbol": "USDC", "name": "USD Coin"},
                "priceNative": "70.0",
                "priceUsd": "70.0",
                "liquidity": {"usd": "1000000", "base": "1000", "quote": "70000"},
                "observed_at_ms": NOW_MS,
            }
        ],
    }


def _store(tmp_path: Path) -> ArbitrageStore:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    return store


def test_read_only_http_catalog_fetches_v1_capabilities_with_injected_client() -> None:
    fake = FakeHttpClient(
        [
            _dex_payload(),
            {"lastUpdateId": 123, "bids": [["70.10", "2"]], "asks": [["70.20", "3"]]},
            [{"market": "KRW-SOL", "timestamp": NOW_MS, "orderbook_units": [{"bid_price": 115000, "bid_size": 1, "ask_price": 115100, "ask_size": 1}]}],
            [{"market": "KRW-USDT", "timestamp": NOW_MS, "orderbook_units": [{"bid_price": 1390, "bid_size": 1, "ask_price": 1391, "ask_size": 1}]}],
            {"jsonrpc": "2.0", "id": 1, "result": "0xbc614e"},
        ]
    )
    catalog = _catalog(fake, {"ALCHEMY_API_KEY": RAW_SECRET})

    assert set(READ_ONLY_HTTP_V1_CAPABILITIES) == {"dex_pool", "cex_orderbook", "krw_orderbook", "fx_rate", "rpc_freshness"}
    assert catalog.fetch_payload({"provider_key": "dexscreener", "capability": "dex_pool", "chain_id": "polygon", "pool_address": POOL_CA})["pairs"]
    assert catalog.fetch_payload({"provider_key": "binance_public", "capability": "cex_orderbook", "symbol": "SOLUSDT", "observed_at_ms": NOW_MS})["symbol"] == "SOLUSDT"
    assert "orderbooks" in catalog.fetch_payload({"provider_key": "upbit_public", "capability": "krw_orderbook", "market": "KRW-SOL"})
    fx_payload = catalog.fetch_payload({"provider_key": "upbit_public", "capability": "fx_rate"})
    rpc_payload = catalog.fetch_payload({"provider_key": "alchemy", "capability": "rpc_freshness", "network": "polygon-mainnet"})

    assert fx_payload["rate"] == pytest.approx(1390.5)
    assert rpc_payload["result"] == "0xbc614e"
    assert RAW_SECRET not in repr((fx_payload, rpc_payload))
    assert [request["method"] for request in fake.requests] == ["GET", "GET", "GET", "GET", "POST"]
    assert not any(fragment in request["url"].lower() for request in fake.requests for fragment in ("/orders", "/withdraw", "/wallet", "/swap", "/bridge", "/sign"))


def test_missing_api_key_disables_only_affected_http_provider() -> None:
    fake = FakeHttpClient([_dex_payload()])
    catalog = _catalog(fake, {})

    dex_status = next(status for status in catalog.adapter_statuses("dex_pool") if status.provider_key == "dexscreener")
    rpc_status = next(status for status in catalog.adapter_statuses("rpc_freshness") if status.provider_key == "alchemy")

    assert dex_status.enabled is True
    assert rpc_status.enabled is False
    assert rpc_status.reason == "missing_env:ALCHEMY_API_KEY"
    assert rpc_status.diagnostics == ("missing_env:ALCHEMY_API_KEY",)
    assert catalog.adapters_for("dex_pool")[0].provider_key == "dexscreener"
    assert catalog.adapters_for("rpc_freshness") == ()
    assert RAW_SECRET not in repr(rpc_status)


def test_invalid_http_provider_payload_is_redacted() -> None:
    fake = FakeHttpClient([{"api_key": RAW_SECRET, "pairs": []}])
    catalog = _catalog(fake, {})

    with pytest.raises(ProviderPayloadError) as exc_info:
        catalog.fetch_payload({"provider_key": "dexscreener", "capability": "dex_pool", "chain_id": "polygon", "pool_address": POOL_CA})

    rendered = repr(exc_info.value.to_deadletter_payload())
    assert exc_info.value.error_code == "invalid_dex_pool_payload"
    assert RAW_SECRET not in rendered
    assert "<redacted>" in rendered


def test_http_provider_timeout_uses_redacted_diagnostics() -> None:
    fake = FakeHttpClient(exc=TimeoutError(f"timeout while calling {RAW_SECRET}"))
    catalog = _catalog(fake, {"ALCHEMY_API_KEY": RAW_SECRET})

    with pytest.raises(ProviderHttpTimeout) as exc_info:
        catalog.fetch_payload({"provider_key": "alchemy", "capability": "rpc_freshness", "network": "eth-mainnet"})

    assert RAW_SECRET not in str(exc_info.value)
    assert "<redacted>" in str(exc_info.value)


def test_live_provider_runner_can_ingest_http_adapter_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fake = FakeHttpClient([_dex_payload()])
    runner = LiveProviderJobRunner(store, http_adapters=_catalog(fake, {}))

    [result] = runner.run_once(
        [{"provider_key": "dexscreener", "capability": "dex_pool", "scope_key": "polygon:sol-usdc", "chain_id": "polygon", "pool_address": POOL_CA}],
        now_ms=NOW_MS,
    )

    with store.conn() as conn:
        tick_count = conn.execute("SELECT COUNT(*) AS n FROM arb_market_ticks").fetchone()["n"]
        order_count = conn.execute("SELECT COUNT(*) AS n FROM arb_orders").fetchone()["n"]

    assert result.status == "OK"
    assert tick_count == 1
    assert order_count == 0
