from __future__ import annotations

from dataclasses import dataclass


CAPABILITY_DEX_POOL = "dex_pool"
CAPABILITY_DEX_POOL_PRICE = "dex_pool_price"
CAPABILITY_DEX_PAIR_SEARCH = "dex_pair_search"
CAPABILITY_CEX_ORDERBOOK = "cex_orderbook"
CAPABILITY_KRW_ORDERBOOK = "krw_orderbook"
CAPABILITY_FX_RATE = "fx_rate"
CAPABILITY_RPC_FRESHNESS = "rpc_freshness"
CAPABILITY_RPC_BLOCK_FRESHNESS = "rpc_block_freshness"
CAPABILITY_SWAP_QUOTE = "swap_quote"
CAPABILITY_SWAP_BUILD_TX = "swap_build_tx"
CAPABILITY_BRIDGE_QUOTE = "bridge_quote"
CAPABILITY_BRIDGE_BUILD_TX = "bridge_build_tx"
CAPABILITY_RISK_CHECK = "risk_check"
CAPABILITY_EXPLORER_HISTORY = "explorer_history"
CAPABILITY_COIN_PRICE = "coin_price"

SUPPORTED_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_DEX_POOL,
    CAPABILITY_DEX_POOL_PRICE,
    CAPABILITY_DEX_PAIR_SEARCH,
    CAPABILITY_CEX_ORDERBOOK,
    CAPABILITY_KRW_ORDERBOOK,
    CAPABILITY_FX_RATE,
    CAPABILITY_RPC_FRESHNESS,
    CAPABILITY_RPC_BLOCK_FRESHNESS,
    CAPABILITY_SWAP_QUOTE,
    CAPABILITY_SWAP_BUILD_TX,
    CAPABILITY_BRIDGE_QUOTE,
    CAPABILITY_BRIDGE_BUILD_TX,
    CAPABILITY_RISK_CHECK,
    CAPABILITY_EXPLORER_HISTORY,
    CAPABILITY_COIN_PRICE,
)
SUPPORTED_CAPABILITY_SET = frozenset(SUPPORTED_CAPABILITIES)

CAPABILITY_ALIASES: dict[str, str] = {
    CAPABILITY_DEX_POOL_PRICE: CAPABILITY_DEX_POOL,
    CAPABILITY_RPC_BLOCK_FRESHNESS: CAPABILITY_RPC_FRESHNESS,
}

READ_ONLY_HTTP_V1_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_DEX_POOL,
    CAPABILITY_CEX_ORDERBOOK,
    CAPABILITY_KRW_ORDERBOOK,
    CAPABILITY_FX_RATE,
    CAPABILITY_RPC_FRESHNESS,
)
READ_ONLY_HTTP_V1_CAPABILITY_SET = frozenset(READ_ONLY_HTTP_V1_CAPABILITIES)


def normalize_capability(capability: str) -> str:
    value = str(capability or "").strip()
    return CAPABILITY_ALIASES.get(value, value)

SUPPORTED_AUTH_TYPES: tuple[str, ...] = ("public", "api_key")
SUPPORTED_PROVIDER_KINDS: tuple[str, ...] = (
    "dex",
    "cex",
    "swap",
    "bridge",
    "coin_price",
    "fx",
    "rpc",
    "explorer",
    "risk",
)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider_key: str
    kind: str
    capabilities: tuple[str, ...]
    auth_type: str
    required_env: tuple[str, ...]
    priority: int
    enabled_by_default: bool
    display_name: str = ""
    docs_url: str = ""


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider_key: str
    enabled: bool
    reason: str
    missing_env: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
