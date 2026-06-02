from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


CAPABILITY_CEX_DEPOSIT_STATUS = "cex_deposit_status"
CAPABILITY_CEX_ORDER_SUBMIT = "cex_order_submit"
CAPABILITY_CEX_ORDER_RECONCILE = "cex_order_reconcile"
CEX_TRADE_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_CEX_DEPOSIT_STATUS,
    CAPABILITY_CEX_ORDER_SUBMIT,
    CAPABILITY_CEX_ORDER_RECONCILE,
)
CEX_OUTCOME_STATUSES = ("success", "pending", "partial", "failed", "unknown")


@dataclass(frozen=True, slots=True)
class CexTradeRequest:
    opportunity_id: int
    route_id: int
    run_id: int
    step_key: str
    route_type: str
    source_venue: str
    destination_venue: str
    cex_market: str
    deposit_network: str
    token_ca: str
    amount_krw: float
    slippage_bps: int
    idempotency_key: str
    source_chain: str = ""
    destination_chain: str = ""
    pool_ca: str = ""
    provider_refs: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CexDepositStatus:
    adapter_name: str
    dry_run: bool
    simulated: bool
    status: str
    terminal: bool
    deposit_ref: str
    fee_krw: float
    gas_krw: float
    latency_ms: int
    payload_evidence: dict[str, Any]
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "dry_run": self.dry_run,
            "simulated": self.simulated,
            "status": self.status,
            "terminal": self.terminal,
            "deposit_ref": self.deposit_ref,
            "fee_krw": self.fee_krw,
            "gas_krw": self.gas_krw,
            "latency_ms": self.latency_ms,
            "payload_evidence": self.payload_evidence,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class CexOrderSubmitResult:
    adapter_name: str
    dry_run: bool
    simulated: bool
    status: str
    order_ref: str
    fee_krw: float
    gas_krw: float
    latency_ms: int
    payload_evidence: dict[str, Any]
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "dry_run": self.dry_run,
            "simulated": self.simulated,
            "status": self.status,
            "order_ref": self.order_ref,
            "fee_krw": self.fee_krw,
            "gas_krw": self.gas_krw,
            "latency_ms": self.latency_ms,
            "payload_evidence": self.payload_evidence,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class CexOrderReconcile:
    adapter_name: str
    dry_run: bool
    simulated: bool
    status: str
    terminal: bool
    order_ref: str
    filled_amount_krw: float
    fee_krw: float
    gas_krw: float
    latency_ms: int
    payload_evidence: dict[str, Any]
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "dry_run": self.dry_run,
            "simulated": self.simulated,
            "status": self.status,
            "terminal": self.terminal,
            "order_ref": self.order_ref,
            "filled_amount_krw": self.filled_amount_krw,
            "fee_krw": self.fee_krw,
            "gas_krw": self.gas_krw,
            "latency_ms": self.latency_ms,
            "payload_evidence": self.payload_evidence,
            "capabilities": list(self.capabilities),
        }


@runtime_checkable
class CexTradeAdapter(Protocol):
    adapter_name: str
    dry_run: bool
    simulated: bool
    capabilities: tuple[str, ...]

    def deposit_status(self, request: CexTradeRequest) -> CexDepositStatus:
        ...

    def submit_order(self, request: CexTradeRequest) -> CexOrderSubmitResult:
        ...

    def reconcile_order(self, request: CexTradeRequest, submit_result: CexOrderSubmitResult) -> CexOrderReconcile:
        ...


class DeterministicCexTradeAdapter:
    """No-network CEX adapter for deterministic deposit/order tests.

    This adapter models deposit and sell-order boundaries only. It does not
    implement CEX withdrawals, private withdrawal permission, or real order
    network submission.
    """

    adapter_name = "deterministic_cex_trade"
    dry_run = True
    simulated = True
    capabilities = CEX_TRADE_CAPABILITIES

    def deposit_status(self, request: CexTradeRequest) -> CexDepositStatus:
        _validate_request(request)
        status = _simulated_status(request, phase="deposit", fallback="success")
        deposit_ref = f"cex_deposit_{_digest('cex_deposit', _request_evidence(request))[:16]}"
        fee_krw = _cex_fee_krw(request)
        evidence = _phase_evidence(
            request,
            phase="deposit_status",
            status=status,
            extra={
                "deposit_ref": deposit_ref,
                "deposit_network": str(request.deposit_network or "").upper(),
                "blocked_deposit": status == "failed",
            },
        )
        return CexDepositStatus(
            adapter_name=self.adapter_name,
            dry_run=True,
            simulated=True,
            status=status,
            terminal=_terminal(status),
            deposit_ref=deposit_ref,
            fee_krw=fee_krw,
            gas_krw=0.0,
            latency_ms=0,
            payload_evidence=evidence,
            capabilities=self.capabilities,
        )

    def submit_order(self, request: CexTradeRequest) -> CexOrderSubmitResult:
        _validate_request(request)
        status = _simulated_status(request, phase="order_submit", fallback="success")
        order_ref = f"cex_order_{_digest('cex_order_submit', _request_evidence(request))[:16]}"
        fee_krw = _cex_fee_krw(request)
        evidence = _phase_evidence(
            request,
            phase="order_submit",
            status=status,
            extra={
                "order_ref": order_ref,
                "order_side": "sell",
                "order_type": "market",
                "external_submission": False,
                "real_cex_order": False,
                "cex_withdrawal": False,
            },
        )
        return CexOrderSubmitResult(
            adapter_name=self.adapter_name,
            dry_run=True,
            simulated=True,
            status=status,
            order_ref=order_ref,
            fee_krw=fee_krw,
            gas_krw=0.0,
            latency_ms=0,
            payload_evidence=evidence,
            capabilities=self.capabilities,
        )

    def reconcile_order(self, request: CexTradeRequest, submit_result: CexOrderSubmitResult) -> CexOrderReconcile:
        _validate_request(request)
        status = _simulated_status(request, phase="order_reconcile", fallback=submit_result.status)
        fee_krw = submit_result.fee_krw
        filled_amount_krw = _filled_amount_krw(float(request.amount_krw), status)
        evidence = _phase_evidence(
            request,
            phase="order_reconcile",
            status=status,
            extra={
                "order_ref": submit_result.order_ref,
                "filled_amount_krw": filled_amount_krw,
                "terminal": _terminal(status),
                "submit_evidence": submit_result.to_dict(),
            },
        )
        return CexOrderReconcile(
            adapter_name=self.adapter_name,
            dry_run=True,
            simulated=True,
            status=status,
            terminal=_terminal(status),
            order_ref=submit_result.order_ref,
            filled_amount_krw=filled_amount_krw,
            fee_krw=fee_krw,
            gas_krw=0.0,
            latency_ms=0,
            payload_evidence=evidence,
            capabilities=self.capabilities,
        )


def _validate_request(request: CexTradeRequest) -> None:
    if int(request.opportunity_id) <= 0:
        raise ValueError("opportunity_id_required")
    if int(request.route_id) <= 0:
        raise ValueError("route_id_required")
    if int(request.run_id) <= 0:
        raise ValueError("run_id_required")
    if not str(request.step_key or "").strip():
        raise ValueError("step_key_required")
    if not str(request.route_type or "").strip():
        raise ValueError("route_type_required")
    if not str(request.source_venue or "").strip():
        raise ValueError("source_venue_required")
    if not str(request.destination_venue or "").strip():
        raise ValueError("destination_venue_required")
    if not str(request.cex_market or "").strip():
        raise ValueError("cex_market_required")
    if not str(request.deposit_network or "").strip():
        raise ValueError("deposit_network_required")
    if not str(request.token_ca or "").strip():
        raise ValueError("token_ca_required")
    if float(request.amount_krw) <= 0:
        raise ValueError("positive_amount_krw_required")
    if int(request.slippage_bps) < 0:
        raise ValueError("slippage_bps_must_be_non_negative")
    if not str(request.idempotency_key or "").strip():
        raise ValueError("idempotency_key_required")


def _phase_evidence(
    request: CexTradeRequest,
    *,
    phase: str,
    status: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **_request_evidence(request),
        "phase": phase,
        "status": status,
        "dry_run": True,
        "simulated": True,
        "synthetic": True,
        "requires_secret": False,
        "network_calls": 0,
        "adapter_capabilities": list(CEX_TRADE_CAPABILITIES),
        **dict(extra or {}),
    }


def _request_evidence(request: CexTradeRequest) -> dict[str, Any]:
    return {
        "opportunity_id": int(request.opportunity_id),
        "route_id": int(request.route_id),
        "run_id": int(request.run_id),
        "step_key": str(request.step_key),
        "route_type": str(request.route_type),
        "source_chain": str(request.source_chain or "").upper(),
        "destination_chain": str(request.destination_chain or "").upper(),
        "source_venue": str(request.source_venue or "").upper(),
        "destination_venue": str(request.destination_venue or "").upper(),
        "token_ca": _normalize_address(request.token_ca),
        "pool_ca": _normalize_address(request.pool_ca),
        "cex_market": str(request.cex_market or ""),
        "deposit_network": str(request.deposit_network or "").upper(),
        "amount_krw": round(float(request.amount_krw), 8),
        "slippage_bps": int(request.slippage_bps),
        "idempotency_key": str(request.idempotency_key),
        "provider_refs": _redact_sensitive(request.provider_refs),
        "payload": _redact_sensitive(request.payload),
    }


def _simulated_status(request: CexTradeRequest, *, phase: str, fallback: str) -> str:
    phase_keys = {
        "deposit": ("deposit_status_by_step", "deposit_status"),
        "order_submit": ("order_submit_status_by_step", "order_submit_status", "submit_status_by_step", "submit_status"),
        "order_reconcile": (
            "order_reconcile_status_by_step",
            "order_reconcile_status",
            "reconcile_status_by_step",
            "reconcile_status",
            "status_by_step",
            "status",
        ),
    }.get(phase, ("status_by_step", "status"))
    for simulation in _simulation_payloads(request.payload):
        for key in phase_keys:
            status = _status_for_step(simulation.get(key), request.step_key)
            if status:
                return status
        status = _status_for_step(simulation.get("outcome_by_step"), request.step_key)
        if status:
            return status
        status = _status_for_step(simulation.get("outcome"), request.step_key)
        if status:
            return status
    return _normalized_status(fallback) or "unknown"


def _simulation_payloads(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    payloads: list[Mapping[str, Any]] = []
    direct = payload.get("cex_simulation")
    if isinstance(direct, Mapping):
        payloads.append(direct)
    route_payload = payload.get("route_payload")
    if isinstance(route_payload, Mapping):
        route_simulation = route_payload.get("cex_simulation")
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
    status = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if status in CEX_OUTCOME_STATUSES:
        return status
    return ""


def _terminal(status: str) -> bool:
    return status in {"success", "partial", "failed"}


def _filled_amount_krw(amount_krw: float, status: str) -> float:
    if status == "success":
        return round(amount_krw, 2)
    if status == "partial":
        return round(amount_krw * 0.5, 2)
    return 0.0


def _cex_fee_krw(request: CexTradeRequest) -> float:
    return round(max(100.0, float(request.amount_krw) * 0.0005), 2)


def _normalize_address(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _digest(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _sensitive_key(str(key)) else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    return value


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
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
