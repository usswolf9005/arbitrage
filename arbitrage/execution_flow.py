from __future__ import annotations

from collections.abc import Iterable


FLOW_EDGE_ENDPOINTS: dict[str, tuple[str, str]] = {
    "signal-precheck": ("signal", "precheck"),
    "precheck-buy": ("precheck", "dexBuy"),
    "buy-same": ("dexBuy", "sameDexSell"),
    "buy-wallet-hold": ("dexBuy", "walletHold"),
    "buy-bridge-dex": ("dexBuy", "bridgeDexSell"),
    "buy-bridge-cex": ("dexBuy", "bridgeCexDeposit"),
    "bridge-cex-sell": ("bridgeCexDeposit", "bridgeCexSell"),
    "buy-direct-cex": ("dexBuy", "directCexDeposit"),
    "direct-cex-sell": ("directCexDeposit", "directCexSell"),
}

ROUTE_NODE_IDS: dict[str, tuple[str, ...]] = {
    "same_dex_sell": ("sameDexSell",),
    "bridge_dex_sell": ("bridgeDexSell",),
    "bridge_cex_sell": ("bridgeCexDeposit", "bridgeCexSell"),
    "direct_cex_sell": ("directCexDeposit", "directCexSell"),
}

ROUTE_EDGE_IDS: dict[str, tuple[str, ...]] = {
    "same_dex_sell": ("buy-same",),
    "bridge_dex_sell": ("buy-bridge-dex",),
    "bridge_cex_sell": ("buy-bridge-cex", "bridge-cex-sell"),
    "direct_cex_sell": ("buy-direct-cex", "direct-cex-sell"),
}

STEP_NODE_IDS: dict[str, dict[str, str]] = {
    "same_dex_sell": {
        "precheck": "precheck",
        "dex_buy": "dexBuy",
        "wallet_hold": "walletHold",
        "exit_route_select": "sameDexSell",
        "same_dex_sell": "sameDexSell",
        "settle": "sameDexSell",
    },
    "bridge_dex_sell": {
        "precheck": "precheck",
        "dex_buy": "dexBuy",
        "wallet_hold": "walletHold",
        "exit_route_select": "bridgeDexSell",
        "bridge": "bridgeDexSell",
        "bridge_dex_sell": "bridgeDexSell",
        "settle": "bridgeDexSell",
    },
    "bridge_cex_sell": {
        "precheck": "precheck",
        "dex_buy": "dexBuy",
        "wallet_hold": "walletHold",
        "exit_route_select": "bridgeCexDeposit",
        "bridge": "bridgeCexDeposit",
        "cex_deposit": "bridgeCexDeposit",
        "cex_sell": "bridgeCexSell",
        "settle": "bridgeCexSell",
    },
    "direct_cex_sell": {
        "precheck": "precheck",
        "dex_buy": "dexBuy",
        "wallet_hold": "walletHold",
        "exit_route_select": "directCexDeposit",
        "cex_deposit": "directCexDeposit",
        "cex_sell": "directCexSell",
        "settle": "directCexSell",
    },
}

STEP_EDGE_IDS: dict[str, dict[str, str]] = {
    "same_dex_sell": {
        "precheck": "signal-precheck",
        "dex_buy": "precheck-buy",
        "wallet_hold": "buy-wallet-hold",
        "exit_route_select": "buy-same",
        "same_dex_sell": "buy-same",
        "settle": "buy-same",
    },
    "bridge_dex_sell": {
        "precheck": "signal-precheck",
        "dex_buy": "precheck-buy",
        "wallet_hold": "buy-wallet-hold",
        "exit_route_select": "buy-bridge-dex",
        "bridge": "buy-bridge-dex",
        "bridge_dex_sell": "buy-bridge-dex",
        "settle": "buy-bridge-dex",
    },
    "bridge_cex_sell": {
        "precheck": "signal-precheck",
        "dex_buy": "precheck-buy",
        "wallet_hold": "buy-wallet-hold",
        "exit_route_select": "buy-bridge-cex",
        "bridge": "buy-bridge-cex",
        "cex_deposit": "buy-bridge-cex",
        "cex_sell": "bridge-cex-sell",
        "settle": "bridge-cex-sell",
    },
    "direct_cex_sell": {
        "precheck": "signal-precheck",
        "dex_buy": "precheck-buy",
        "wallet_hold": "buy-wallet-hold",
        "exit_route_select": "buy-direct-cex",
        "cex_deposit": "buy-direct-cex",
        "cex_sell": "direct-cex-sell",
        "settle": "direct-cex-sell",
    },
}

STEP_STATUS_STATE = {
    "PENDING": "wait",
    "RUNNING": "active",
    "COMPLETED": "done",
    "RECONCILE": "warn",
    "BLOCKED": "blocked",
    "FAILED": "failed",
    "SKIPPED": "skipped",
}


def normalize_route_type(route_type: str | None) -> str:
    normalized = str(route_type or "same_dex_sell")
    return normalized if normalized in ROUTE_NODE_IDS else "same_dex_sell"


def route_node_ids(route_type: str | None) -> tuple[str, ...]:
    return ROUTE_NODE_IDS[normalize_route_type(route_type)]


def route_edge_ids(route_type: str | None) -> tuple[str, ...]:
    return ROUTE_EDGE_IDS[normalize_route_type(route_type)]


def step_node_id(route_type: str | None, step_key: str) -> str | None:
    return STEP_NODE_IDS[normalize_route_type(route_type)].get(str(step_key))


def step_edge_id(route_type: str | None, step_key: str) -> str | None:
    return STEP_EDGE_IDS[normalize_route_type(route_type)].get(str(step_key))


def flow_edge_endpoints(edge_id: str) -> tuple[str, str]:
    return FLOW_EDGE_ENDPOINTS.get(str(edge_id), ("", ""))


def ui_state_for_step_status(status: str | None) -> str:
    return STEP_STATUS_STATE.get(str(status or "PENDING").upper(), "wait")


def unique_ordered(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered
