from __future__ import annotations

from pathlib import Path
from typing import Any

from arbitrage.provider_scheduler import ReadOnlyPollingScheduler
from arbitrage.providers.base import ProviderSpec
from arbitrage.providers.registry import ProviderRegistry
from arbitrage.store import ArbitrageStore


NOW_MS = 1_779_539_700_000


def _store(tmp_path: Path) -> ArbitrageStore:
    store = ArbitrageStore(str(tmp_path / "arbitrage.db"))
    store.init()
    return store


def _spec(
    provider_key: str,
    *,
    capabilities: tuple[str, ...] = ("cex_orderbook",),
    priority: int = 10,
    enabled_by_default: bool = True,
) -> ProviderSpec:
    return ProviderSpec(
        provider_key=provider_key,
        kind="cex",
        capabilities=capabilities,
        auth_type="public",
        required_env=(),
        priority=priority,
        enabled_by_default=enabled_by_default,
    )


def _registry(*specs: ProviderSpec) -> ProviderRegistry:
    return ProviderRegistry(specs)


def _orderbook_payload(*, observed_at_ms: int = NOW_MS, stale: bool = False) -> dict[str, Any]:
    payload = {
        "symbol": "SOLUSDT",
        "lastUpdateId": 987654321,
        "observed_at_ms": observed_at_ms,
        "bids": [["84.00", "5"]],
        "asks": [["84.20", "4"]],
    }
    if stale:
        payload["stale"] = True
    return payload


def _table_rows(store: ArbitrageStore, table: str) -> list[dict[str, Any]]:
    with store.conn() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]


def _health_by_provider(store: ArbitrageStore) -> dict[str, dict[str, Any]]:
    return {row["provider_key"]: row for row in store.fetch_provider_health()}


def test_scheduler_remains_disabled_without_env_or_explicit_enable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    scheduler = ReadOnlyPollingScheduler(
        store,
        registry=_registry(_spec("binance_public")),
        fetchers={"binance_public": lambda _job: calls.append("called") or _orderbook_payload()},
        environ={},
    )

    disabled = scheduler.run_once(
        [{"provider_key": "binance_public", "capability": "cex_orderbook", "scope_key": "SOL-USDT"}],
        now_ms=NOW_MS,
    )
    assert disabled.enabled is False
    assert disabled.skipped_reason == "live_collectors_disabled"
    assert calls == []

    explicit = ReadOnlyPollingScheduler(
        store,
        registry=_registry(_spec("binance_public")),
        fetchers={"binance_public": lambda _job: calls.append("explicit") or _orderbook_payload()},
        environ={},
        enabled=True,
    )
    summary = explicit.run_once(
        [{"provider_key": "binance_public", "capability": "cex_orderbook", "scope_key": "SOL-USDT"}],
        now_ms=NOW_MS,
    )

    assert summary.enabled is True
    assert calls == ["explicit"]


def test_scheduler_retries_with_backoff_then_falls_back_to_next_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    sleeps: list[float] = []

    def primary(_job: dict[str, Any]) -> dict[str, Any]:
        calls.append("binance_public")
        raise TimeoutError("timeout while calling redaction_fixture_scheduler_secret_123456")

    def fallback(_job: dict[str, Any]) -> dict[str, Any]:
        calls.append("okx_public")
        return _orderbook_payload()

    scheduler = ReadOnlyPollingScheduler(
        store,
        registry=_registry(
            _spec("binance_public", priority=100),
            _spec("okx_public", priority=90),
        ),
        fetchers={"binance_public": primary, "okx_public": fallback},
        enabled=True,
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.5,
    )

    summary = scheduler.run_once(
        [
            {
                "provider_key": "binance_public",
                "capability": "cex_orderbook",
                "scope_key": "SOL-USDT",
                "max_retries": 1,
                "backoff_ms": 25,
                "interval_ms": 1_000,
                "jitter_ms": 100,
            }
        ],
        now_ms=NOW_MS,
    )

    assert calls == ["binance_public", "binance_public", "okx_public"]
    assert sleeps == [0.025]
    result = summary.results[0]
    assert result.status == "OK"
    assert result.provider_key == "okx_public"
    assert result.fallback_used is True
    assert result.next_due_ms == NOW_MS + 1_050
    assert store.get_collect_cursor("binance_public", "SOL-USDT") == ""
    assert store.get_collect_cursor("okx_public", "SOL-USDT") == str(NOW_MS)

    health = _health_by_provider(store)
    assert health["binance_public"]["status"] == "DEGRADED"
    assert health["binance_public"]["error_code"] == "provider_timeout"
    assert health["okx_public"]["status"] == "ACTIVE"
    assert "redaction_fixture_scheduler_secret" not in repr(store.fetch_dead_letters())

    event_types = [row["event_type"] for row in store.fetch_event_log_replay()]
    assert event_types.count("provider.job.failed") == 2
    assert "provider.job.completed" in event_types


def test_scheduler_does_not_advance_cursor_for_null_stale_or_invalid_results(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fetchers = {
        "null_provider": lambda _job: None,
        "stale_provider": lambda _job: _orderbook_payload(stale=True),
        "invalid_provider": lambda _job: {"symbol": "SOLUSDT", "observed_at_ms": NOW_MS, "bids": [], "asks": []},
    }
    scheduler = ReadOnlyPollingScheduler(
        store,
        registry=_registry(
            _spec("null_provider", priority=100),
            _spec("stale_provider", priority=90),
            _spec("invalid_provider", priority=80),
        ),
        fetchers=fetchers,
        enabled=True,
    )

    results = []
    for provider_key in fetchers:
        summary = scheduler.run_once(
            [
                {
                    "provider_key": provider_key,
                    "capability": "cex_orderbook",
                    "scope_key": f"{provider_key}:SOL-USDT",
                    "fallback_enabled": False,
                }
            ],
            now_ms=NOW_MS,
        )
        results.append(summary.results[0])

    assert [result.status for result in results] == ["DEGRADED", "DEGRADED", "DEGRADED"]
    assert [result.error_code for result in results] == [
        "provider_result_null",
        "provider_result_stale",
        "missing_best_bid",
    ]
    for provider_key in fetchers:
        assert store.get_collect_cursor(provider_key, f"{provider_key}:SOL-USDT") == ""

    deadletters = store.fetch_dead_letters()
    assert [row["error_code"] for row in deadletters if row["reason"] == "collect_failure"] == [
        "provider_result_null",
        "provider_result_stale",
        "missing_best_bid",
    ]
    stale_deadletter = next(row for row in deadletters if row["error_code"] == "provider_result_stale")
    stale_raw_payload = stale_deadletter["payload"]["raw_payload"]
    assert stale_raw_payload["capability"] == "cex_orderbook"
    assert stale_raw_payload["stale_source"] is True
    assert stale_raw_payload["retry_count"] == 0
    assert "payload_summary" in stale_raw_payload

    stale_orderbook = _table_rows(store, "arb_orderbook_snapshots")[0]
    stale_tick = _table_rows(store, "arb_market_ticks")[0]
    assert stale_orderbook["stale"] == 1
    assert stale_tick["stale"] == 1


def test_scheduler_exhausted_retries_land_in_dead_letters(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    sleeps: list[float] = []

    def timeout(_job: dict[str, Any]) -> dict[str, Any]:
        calls.append("binance_public")
        raise TimeoutError("timeout redaction_fixture_scheduler_secret_abcdef")

    scheduler = ReadOnlyPollingScheduler(
        store,
        registry=_registry(_spec("binance_public")),
        fetchers={"binance_public": timeout},
        enabled=True,
        sleep_fn=sleeps.append,
    )

    summary = scheduler.run_once(
        [
            {
                "provider_key": "binance_public",
                "capability": "cex_orderbook",
                "scope_key": "SOL-USDT",
                "max_retries": 2,
                "backoff_ms": 10,
                "fallback_enabled": False,
            }
        ],
        now_ms=NOW_MS,
    )

    result = summary.results[0]
    assert calls == ["binance_public", "binance_public", "binance_public"]
    assert sleeps == [0.01, 0.02]
    assert result.status == "DEGRADED"
    assert result.attempts == 3
    assert result.error_code == "provider_timeout"
    assert store.get_collect_cursor("binance_public", "SOL-USDT") == ""

    deadletters = store.fetch_dead_letters()
    exhausted = [row for row in deadletters if row["reason"] == "provider_retries_exhausted"][-1]
    assert exhausted["error_code"] == "provider_timeout"
    assert exhausted["payload"]["provider"] == "binance_public"
    assert exhausted["payload"]["capability"] == "cex_orderbook"
    assert exhausted["payload"]["scope_key"] == "SOL-USDT"
    assert exhausted["payload"]["retry_count"] == 2
    assert "payload_summary" in exhausted["payload"]
    assert "redaction_fixture_scheduler_secret" not in repr(deadletters)


def test_scheduler_marks_disabled_provider_and_uses_enabled_fallback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    scheduler = ReadOnlyPollingScheduler(
        store,
        registry=_registry(
            _spec("binance_public", priority=100, enabled_by_default=False),
            _spec("okx_public", priority=90),
        ),
        fetchers={
            "binance_public": lambda _job: calls.append("disabled") or _orderbook_payload(),
            "okx_public": lambda _job: calls.append("fallback") or _orderbook_payload(),
        },
        enabled=True,
    )

    summary = scheduler.run_once(
        [{"provider_key": "binance_public", "capability": "cex_orderbook", "scope_key": "SOL-USDT"}],
        now_ms=NOW_MS,
    )

    assert calls == ["fallback"]
    assert summary.results[0].provider_key == "okx_public"
    health = _health_by_provider(store)
    assert health["binance_public"]["status"] == "DISABLED"
    assert health["binance_public"]["error_code"] == "disabled_by_default"
    assert health["okx_public"]["status"] == "ACTIVE"


def test_scheduler_blocks_private_capability_without_fetching(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    scheduler = ReadOnlyPollingScheduler(
        store,
        registry=_registry(_spec("zerox", capabilities=("swap_build_tx",))),
        fetchers={"zerox": lambda _job: calls.append("private") or {"order": "should_not_happen"}},
        enabled=True,
    )

    summary = scheduler.run_once(
        [{"provider_key": "zerox", "capability": "swap_build_tx", "scope_key": "SOL-USDT"}],
        now_ms=NOW_MS,
    )

    assert calls == []
    assert summary.results[0].status == "DEGRADED"
    assert summary.results[0].error_code == "capability_not_read_only"
    assert store.fetch_dead_letters()[-1]["error_code"] == "capability_not_read_only"
    assert _table_rows(store, "arb_orders") == []
    assert _table_rows(store, "arb_transactions") == []
    assert _table_rows(store, "arb_transfers") == []
