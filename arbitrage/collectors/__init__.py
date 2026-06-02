"""Read-only fixture collectors for arbitrage observations."""

from .base import (
    STATUS_DEGRADED,
    STATUS_OK,
    CollectorResult,
    CollectorStatus,
    ProviderPayloadError,
    current_observed_at_ms,
    ensure_payload_mapping,
    monotonic_cursor_value,
    normalize_observed_at_ms,
    provider_payload_error,
    redact_provider_payload,
    redact_provider_text,
)
from .cex import ingest_cex_orderbook_fixture
from .dex import ingest_dex_fixture
from .fx import ingest_fx_fixture
from .rpc import ingest_rpc_freshness_fixture

__all__ = [
    "STATUS_DEGRADED",
    "STATUS_OK",
    "CollectorResult",
    "CollectorStatus",
    "ProviderPayloadError",
    "current_observed_at_ms",
    "ensure_payload_mapping",
    "monotonic_cursor_value",
    "normalize_observed_at_ms",
    "provider_payload_error",
    "redact_provider_payload",
    "redact_provider_text",
    "ingest_cex_orderbook_fixture",
    "ingest_dex_fixture",
    "ingest_fx_fixture",
    "ingest_rpc_freshness_fixture",
]
