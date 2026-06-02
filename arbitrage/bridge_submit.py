from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .providers.base import CAPABILITY_BRIDGE_BUILD_TX, CAPABILITY_BRIDGE_QUOTE


CAPABILITY_BRIDGE_SUBMIT = "bridge_submit"
CAPABILITY_BRIDGE_STATUS = "bridge_status"
BRIDGE_SUBMIT_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_BRIDGE_QUOTE,
    CAPABILITY_BRIDGE_BUILD_TX,
    CAPABILITY_BRIDGE_SUBMIT,
    CAPABILITY_BRIDGE_STATUS,
)
BRIDGE_OUTCOME_STATUSES = ("success", "pending", "partial", "failed", "unknown")


@dataclass(frozen=True, slots=True)
class BridgeSubmitRequest:
    opportunity_id: int
    route_id: int
    run_id: int
    step_key: str
    route_type: str
    source_chain: str
    destination_chain: str
    token_ca: str
    amount_krw: float
    slippage_bps: int
    idempotency_key: str
    source_venue: str = ""
    destination_venue: str = ""
    pool_ca: str = ""
    cex_market: str = ""
    deposit_network: str = ""
    provider_refs: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BridgeQuote:
    adapter_name: str
    dry_run: bool
    simulated: bool
    status: str
    bridge_ref: str
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
            "bridge_ref": self.bridge_ref,
            "fee_krw": self.fee_krw,
            "gas_krw": self.gas_krw,
            "latency_ms": self.latency_ms,
            "payload_evidence": self.payload_evidence,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class BridgeBuild:
    adapter_name: str
    dry_run: bool
    simulated: bool
    status: str
    build_ref: str
    bridge_ref: str
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
            "build_ref": self.build_ref,
            "bridge_ref": self.bridge_ref,
            "fee_krw": self.fee_krw,
            "gas_krw": self.gas_krw,
            "latency_ms": self.latency_ms,
            "payload_evidence": self.payload_evidence,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class BridgeSubmitResult:
    adapter_name: str
    dry_run: bool
    simulated: bool
    status: str
    submit_ref: str
    bridge_ref: str
    fee_krw: float
    gas_krw: float
    latency_ms: int
    quote_evidence: dict[str, Any]
    build_evidence: dict[str, Any]
    payload_evidence: dict[str, Any]
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "dry_run": self.dry_run,
            "simulated": self.simulated,
            "status": self.status,
            "submit_ref": self.submit_ref,
            "bridge_ref": self.bridge_ref,
            "fee_krw": self.fee_krw,
            "gas_krw": self.gas_krw,
            "latency_ms": self.latency_ms,
            "quote_evidence": self.quote_evidence,
            "build_evidence": self.build_evidence,
            "payload_evidence": self.payload_evidence,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class BridgeStatus:
    adapter_name: str
    dry_run: bool
    simulated: bool
    status: str
    terminal: bool
    submit_ref: str
    bridge_ref: str
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
            "submit_ref": self.submit_ref,
            "bridge_ref": self.bridge_ref,
            "fee_krw": self.fee_krw,
            "gas_krw": self.gas_krw,
            "latency_ms": self.latency_ms,
            "payload_evidence": self.payload_evidence,
            "capabilities": list(self.capabilities),
        }


@runtime_checkable
class BridgeSubmitAdapter(Protocol):
    adapter_name: str
    dry_run: bool
    simulated: bool
    capabilities: tuple[str, ...]

    def quote(self, request: BridgeSubmitRequest) -> BridgeQuote:
        ...

    def build(self, request: BridgeSubmitRequest, quote: BridgeQuote) -> BridgeBuild:
        ...

    def submit(self, request: BridgeSubmitRequest, build: BridgeBuild) -> BridgeSubmitResult:
        ...

    def status(self, request: BridgeSubmitRequest, submit_result: BridgeSubmitResult) -> BridgeStatus:
        ...

    def reconcile(self, request: BridgeSubmitRequest, submit_result: BridgeSubmitResult) -> BridgeStatus:
        ...


class DeterministicBridgeSubmitAdapter:
    """No-network bridge adapter used for deterministic tests and safe Part 8 boundaries.

    Real bridge providers should be added by capability name and keep API keys
    outside DB/log/SSE payloads. This default adapter never signs, never submits
    a raw transaction, and never calls a provider SDK.
    """

    adapter_name = "deterministic_bridge_submit"
    dry_run = True
    simulated = True
    capabilities = BRIDGE_SUBMIT_CAPABILITIES

    def quote(self, request: BridgeSubmitRequest) -> BridgeQuote:
        _validate_request(request)
        status = _simulated_status(request, phase="quote", fallback="success")
        fees = _bridge_fee_evidence(request)
        bridge_ref = f"bridge_quote_{_digest('bridge_quote', _request_evidence(request))[:16]}"
        evidence = _phase_evidence(
            request,
            phase="quote",
            status=status,
            bridge_ref=bridge_ref,
            extra={
                "amount_out_min_krw": max(0.0, round(float(request.amount_krw) - fees["fee_krw"] - fees["gas_krw"], 2)),
            },
        )
        return BridgeQuote(
            adapter_name=self.adapter_name,
            dry_run=True,
            simulated=True,
            status=status,
            bridge_ref=bridge_ref,
            fee_krw=fees["fee_krw"],
            gas_krw=fees["gas_krw"],
            latency_ms=fees["latency_ms"],
            payload_evidence=evidence,
            capabilities=self.capabilities,
        )

    def build(self, request: BridgeSubmitRequest, quote: BridgeQuote) -> BridgeBuild:
        _validate_request(request)
        status = _simulated_status(
            request,
            phase="build",
            fallback=quote.status if quote.status != "success" else "success",
        )
        build_ref = f"bridge_build_{_digest('bridge_build', _request_evidence(request), quote.to_dict())[:16]}"
        evidence = _phase_evidence(
            request,
            phase="build",
            status=status,
            bridge_ref=quote.bridge_ref,
            extra={
                "build_ref": build_ref,
                "transaction_kind": "simulated_bridge_unsigned_intent",
                "unsigned_intent": {
                    "token_ca": _normalize_address(request.token_ca),
                    "pool_ca": _normalize_address(request.pool_ca),
                    "amount_krw": float(request.amount_krw),
                    "slippage_bps": int(request.slippage_bps),
                    "not_for_signing": True,
                },
                "signed_payload": None,
                "raw_transaction": None,
                "quote_evidence": quote.to_dict(),
            },
        )
        return BridgeBuild(
            adapter_name=self.adapter_name,
            dry_run=True,
            simulated=True,
            status=status,
            build_ref=build_ref,
            bridge_ref=quote.bridge_ref,
            fee_krw=quote.fee_krw,
            gas_krw=quote.gas_krw,
            latency_ms=quote.latency_ms,
            payload_evidence=evidence,
            capabilities=self.capabilities,
        )

    def submit(self, request: BridgeSubmitRequest, build: BridgeBuild) -> BridgeSubmitResult:
        _validate_request(request)
        status = _simulated_status(
            request,
            phase="submit",
            fallback=build.status if build.status != "success" else "success",
        )
        submit_ref = f"bridge_submit_{_digest('bridge_submit', _request_evidence(request), build.to_dict())[:16]}"
        evidence = _phase_evidence(
            request,
            phase="submit",
            status=status,
            bridge_ref=build.bridge_ref,
            extra={
                "submit_ref": submit_ref,
                "build_ref": build.build_ref,
                "external_submission": False,
                "real_bridge_submit": False,
                "raw_transaction": None,
                "signed_payload": None,
            },
        )
        return BridgeSubmitResult(
            adapter_name=self.adapter_name,
            dry_run=True,
            simulated=True,
            status=status,
            submit_ref=submit_ref,
            bridge_ref=build.bridge_ref,
            fee_krw=build.fee_krw,
            gas_krw=build.gas_krw,
            latency_ms=build.latency_ms,
            quote_evidence=dict(build.payload_evidence.get("quote_evidence") or {}),
            build_evidence=build.to_dict(),
            payload_evidence=evidence,
            capabilities=self.capabilities,
        )

    def status(self, request: BridgeSubmitRequest, submit_result: BridgeSubmitResult) -> BridgeStatus:
        return self._status_like(request, submit_result, phase="status")

    def reconcile(self, request: BridgeSubmitRequest, submit_result: BridgeSubmitResult) -> BridgeStatus:
        return self._status_like(request, submit_result, phase="reconcile")

    def _status_like(self, request: BridgeSubmitRequest, submit_result: BridgeSubmitResult, *, phase: str) -> BridgeStatus:
        _validate_request(request)
        status = _simulated_status(request, phase=phase, fallback=submit_result.status)
        evidence = _phase_evidence(
            request,
            phase=phase,
            status=status,
            bridge_ref=submit_result.bridge_ref,
            extra={
                "submit_ref": submit_result.submit_ref,
                "reconcile_ref": f"bridge_{phase}_{_digest(phase, submit_result.to_dict())[:16]}",
                "terminal": _terminal(status),
            },
        )
        return BridgeStatus(
            adapter_name=self.adapter_name,
            dry_run=True,
            simulated=True,
            status=status,
            terminal=_terminal(status),
            submit_ref=submit_result.submit_ref,
            bridge_ref=submit_result.bridge_ref,
            fee_krw=submit_result.fee_krw,
            gas_krw=submit_result.gas_krw,
            latency_ms=submit_result.latency_ms,
            payload_evidence=evidence,
            capabilities=self.capabilities,
        )


def _validate_request(request: BridgeSubmitRequest) -> None:
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
    if not str(request.source_chain or "").strip():
        raise ValueError("source_chain_required")
    if not str(request.destination_chain or "").strip():
        raise ValueError("destination_chain_required")
    if not str(request.token_ca or "").strip():
        raise ValueError("token_ca_required")
    if float(request.amount_krw) <= 0:
        raise ValueError("positive_amount_krw_required")
    if int(request.slippage_bps) < 0:
        raise ValueError("slippage_bps_must_be_non_negative")
    if not str(request.idempotency_key or "").strip():
        raise ValueError("idempotency_key_required")


def _bridge_fee_evidence(request: BridgeSubmitRequest) -> dict[str, Any]:
    amount = float(request.amount_krw)
    return {
        "fee_krw": round(max(250.0, amount * 0.0015), 2),
        "gas_krw": round(max(700.0, amount * 0.0025), 2),
        "latency_ms": 0,
    }


def _phase_evidence(
    request: BridgeSubmitRequest,
    *,
    phase: str,
    status: str,
    bridge_ref: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **_request_evidence(request),
        "phase": phase,
        "status": status,
        "bridge_ref": bridge_ref,
        "dry_run": True,
        "simulated": True,
        "synthetic": True,
        "requires_secret": False,
        "network_calls": 0,
        "adapter_capabilities": list(BRIDGE_SUBMIT_CAPABILITIES),
        **dict(extra or {}),
    }


def _request_evidence(request: BridgeSubmitRequest) -> dict[str, Any]:
    return {
        "opportunity_id": int(request.opportunity_id),
        "route_id": int(request.route_id),
        "run_id": int(request.run_id),
        "step_key": str(request.step_key),
        "route_type": str(request.route_type),
        "source_chain": str(request.source_chain).upper(),
        "destination_chain": str(request.destination_chain).upper(),
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


def _simulated_status(request: BridgeSubmitRequest, *, phase: str, fallback: str) -> str:
    phase_keys = {
        "quote": ("quote_status_by_step", "quote_status"),
        "build": ("build_status_by_step", "build_status"),
        "submit": ("submit_status_by_step", "submit_status"),
        "status": ("status_by_step", "status"),
        "reconcile": ("reconcile_status_by_step", "reconcile_status", "status_by_step", "status"),
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
    direct = payload.get("bridge_simulation")
    if isinstance(direct, Mapping):
        payloads.append(direct)
    route_payload = payload.get("route_payload")
    if isinstance(route_payload, Mapping):
        route_simulation = route_payload.get("bridge_simulation")
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
    if status in BRIDGE_OUTCOME_STATUSES:
        return status
    return ""


def _terminal(status: str) -> bool:
    return status in {"success", "partial", "failed"}


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
