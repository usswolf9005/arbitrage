from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .providers.base import CAPABILITY_SWAP_BUILD_TX, CAPABILITY_SWAP_QUOTE


CAPABILITY_SWAP_SUBMIT = "swap_submit"
CAPABILITY_SWAP_STATUS = "swap_status"
DEX_SWAP_SUBMIT_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_SWAP_QUOTE,
    CAPABILITY_SWAP_BUILD_TX,
    CAPABILITY_SWAP_SUBMIT,
    CAPABILITY_SWAP_STATUS,
)


@dataclass(frozen=True, slots=True)
class DexSwapRequest:
    route_id: int
    opportunity_id: int
    chain: str
    buy_market: Mapping[str, Any]
    sell_market: Mapping[str, Any]
    token_ca: str
    pool_ca: str
    amount_krw: float
    slippage_bps: int
    idempotency_key: str
    step_key: str = "dex_swap"
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DexSwapQuote:
    adapter_name: str
    dry_run: bool
    status: str
    gas_krw: float
    fee_krw: float
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "dry_run": self.dry_run,
            "status": self.status,
            "gas_krw": self.gas_krw,
            "fee_krw": self.fee_krw,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class DexSwapBuild:
    adapter_name: str
    dry_run: bool
    status: str
    build_ref: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "dry_run": self.dry_run,
            "status": self.status,
            "build_ref": self.build_ref,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class DexSwapSubmitResult:
    adapter_name: str
    dry_run: bool
    status: str
    tx_hash: str
    submit_ref: str
    gas_krw: float
    fee_krw: float
    quote_evidence: dict[str, Any]
    build_evidence: dict[str, Any]
    payload_evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "dry_run": self.dry_run,
            "status": self.status,
            "tx_hash": self.tx_hash,
            "submit_ref": self.submit_ref,
            "gas_krw": self.gas_krw,
            "fee_krw": self.fee_krw,
            "quote_evidence": self.quote_evidence,
            "build_evidence": self.build_evidence,
            "payload_evidence": self.payload_evidence,
        }


@dataclass(frozen=True, slots=True)
class DexSwapStatus:
    adapter_name: str
    dry_run: bool
    status: str
    terminal: bool
    tx_hash: str
    submit_ref: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "dry_run": self.dry_run,
            "status": self.status,
            "terminal": self.terminal,
            "tx_hash": self.tx_hash,
            "submit_ref": self.submit_ref,
            "evidence": self.evidence,
        }


@runtime_checkable
class DexSwapSubmitAdapter(Protocol):
    adapter_name: str
    dry_run: bool
    capabilities: tuple[str, ...]

    def quote(self, request: DexSwapRequest) -> DexSwapQuote:
        ...

    def build(self, request: DexSwapRequest, quote: DexSwapQuote) -> DexSwapBuild:
        ...

    def submit(self, request: DexSwapRequest, build: DexSwapBuild) -> DexSwapSubmitResult:
        ...

    def status(self, request: DexSwapRequest, submit_result: DexSwapSubmitResult) -> DexSwapStatus:
        ...

    def reconcile(self, request: DexSwapRequest, submit_result: DexSwapSubmitResult) -> DexSwapStatus:
        ...


class DryRunDexSwapAdapter:
    """Deterministic DEX swap adapter for Part 7 dry-runs only.

    Future real DEX providers should implement the same capability-oriented
    protocol and resolve secrets outside this boundary. Execution code should
    depend on capability names, not concrete provider keys.
    """

    adapter_name = "dry_run_dex_swap"
    dry_run = True
    capabilities = DEX_SWAP_SUBMIT_CAPABILITIES

    def quote(self, request: DexSwapRequest) -> DexSwapQuote:
        _validate_request(request)
        amount = float(request.amount_krw)
        gas_krw = round(max(500.0, amount * 0.002), 2)
        fee_krw = round(max(100.0, amount * 0.0005), 2)
        slippage_krw = round(amount * (max(int(request.slippage_bps), 0) / 10_000.0), 2)
        expected_out_krw = round(amount + max(amount * 0.003, 1.0), 2)
        min_out_krw = round(max(0.0, expected_out_krw - slippage_krw - gas_krw - fee_krw), 2)
        evidence = {
            "quote_ref": f"dryrun_quote_{_digest('quote', _request_evidence(request))[:16]}",
            "route_id": int(request.route_id),
            "opportunity_id": int(request.opportunity_id),
            "step_key": str(request.step_key),
            "chain": str(request.chain),
            "token_ca": _normalize_address(request.token_ca),
            "pool_ca": _normalize_address(request.pool_ca),
            "amount_krw": amount,
            "slippage_bps": int(request.slippage_bps),
            "amount_out_expected_krw": expected_out_krw,
            "amount_out_min_krw": min_out_krw,
            "buy_market": _market_evidence(request.buy_market),
            "sell_market": _market_evidence(request.sell_market),
            "adapter_capabilities": list(self.capabilities),
            "requires_secret": False,
            "network_calls": 0,
        }
        return DexSwapQuote(
            adapter_name=self.adapter_name,
            dry_run=True,
            status="success",
            gas_krw=gas_krw,
            fee_krw=fee_krw,
            evidence=evidence,
        )

    def build(self, request: DexSwapRequest, quote: DexSwapQuote) -> DexSwapBuild:
        _validate_request(request)
        if not quote.dry_run or quote.status != "success":
            raise ValueError("successful_dry_run_quote_required")
        build_ref = f"dryrun_build_{_digest('build', _request_evidence(request), quote.evidence)[:16]}"
        evidence = {
            "build_ref": build_ref,
            "route_id": int(request.route_id),
            "opportunity_id": int(request.opportunity_id),
            "step_key": str(request.step_key),
            "chain": str(request.chain),
            "transaction_kind": "dry_run_unsigned_intent",
            "unsigned_intent": {
                "token_ca": _normalize_address(request.token_ca),
                "pool_ca": _normalize_address(request.pool_ca),
                "amount_krw": float(request.amount_krw),
                "slippage_bps": int(request.slippage_bps),
                "not_for_signing": True,
            },
            "signed_payload": None,
            "raw_transaction": None,
            "quote_evidence": quote.evidence,
            "gas_krw": quote.gas_krw,
            "fee_krw": quote.fee_krw,
            "adapter_capabilities": list(self.capabilities),
            "requires_secret": False,
            "network_calls": 0,
        }
        return DexSwapBuild(
            adapter_name=self.adapter_name,
            dry_run=True,
            status="success",
            build_ref=build_ref,
            evidence=evidence,
        )

    def submit(self, request: DexSwapRequest, build: DexSwapBuild) -> DexSwapSubmitResult:
        _validate_request(request)
        if not build.dry_run or build.status != "success":
            raise ValueError("successful_dry_run_build_required")
        digest = _digest("submit", _request_evidence(request), build.evidence)
        tx_hash = f"dryrun_{digest[:48]}"
        submit_ref = f"dryrun_submit_{digest[:16]}"
        simulated_status = _simulated_submit_status(request)
        payload_evidence = {
            "submit_ref": submit_ref,
            "tx_hash": tx_hash,
            "idempotency_key": str(request.idempotency_key),
            "route_id": int(request.route_id),
            "opportunity_id": int(request.opportunity_id),
            "step_key": str(request.step_key),
            "dry_run": True,
            "synthetic": True,
            "status": simulated_status,
            "real_chain_state": False,
            "external_submission": False,
            "network_calls": 0,
            "requires_secret": False,
            "adapter_capabilities": list(self.capabilities),
        }
        if simulated_status != "success":
            payload_evidence["simulated_outcome"] = True
            payload_evidence["simulation_source"] = "request_payload"
        return DexSwapSubmitResult(
            adapter_name=self.adapter_name,
            dry_run=True,
            status=simulated_status,
            tx_hash=tx_hash,
            submit_ref=submit_ref,
            gas_krw=float(build.evidence.get("gas_krw") or 0.0) or float(_safe_quote_value(build, "gas_krw")),
            fee_krw=float(build.evidence.get("fee_krw") or 0.0) or float(_safe_quote_value(build, "fee_krw")),
            quote_evidence=dict(build.evidence.get("quote_evidence") or {}),
            build_evidence=build.evidence,
            payload_evidence=payload_evidence,
        )

    def status(self, request: DexSwapRequest, submit_result: DexSwapSubmitResult) -> DexSwapStatus:
        return self.reconcile(request, submit_result)

    def reconcile(self, request: DexSwapRequest, submit_result: DexSwapSubmitResult) -> DexSwapStatus:
        _validate_request(request)
        evidence = {
            "reconcile_ref": f"dryrun_status_{_digest('status', submit_result.to_dict())[:16]}",
            "dry_run": True,
            "synthetic": True,
            "real_chain_state": False,
            "network_calls": 0,
            "adapter_capabilities": list(self.capabilities),
        }
        return DexSwapStatus(
            adapter_name=self.adapter_name,
            dry_run=True,
            status=submit_result.status,
            terminal=True,
            tx_hash=submit_result.tx_hash,
            submit_ref=submit_result.submit_ref,
            evidence=evidence,
        )

    def execute(self, request: DexSwapRequest) -> DexSwapSubmitResult:
        quote = self.quote(request)
        build = self.build(request, quote)
        return self.submit(request, build)


def _validate_request(request: DexSwapRequest) -> None:
    if int(request.route_id) <= 0:
        raise ValueError("route_id_required")
    if int(request.opportunity_id) <= 0:
        raise ValueError("opportunity_id_required")
    if not str(request.chain or "").strip():
        raise ValueError("chain_required")
    if not str(request.idempotency_key or "").strip():
        raise ValueError("idempotency_key_required")
    if float(request.amount_krw) <= 0:
        raise ValueError("positive_amount_krw_required")
    if int(request.slippage_bps) < 0:
        raise ValueError("slippage_bps_must_be_non_negative")
    if not str(request.token_ca or "").strip():
        raise ValueError("token_ca_required")
    if not str(request.pool_ca or "").strip():
        raise ValueError("pool_ca_required")


def _request_evidence(request: DexSwapRequest) -> dict[str, Any]:
    return {
        "route_id": int(request.route_id),
        "opportunity_id": int(request.opportunity_id),
        "chain": str(request.chain).upper(),
        "buy_market": _market_evidence(request.buy_market),
        "sell_market": _market_evidence(request.sell_market),
        "token_ca": _normalize_address(request.token_ca),
        "pool_ca": _normalize_address(request.pool_ca),
        "amount_krw": round(float(request.amount_krw), 8),
        "slippage_bps": int(request.slippage_bps),
        "idempotency_key": str(request.idempotency_key),
        "step_key": str(request.step_key),
        "payload": dict(request.payload or {}),
    }


def _market_evidence(market: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": market.get("id"),
        "venue": market.get("venue"),
        "venue_type": market.get("venue_type"),
        "chain": market.get("chain"),
        "market": market.get("market"),
        "market_key": market.get("market_key"),
        "market_type": market.get("market_type"),
        "token_ca": _normalize_address(market.get("token_ca")),
        "pool_ca": _normalize_address(market.get("pool_ca")),
        "quote_asset": market.get("quote_asset"),
    }


def _normalize_address(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _digest(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_quote_value(build: DexSwapBuild, key: str) -> float:
    quote = build.evidence.get("quote_evidence")
    if isinstance(quote, Mapping):
        try:
            return float(quote.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _simulated_submit_status(request: DexSwapRequest) -> str:
    """Resolve deterministic dry-run failure simulation from request payload.

    Test and route payloads may set:
    - dry_run_simulation.unknown_outcome_step = "dex_buy"
    - dry_run_simulation.unknown_outcome_steps = ["same_dex_sell"]
    - dry_run_simulation.submit_status_by_step = {"dex_buy": "unknown"}
    - dry_run_simulation.submit_status = "unknown"
    """
    step_key = str(request.step_key)
    for simulation in _simulation_payloads(request.payload):
        status = _status_for_step(simulation.get("submit_status_by_step"), step_key)
        if status:
            return status
        status = _status_for_step(simulation.get("submit_status"), step_key)
        if status:
            return status
        if _step_selected(simulation.get("unknown_outcome_step"), step_key):
            return "unknown"
        if _step_selected(simulation.get("unknown_outcome_steps"), step_key):
            return "unknown"
        if _step_selected(simulation.get("simulate_unknown_outcome"), step_key):
            return "unknown"
    return "success"


def _simulation_payloads(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    payloads: list[Mapping[str, Any]] = []
    direct = payload.get("dry_run_simulation")
    if isinstance(direct, Mapping):
        payloads.append(direct)
    route_payload = payload.get("route_payload")
    if isinstance(route_payload, Mapping):
        route_simulation = route_payload.get("dry_run_simulation")
        if isinstance(route_simulation, Mapping):
            payloads.append(route_simulation)
    payloads.append(payload)
    return payloads


def _status_for_step(raw: Any, step_key: str) -> str:
    if isinstance(raw, Mapping):
        value = raw.get(step_key) or raw.get("*")
        return _normalized_status(value)
    return _normalized_status(raw)


def _normalized_status(raw: Any) -> str:
    status = str(raw or "").strip().lower()
    if not status:
        return ""
    return status.replace(" ", "_").replace("-", "_")


def _step_selected(raw: Any, step_key: str) -> bool:
    if raw is True:
        return True
    if isinstance(raw, str):
        normalized = raw.strip()
        return normalized in {step_key, "*"} or normalized.lower() in {"1", "true", "yes", "all"}
    if isinstance(raw, Mapping):
        return bool(raw.get(step_key) or raw.get("*"))
    if isinstance(raw, (list, tuple, set)):
        return any(_step_selected(item, step_key) for item in raw)
    return False
