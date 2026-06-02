from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from arbitrage.store import ArbitrageStore


IDENTITY_VERIFIED = "VERIFIED"
IDENTITY_UNKNOWN = "UNKNOWN"
IDENTITY_AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class NormalizedIdentity:
    asset_id: int | None
    market_id: int | None
    identity_status: str
    warning_reasons: tuple[str, ...]
    executable: bool
    token_id: int | None = None
    venue_id: int | None = None
    venue_code: str = ""
    venue_type: str = ""
    market_symbol: str = ""
    chain_id: str = ""
    chain_code: str = ""
    contract_address: str = ""
    bridge_group: str = ""
    error_code: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "market_id": self.market_id,
            "identity_status": self.identity_status,
            "warning_reasons": list(self.warning_reasons),
            "executable": self.executable,
            "token_id": self.token_id,
            "venue_id": self.venue_id,
            "venue_code": self.venue_code,
            "venue_type": self.venue_type,
            "market_symbol": self.market_symbol,
            "chain_id": self.chain_id,
            "chain_code": self.chain_code,
            "contract_address": self.contract_address,
            "bridge_group": self.bridge_group,
            "error_code": self.error_code,
            "evidence": dict(self.evidence),
        }


class IdentityNormalizer:
    """Resolve raw collector rows into identities that detectors can trust."""

    def __init__(self, store: ArbitrageStore):
        self.store = store

    def normalize_market(
        self,
        market_id: int,
        *,
        token_chain_id: str | None = None,
        token_contract_address: str | None = None,
    ) -> NormalizedIdentity:
        market = self._fetch_market(market_id)
        if market is None:
            return self._blocked(
                error_code="unknown_market_id",
                identity_status=IDENTITY_UNKNOWN,
                market_id=int(market_id),
                evidence={"market_id": int(market_id)},
            )

        venue_type = _clean_code(market.get("venue_type"))
        market_type = _clean_code(market.get("market_type"))
        if venue_type == "DEX" or market_type == "DEX_POOL":
            return self._normalize_dex_market(
                market,
                token_chain_id=token_chain_id,
                token_contract_address=token_contract_address,
            )
        if venue_type == "CEX" or market_type.startswith("CEX"):
            return self.normalize_cex_market(
                str(market.get("venue_code") or ""),
                str(market.get("market_symbol") or ""),
                expected_market_id=int(market["id"]),
            )

        return self._blocked(
            error_code="unsupported_market_type",
            identity_status=IDENTITY_UNKNOWN,
            asset_id=int(market["asset_id"]),
            market_id=int(market["id"]),
            venue_id=int(market["venue_id"]),
            venue_code=str(market.get("venue_code") or ""),
            venue_type=str(market.get("venue_type") or ""),
            market_symbol=str(market.get("market_symbol") or ""),
            evidence={
                "market_type": str(market.get("market_type") or ""),
                "venue_type": str(market.get("venue_type") or ""),
            },
        )

    def normalize_onchain_token(
        self,
        *,
        chain_id: str,
        contract_address: str,
        expected_asset_id: int | None = None,
        market_id: int | None = None,
        venue_id: int | None = None,
        venue_code: str = "",
        venue_type: str = "",
        market_symbol: str = "",
    ) -> NormalizedIdentity:
        chain_id = str(chain_id or "").strip()
        contract_address = _normalize_address(contract_address)
        if not chain_id:
            return self._blocked(
                error_code="missing_chain_id",
                identity_status=IDENTITY_UNKNOWN,
                asset_id=expected_asset_id,
                market_id=market_id,
                venue_id=venue_id,
                venue_code=venue_code,
                venue_type=venue_type,
                market_symbol=market_symbol,
                contract_address=contract_address,
            )
        if not contract_address:
            return self._blocked(
                error_code="missing_token_contract_address",
                identity_status=IDENTITY_UNKNOWN,
                asset_id=expected_asset_id,
                market_id=market_id,
                venue_id=venue_id,
                venue_code=venue_code,
                venue_type=venue_type,
                market_symbol=market_symbol,
                chain_id=chain_id,
            )

        rows = self._fetch_tokens(chain_id=chain_id, contract_address=contract_address)
        evidence = {"chain_id": chain_id, "contract_address": contract_address}
        if not rows:
            return self._blocked(
                error_code="unknown_onchain_token_identity",
                identity_status=IDENTITY_UNKNOWN,
                asset_id=expected_asset_id,
                market_id=market_id,
                venue_id=venue_id,
                venue_code=venue_code,
                venue_type=venue_type,
                market_symbol=market_symbol,
                chain_id=chain_id,
                contract_address=contract_address,
                evidence=evidence,
            )
        if len(rows) > 1:
            return self._blocked(
                error_code="ambiguous_onchain_token_identity",
                identity_status=IDENTITY_AMBIGUOUS,
                asset_id=expected_asset_id,
                market_id=market_id,
                venue_id=venue_id,
                venue_code=venue_code,
                venue_type=venue_type,
                market_symbol=market_symbol,
                chain_id=chain_id,
                contract_address=contract_address,
                evidence={**evidence, "token_ids": [int(row["id"]) for row in rows]},
            )

        token = rows[0]
        token_asset_id = int(token["asset_id"])
        if expected_asset_id is not None and token_asset_id != int(expected_asset_id):
            return self._blocked(
                error_code="asset_identity_mismatch",
                identity_status=IDENTITY_UNKNOWN,
                asset_id=int(expected_asset_id),
                market_id=market_id,
                token_id=int(token["id"]),
                venue_id=venue_id,
                venue_code=venue_code,
                venue_type=venue_type,
                market_symbol=market_symbol,
                chain_id=chain_id,
                chain_code=str(token.get("chain_code") or ""),
                contract_address=contract_address,
                evidence={
                    **evidence,
                    "expected_asset_id": int(expected_asset_id),
                    "token_asset_id": token_asset_id,
                },
            )

        return NormalizedIdentity(
            asset_id=token_asset_id,
            market_id=market_id,
            identity_status=IDENTITY_VERIFIED,
            warning_reasons=(),
            executable=True,
            token_id=int(token["id"]),
            venue_id=venue_id,
            venue_code=venue_code,
            venue_type=venue_type,
            market_symbol=market_symbol,
            chain_id=chain_id,
            chain_code=str(token.get("chain_code") or ""),
            contract_address=contract_address,
            bridge_group=str(token.get("bridge_group") or ""),
            evidence={**evidence, "bridge_group": str(token.get("bridge_group") or "")},
        )

    def normalize_cex_market(
        self,
        venue_code: str,
        market_symbol: str,
        *,
        expected_market_id: int | None = None,
    ) -> NormalizedIdentity:
        venue_code = _clean_code(venue_code)
        market_symbol = str(market_symbol or "").strip().upper()
        evidence = {"venue_code": venue_code, "market_symbol": market_symbol}
        if not venue_code:
            return self._blocked(
                error_code="missing_venue_code",
                identity_status=IDENTITY_UNKNOWN,
                market_id=expected_market_id,
                market_symbol=market_symbol,
                evidence=evidence,
            )
        if not market_symbol:
            return self._blocked(
                error_code="missing_cex_market_symbol",
                identity_status=IDENTITY_UNKNOWN,
                market_id=expected_market_id,
                venue_code=venue_code,
                evidence=evidence,
            )

        rows = self._fetch_cex_markets(venue_code=venue_code, market_symbol=market_symbol)
        if not rows:
            return self._blocked(
                error_code="unknown_cex_market_identity",
                identity_status=IDENTITY_UNKNOWN,
                market_id=expected_market_id,
                venue_code=venue_code,
                market_symbol=market_symbol,
                evidence=evidence,
            )
        if expected_market_id is not None and all(int(row["id"]) != int(expected_market_id) for row in rows):
            return self._blocked(
                error_code="cex_market_identity_mismatch",
                identity_status=IDENTITY_UNKNOWN,
                market_id=int(expected_market_id),
                venue_code=venue_code,
                market_symbol=market_symbol,
                evidence={**evidence, "matched_market_ids": [int(row["id"]) for row in rows]},
            )
        if len(rows) > 1:
            return self._blocked(
                error_code="ambiguous_cex_market_identity",
                identity_status=IDENTITY_AMBIGUOUS,
                market_id=expected_market_id,
                venue_code=venue_code,
                market_symbol=market_symbol,
                evidence={**evidence, "market_ids": [int(row["id"]) for row in rows]},
            )

        market = rows[0]
        return NormalizedIdentity(
            asset_id=int(market["asset_id"]),
            market_id=int(market["id"]),
            identity_status=IDENTITY_VERIFIED,
            warning_reasons=(),
            executable=True,
            venue_id=int(market["venue_id"]),
            venue_code=str(market["venue_code"]),
            venue_type=str(market["venue_type"]),
            market_symbol=str(market["market_symbol"]),
            chain_code=str(market.get("chain_code") or ""),
            evidence=evidence,
        )

    def _normalize_dex_market(
        self,
        market: Mapping[str, Any],
        *,
        token_chain_id: str | None,
        token_contract_address: str | None,
    ) -> NormalizedIdentity:
        payload = _loads_json_object(str(market.get("payload_json") or "{}"))
        chain_id = str(token_chain_id or payload.get("chain_id") or "").strip()
        contract_address = str(
            token_contract_address
            or payload.get("token_contract_address")
            or payload.get("base_token_address")
            or payload.get("contract_address")
            or ""
        )
        return self.normalize_onchain_token(
            chain_id=chain_id,
            contract_address=contract_address,
            expected_asset_id=int(market["asset_id"]),
            market_id=int(market["id"]),
            venue_id=int(market["venue_id"]),
            venue_code=str(market.get("venue_code") or ""),
            venue_type=str(market.get("venue_type") or ""),
            market_symbol=str(market.get("market_symbol") or ""),
        )

    def _fetch_market(self, market_id: int) -> dict[str, Any] | None:
        with self.store.conn() as conn:
            row = conn.execute(
                """
                SELECT m.*, v.venue_code, v.venue_type
                FROM arb_markets m
                JOIN arb_venues v ON v.id = m.venue_id
                WHERE m.id = ?
                """,
                (int(market_id),),
            ).fetchone()
            return dict(row) if row else None

    def _fetch_tokens(self, *, chain_id: str, contract_address: str) -> list[dict[str, Any]]:
        with self.store.conn() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT t.*, a.symbol
                    FROM arb_tokens t
                    JOIN arb_assets a ON a.id = t.asset_id
                    WHERE t.chain_id = ?
                      AND LOWER(t.contract_address) = LOWER(?)
                    ORDER BY t.id
                    """,
                    (chain_id, contract_address),
                ).fetchall()
            ]

    def _fetch_cex_markets(self, *, venue_code: str, market_symbol: str) -> list[dict[str, Any]]:
        with self.store.conn() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT m.*, v.venue_code, v.venue_type
                    FROM arb_markets m
                    JOIN arb_venues v ON v.id = m.venue_id
                    WHERE UPPER(v.venue_code) = ?
                      AND UPPER(m.market_symbol) = ?
                      AND (UPPER(v.venue_type) = 'CEX' OR UPPER(m.market_type) LIKE 'CEX%')
                    ORDER BY m.id
                    """,
                    (venue_code, market_symbol),
                ).fetchall()
            ]

    def _blocked(
        self,
        *,
        error_code: str,
        identity_status: str,
        asset_id: int | None = None,
        market_id: int | None = None,
        token_id: int | None = None,
        venue_id: int | None = None,
        venue_code: str = "",
        venue_type: str = "",
        market_symbol: str = "",
        chain_id: str = "",
        chain_code: str = "",
        contract_address: str = "",
        bridge_group: str = "",
        evidence: Mapping[str, Any] | None = None,
    ) -> NormalizedIdentity:
        payload = {
            "error_code": error_code,
            "identity_status": identity_status,
            "asset_id": asset_id,
            "market_id": market_id,
            "token_id": token_id,
            "venue_id": venue_id,
            "venue_code": venue_code,
            "venue_type": venue_type,
            "market_symbol": market_symbol,
            "chain_id": chain_id,
            "chain_code": chain_code,
            "contract_address": contract_address,
            "bridge_group": bridge_group,
            "evidence": dict(evidence or {}),
        }
        self.store.append_dead_letter(
            reason="identity_normalization",
            deadletter_key=_deadletter_key(error_code, payload),
            error_code=error_code,
            retryable=False,
            payload=payload,
        )
        return NormalizedIdentity(
            asset_id=asset_id,
            market_id=market_id,
            identity_status=identity_status,
            warning_reasons=(error_code,),
            executable=False,
            token_id=token_id,
            venue_id=venue_id,
            venue_code=venue_code,
            venue_type=venue_type,
            market_symbol=market_symbol,
            chain_id=chain_id,
            chain_code=chain_code,
            contract_address=contract_address,
            bridge_group=bridge_group,
            error_code=error_code,
            evidence=evidence or {},
        )


def normalize_market_identity(
    store: ArbitrageStore,
    market_id: int,
    *,
    token_chain_id: str | None = None,
    token_contract_address: str | None = None,
) -> NormalizedIdentity:
    return IdentityNormalizer(store).normalize_market(
        market_id,
        token_chain_id=token_chain_id,
        token_contract_address=token_contract_address,
    )


def _clean_code(value: object) -> str:
    return str(value or "").strip().upper()


def _normalize_address(value: object) -> str:
    return str(value or "").strip().lower()


def _loads_json_object(raw: str) -> dict[str, Any]:
    try:
        loaded = json.loads(raw)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _deadletter_key(error_code: str, payload: Mapping[str, Any]) -> str:
    material = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"identity_normalization:{error_code}:{digest}"
