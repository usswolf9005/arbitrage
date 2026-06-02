from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ..collectors.base import (
    ProviderPayloadError,
    ensure_payload_mapping,
    provider_payload_error,
    redact_provider_text,
)
from .base import (
    CAPABILITY_CEX_ORDERBOOK,
    CAPABILITY_DEX_POOL,
    CAPABILITY_FX_RATE,
    CAPABILITY_KRW_ORDERBOOK,
    CAPABILITY_RPC_FRESHNESS,
    ProviderSpec,
    READ_ONLY_HTTP_V1_CAPABILITY_SET,
    READ_ONLY_HTTP_V1_CAPABILITIES,
    normalize_capability,
)
from .registry import ProviderRegistry
from .secrets import EnvSecretResolver


_DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
_BINANCE_BASE_URL = "https://api.binance.com"
_OKX_BASE_URL = "https://www.okx.com"
_BYBIT_BASE_URL = "https://api.bybit.com"
_UPBIT_BASE_URL = "https://api.upbit.com"
_BITHUMB_BASE_URL = "https://api.bithumb.com"

_READ_ONLY_HTTP_ADAPTERS = frozenset(
    {
        ("dexscreener", CAPABILITY_DEX_POOL),
        ("binance_public", CAPABILITY_CEX_ORDERBOOK),
        ("okx_public", CAPABILITY_CEX_ORDERBOOK),
        ("bybit_public", CAPABILITY_CEX_ORDERBOOK),
        ("upbit_public", CAPABILITY_CEX_ORDERBOOK),
        ("upbit_public", CAPABILITY_KRW_ORDERBOOK),
        ("upbit_public", CAPABILITY_FX_RATE),
        ("bithumb_public", CAPABILITY_CEX_ORDERBOOK),
        ("bithumb_public", CAPABILITY_KRW_ORDERBOOK),
        ("bithumb_public", CAPABILITY_FX_RATE),
        ("alchemy", CAPABILITY_RPC_FRESHNESS),
    }
)
_PRIVATE_ENDPOINT_FRAGMENTS = (
    "/orders",
    "/withdraw",
    "/private",
    "/wallet",
    "/swap",
    "/bridge",
    "/sign",
    "submit",
)


class HttpJsonClient(Protocol):
    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 5.0,
    ) -> Any:
        ...

    def post_json(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 5.0,
    ) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class HttpRequestSpec:
    method: str
    url: str
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        method = str(self.method or "").strip().upper()
        if method not in {"GET", "POST"}:
            raise ValueError(f"unsupported read-only HTTP method: {self.method}")
        object.__setattr__(self, "method", method)
        _assert_read_only_url(self.url)


@dataclass(frozen=True, slots=True)
class HttpAdapterStatus:
    provider_key: str
    capability: str
    enabled: bool
    reason: str
    missing_env: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderAdapterDisabled(RuntimeError):
    def __init__(self, status: HttpAdapterStatus) -> None:
        self.status = status
        super().__init__(
            f"{status.provider_key}:{status.capability}:provider_disabled:{status.reason}"
        )

    def to_dict(self) -> dict[str, Any]:
        return self.status.to_dict()


class ProviderHttpTimeout(TimeoutError):
    def __init__(self, provider_key: str, capability: str, url: str) -> None:
        self.provider_key = provider_key
        self.capability = capability
        super().__init__(f"{provider_key}:{capability}:provider_timeout:{url}")


class ProviderHttpError(RuntimeError):
    def __init__(self, provider_key: str, capability: str, message: str) -> None:
        self.provider_key = provider_key
        self.capability = capability
        super().__init__(f"{provider_key}:{capability}:provider_http_error:{message}")


class UrllibJsonHttpClient:
    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 5.0,
    ) -> Any:
        request_url = _url_with_params(url, params)
        request = urllib.request.Request(request_url, headers=dict(headers or {}), method="GET")
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return _decode_json(response.read())

    def post_json(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 5.0,
    ) -> Any:
        request_headers = {"Content-Type": "application/json", **dict(headers or {})}
        body = json.dumps(dict(json_body or {}), separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return _decode_json(response.read())


class ReadOnlyHttpAdapterCatalog:
    def __init__(
        self,
        *,
        registry: ProviderRegistry | None = None,
        http_client: HttpJsonClient | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self.environ = environ if environ is not None else os.environ
        self.registry = registry or ProviderRegistry(secret_resolver=EnvSecretResolver(self.environ))
        self.http_client = http_client or UrllibJsonHttpClient()
        self.timeout_s = float(timeout_s)

    def adapter_for(self, provider_key: str, capability: str) -> "ReadOnlyHttpProviderAdapter":
        normalized_capability = _normalize_read_only_capability(capability)
        spec = self.registry.spec_for(provider_key)
        if not _spec_supports_capability(spec, normalized_capability):
            raise KeyError(f"provider '{provider_key}' does not support capability '{normalized_capability}'")
        if not _adapter_implemented(provider_key, normalized_capability):
            raise KeyError(f"provider '{provider_key}' has no HTTP adapter for '{normalized_capability}'")
        return ReadOnlyHttpProviderAdapter(
            spec=spec,
            capability=normalized_capability,
            status=self._status_for(spec, normalized_capability),
            http_client=self.http_client,
            environ=self.environ,
            timeout_s=self.timeout_s,
        )

    def adapters_for(self, capability: str) -> tuple["ReadOnlyHttpProviderAdapter", ...]:
        normalized_capability = _normalize_read_only_capability(capability)
        adapters: list[ReadOnlyHttpProviderAdapter] = []
        for spec in self.registry.providers_for(normalized_capability):
            if not _adapter_implemented(spec.provider_key, normalized_capability):
                continue
            adapter = self.adapter_for(spec.provider_key, normalized_capability)
            if adapter.status.enabled:
                adapters.append(adapter)
        return tuple(adapters)

    def fetch_payload(self, job: Mapping[str, Any]) -> dict[str, Any]:
        provider_key = str(job.get("provider_key") or "").strip()
        capability = str(job.get("capability") or "").strip()
        if not provider_key:
            raise ValueError("provider_key_required")
        return self.adapter_for(provider_key, capability).fetch_payload(job)

    def adapter_statuses(self, capability: str | None = None) -> tuple[HttpAdapterStatus, ...]:
        normalized_filter = _normalize_read_only_capability(capability) if capability else ""
        statuses: list[HttpAdapterStatus] = []
        for spec in self.registry.all_specs():
            capabilities = tuple(
                item
                for item in READ_ONLY_HTTP_V1_CAPABILITIES
                if _spec_supports_capability(spec, item) and (not normalized_filter or item == normalized_filter)
            )
            for item in capabilities:
                statuses.append(self._status_for(spec, item))
        return tuple(sorted(statuses, key=lambda status: (status.capability, status.provider_key)))

    def _status_for(self, spec: ProviderSpec, capability: str) -> HttpAdapterStatus:
        normalized_capability = _normalize_read_only_capability(capability)
        provider_status = self.registry.status_for(spec.provider_key)
        diagnostics = tuple(redact_provider_text(item) for item in provider_status.diagnostics)
        if not _adapter_implemented(spec.provider_key, normalized_capability):
            return HttpAdapterStatus(
                provider_key=spec.provider_key,
                capability=normalized_capability,
                enabled=False,
                reason="adapter_not_implemented",
                missing_env=provider_status.missing_env,
                diagnostics=diagnostics,
            )
        if not provider_status.enabled:
            return HttpAdapterStatus(
                provider_key=spec.provider_key,
                capability=normalized_capability,
                enabled=False,
                reason=provider_status.reason,
                missing_env=provider_status.missing_env,
                diagnostics=diagnostics,
            )
        return HttpAdapterStatus(
            provider_key=spec.provider_key,
            capability=normalized_capability,
            enabled=True,
            reason="enabled",
            diagnostics=diagnostics,
        )


class ReadOnlyHttpProviderAdapter:
    def __init__(
        self,
        *,
        spec: ProviderSpec,
        capability: str,
        status: HttpAdapterStatus,
        http_client: HttpJsonClient,
        environ: Mapping[str, str],
        timeout_s: float,
    ) -> None:
        self.spec = spec
        self.provider_key = spec.provider_key
        self.capability = _normalize_read_only_capability(capability)
        self.status = status
        self.http_client = http_client
        self.timeout_s = float(timeout_s)
        self._secret_values = {
            env_name: str(environ.get(env_name) or "")
            for env_name in spec.required_env
            if environ.get(env_name)
        }

    def fetch_payload(self, job: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self.status.enabled:
            raise ProviderAdapterDisabled(self.status)

        request_params = _request_params(job or {})
        request = _build_request(self, request_params)
        redacted_url = _redact_with_secrets(request.url, self._secret_values.values())
        try:
            if request.method == "POST":
                raw_payload = self.http_client.post_json(
                    request.url,
                    json_body=request.json_body,
                    headers=request.headers,
                    timeout_s=self.timeout_s,
                )
            else:
                raw_payload = self.http_client.get_json(
                    request.url,
                    params=request.params,
                    headers=request.headers,
                    timeout_s=self.timeout_s,
                )
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderHttpTimeout(self.provider_key, self.capability, redacted_url) from exc
        except Exception as exc:
            message = _redact_with_secrets(str(exc), self._secret_values.values())
            raise ProviderHttpError(self.provider_key, self.capability, message) from exc

        return _normalize_response(self, raw_payload, request_params)


def _build_request(adapter: ReadOnlyHttpProviderAdapter, params: Mapping[str, Any]) -> HttpRequestSpec:
    provider_key = adapter.provider_key
    capability = adapter.capability
    if (provider_key, capability) == ("dexscreener", CAPABILITY_DEX_POOL):
        chain_id = _required_text_param(params, ("chain_id", "chainId", "chain"), adapter, "chain_id")
        pair_id = _required_text_param(
            params,
            ("pair_id", "pairId", "pair_address", "pairAddress", "pool_address", "poolAddress"),
            adapter,
            "pair_id",
        )
        return HttpRequestSpec(
            method="GET",
            url=f"{_DEXSCREENER_BASE_URL}/latest/dex/pairs/{_url_quote(chain_id)}/{_url_quote(pair_id)}",
        )

    if (provider_key, capability) == ("binance_public", CAPABILITY_CEX_ORDERBOOK):
        symbol = _compact_market(_required_text_param(params, ("symbol", "market"), adapter, "symbol"))
        limit = _int_param(params, ("limit", "depth"), default=50)
        return HttpRequestSpec(
            method="GET",
            url=f"{_BINANCE_BASE_URL}/api/v3/depth",
            params={"symbol": symbol, "limit": limit},
        )

    if (provider_key, capability) == ("okx_public", CAPABILITY_CEX_ORDERBOOK):
        inst_id = _dash_market(_required_text_param(params, ("inst_id", "instId", "symbol", "market"), adapter, "inst_id"))
        return HttpRequestSpec(
            method="GET",
            url=f"{_OKX_BASE_URL}/api/v5/market/books",
            params={"instId": inst_id, "sz": _int_param(params, ("limit", "depth", "sz"), default=50)},
        )

    if (provider_key, capability) == ("bybit_public", CAPABILITY_CEX_ORDERBOOK):
        symbol = _compact_market(_required_text_param(params, ("symbol", "market"), adapter, "symbol"))
        return HttpRequestSpec(
            method="GET",
            url=f"{_BYBIT_BASE_URL}/v5/market/orderbook",
            params={"category": "spot", "symbol": symbol, "limit": _int_param(params, ("limit", "depth"), default=50)},
        )

    if provider_key == "upbit_public" and capability in {CAPABILITY_CEX_ORDERBOOK, CAPABILITY_KRW_ORDERBOOK}:
        market = _upbit_market(_required_text_param(params, ("market", "symbol"), adapter, "market"))
        return HttpRequestSpec(
            method="GET",
            url=f"{_UPBIT_BASE_URL}/v1/orderbook",
            params={"markets": market, "count": _int_param(params, ("limit", "depth", "count"), default=30)},
            headers={"accept": "application/json"},
        )

    if provider_key == "bithumb_public" and capability in {CAPABILITY_CEX_ORDERBOOK, CAPABILITY_KRW_ORDERBOOK}:
        base, quote = _market_base_quote(_required_text_param(params, ("market", "symbol"), adapter, "market"))
        return HttpRequestSpec(method="GET", url=f"{_BITHUMB_BASE_URL}/public/orderbook/{base}_{quote}")

    if (provider_key, capability) == ("upbit_public", CAPABILITY_FX_RATE):
        market = _upbit_market(str(params.get("market") or params.get("symbol") or "KRW-USDT"))
        return HttpRequestSpec(
            method="GET",
            url=f"{_UPBIT_BASE_URL}/v1/orderbook",
            params={"markets": market, "count": 1},
            headers={"accept": "application/json"},
        )

    if (provider_key, capability) == ("bithumb_public", CAPABILITY_FX_RATE):
        base, quote = _market_base_quote(str(params.get("market") or params.get("symbol") or "USDT-KRW"))
        return HttpRequestSpec(method="GET", url=f"{_BITHUMB_BASE_URL}/public/orderbook/{base}_{quote}")

    if (provider_key, capability) == ("alchemy", CAPABILITY_RPC_FRESHNESS):
        api_key = adapter._secret_values.get("ALCHEMY_API_KEY", "")
        network = str(params.get("network") or params.get("chain") or "eth-mainnet").strip()
        return HttpRequestSpec(
            method="POST",
            url=f"https://{_url_quote(network)}.g.alchemy.com/v2/{_url_quote(api_key)}",
            json_body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
            headers={"Content-Type": "application/json"},
        )

    raise KeyError(f"provider '{provider_key}' has no HTTP adapter for '{capability}'")


def _normalize_response(
    adapter: ReadOnlyHttpProviderAdapter,
    raw_payload: Any,
    request_params: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _redact_with_secrets(raw_payload, adapter._secret_values.values())
    observed_at_ms = _observed_at_ms(request_params)

    if adapter.capability == CAPABILITY_DEX_POOL:
        payload_map = _ensure_mapping_payload(adapter, payload)
        if "observed_at_ms" not in payload_map:
            payload_map = {**payload_map, "observed_at_ms": observed_at_ms}
        if not _has_dex_pool_payload(payload_map):
            raise _payload_error(
                adapter,
                error_code="invalid_dex_pool_payload",
                message="DEX pool response is missing pair data",
                payload=payload_map,
            )
        return payload_map

    if adapter.capability in {CAPABILITY_CEX_ORDERBOOK, CAPABILITY_KRW_ORDERBOOK}:
        return _normalize_orderbook_payload(adapter, payload, request_params, observed_at_ms)

    if adapter.capability == CAPABILITY_FX_RATE:
        return _normalize_fx_payload(adapter, payload, observed_at_ms)

    if adapter.capability == CAPABILITY_RPC_FRESHNESS:
        payload_map = _ensure_mapping_payload(adapter, payload)
        if "observed_at_ms" not in payload_map:
            payload_map = {**payload_map, "observed_at_ms": observed_at_ms}
        return payload_map

    raise ValueError(f"unsupported read-only HTTP capability: {adapter.capability}")


def _normalize_orderbook_payload(
    adapter: ReadOnlyHttpProviderAdapter,
    payload: Any,
    request_params: Mapping[str, Any],
    observed_at_ms: int,
) -> dict[str, Any]:
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return {"orderbooks": list(payload), "observed_at_ms": observed_at_ms}

    payload_map = _ensure_mapping_payload(adapter, payload)
    payload_map = {**payload_map}
    payload_map.setdefault("observed_at_ms", observed_at_ms)
    if adapter.provider_key == "binance_public":
        payload_map.setdefault("symbol", _compact_market(str(request_params.get("symbol") or request_params.get("market") or "")))
    if adapter.provider_key == "okx_public":
        payload_map.setdefault("arg", {"instId": _dash_market(str(request_params.get("inst_id") or request_params.get("symbol") or request_params.get("market") or ""))})
    if adapter.provider_key == "bybit_public" and isinstance(payload_map.get("result"), Mapping):
        result = dict(payload_map["result"])
        result.setdefault("s", _compact_market(str(request_params.get("symbol") or request_params.get("market") or "")))
        payload_map["result"] = result
    return payload_map


def _normalize_fx_payload(adapter: ReadOnlyHttpProviderAdapter, payload: Any, observed_at_ms: int) -> dict[str, Any]:
    if adapter.provider_key == "upbit_public":
        item = _first_sequence_mapping(payload)
        units = item.get("orderbook_units") if isinstance(item.get("orderbook_units"), Sequence) else ()
        first_unit = units[0] if units and isinstance(units[0], Mapping) else {}
        bid = _positive_float(first_unit.get("bid_price"))
        ask = _positive_float(first_unit.get("ask_price"))
        if bid is None or ask is None:
            raise _payload_error(
                adapter,
                error_code="invalid_fx_payload",
                message="Upbit FX response is missing bid/ask orderbook evidence",
                payload=payload,
            )
        timestamp = _optional_int(item.get("timestamp")) or observed_at_ms
        return {
            "pair": "USDT/KRW",
            "source": "upbit_public",
            "observed_at_ms": timestamp,
            "rate": (bid + ask) / 2.0,
            "stale": False,
            "evidence": {"bid": bid, "ask": ask},
        }

    if adapter.provider_key == "bithumb_public":
        payload_map = _ensure_mapping_payload(adapter, payload)
        data = payload_map.get("data") if isinstance(payload_map.get("data"), Mapping) else payload_map
        try:
            bid, ask = _bithumb_bid_ask(data)
        except ValueError as exc:
            raise _payload_error(
                adapter,
                error_code="invalid_fx_payload",
                message="Bithumb FX response is missing bid/ask orderbook evidence",
                payload=payload_map,
            ) from exc
        timestamp = _optional_int(data.get("timestamp")) or observed_at_ms
        return {
            "pair": "USDT/KRW",
            "source": "bithumb_public",
            "observed_at_ms": timestamp,
            "rate": (bid + ask) / 2.0,
            "stale": False,
            "evidence": {"bid": bid, "ask": ask},
        }

    payload_map = _ensure_mapping_payload(adapter, payload)
    payload_map.setdefault("observed_at_ms", observed_at_ms)
    return payload_map


def _first_sequence_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, Mapping):
                return item
    if isinstance(value, Mapping):
        return value
    return {}


def _bithumb_bid_ask(data: Mapping[str, Any]) -> tuple[float, float]:
    bids = data.get("bids") if isinstance(data.get("bids"), Sequence) else ()
    asks = data.get("asks") if isinstance(data.get("asks"), Sequence) else ()
    first_bid = bids[0] if bids and isinstance(bids[0], Mapping) else {}
    first_ask = asks[0] if asks and isinstance(asks[0], Mapping) else {}
    bid = _positive_float(first_bid.get("price"))
    ask = _positive_float(first_ask.get("price"))
    if bid is None or ask is None:
        raise ValueError("missing bithumb bid/ask")
    return bid, ask


def _ensure_mapping_payload(adapter: ReadOnlyHttpProviderAdapter, payload: Any) -> dict[str, Any]:
    try:
        return dict(
            ensure_payload_mapping(
                payload,
                provider_key=adapter.provider_key,
                scope_key=adapter.capability,
            )
        )
    except ProviderPayloadError as exc:
        raise _payload_error(
            adapter,
            error_code=exc.error_code,
            message=exc.message,
            payload={"received_type": type(payload).__name__},
        ) from exc


def _payload_error(
    adapter: ReadOnlyHttpProviderAdapter,
    *,
    error_code: str,
    message: str,
    payload: Any,
) -> ProviderPayloadError:
    return provider_payload_error(
        provider_key=adapter.provider_key,
        scope_key=adapter.capability,
        error_code=error_code,
        message=message,
        field_path="payload",
        payload=payload if isinstance(payload, (Mapping, Sequence)) and not isinstance(payload, (str, bytes)) else {"payload": payload},
    )


def _request_params(job: Mapping[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    nested = job.get("params")
    if isinstance(nested, Mapping):
        params.update(dict(nested))
    for key, value in job.items():
        if key not in {"params", "payload"}:
            params.setdefault(str(key), value)
    return params


def _normalize_read_only_capability(capability: str | None) -> str:
    normalized = normalize_capability(str(capability or "").strip())
    if normalized not in READ_ONLY_HTTP_V1_CAPABILITY_SET:
        raise ValueError(f"unsupported read-only HTTP capability: {capability}")
    return normalized


def _spec_supports_capability(spec: ProviderSpec, capability: str) -> bool:
    normalized = _normalize_read_only_capability(capability)
    return any(normalize_capability(item) == normalized for item in spec.capabilities)


def _adapter_implemented(provider_key: str, capability: str) -> bool:
    return (provider_key, _normalize_read_only_capability(capability)) in _READ_ONLY_HTTP_ADAPTERS


def _has_dex_pool_payload(payload: Mapping[str, Any]) -> bool:
    pairs = payload.get("pairs")
    if isinstance(pairs, Sequence) and not isinstance(pairs, (str, bytes, bytearray)):
        return any(isinstance(item, Mapping) for item in pairs)
    return isinstance(payload.get("pair"), Mapping) or isinstance(payload.get("data"), Mapping)


def _required_text_param(
    params: Mapping[str, Any],
    keys: tuple[str, ...],
    adapter: ReadOnlyHttpProviderAdapter,
    field_name: str,
) -> str:
    for key in keys:
        value = params.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    raise _payload_error(
        adapter,
        error_code="missing_adapter_request_field",
        message=f"HTTP adapter request is missing required field {field_name}",
        payload={"required_field": field_name},
    )


def _int_param(params: Mapping[str, Any], keys: tuple[str, ...], *, default: int) -> int:
    for key in keys:
        parsed = _optional_int(params.get(key))
        if parsed is not None and parsed > 0:
            return parsed
    return default


def _observed_at_ms(params: Mapping[str, Any]) -> int:
    return _optional_int(params.get("observed_at_ms")) or _optional_int(params.get("now_ms")) or int(time.time() * 1000)


def _optional_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _compact_market(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _dash_market(value: str) -> str:
    text = str(value or "").strip().upper().replace("_", "-").replace("/", "-")
    if "-" in text:
        left, right = text.split("-", 1)
        return f"{left}-{right}" if left and right else text
    compact = _compact_market(text)
    for suffix in ("USDT", "USDC", "KRW", "USD", "BTC", "ETH"):
        if compact.endswith(suffix) and len(compact) > len(suffix):
            return f"{compact[:-len(suffix)]}-{suffix}"
    return compact


def _upbit_market(value: str) -> str:
    left, right = _market_base_quote(value)
    return f"{right}-{left}"


def _market_base_quote(value: str) -> tuple[str, str]:
    text = str(value or "").strip().upper().replace("/", "-").replace("_", "-")
    if "-" in text:
        left, right = (part for part in text.split("-", 1))
        if left in {"KRW", "USDT", "USDC", "USD", "BTC", "ETH"} and right:
            return right, left
        return left, right
    compact = _compact_market(text)
    for suffix in ("USDT", "USDC", "KRW", "USD", "BTC", "ETH"):
        if compact.endswith(suffix) and len(compact) > len(suffix):
            return compact[:-len(suffix)], suffix
    return compact, "KRW"


def _url_quote(value: str) -> str:
    return urllib.parse.quote(str(value or "").strip(), safe="")


def _url_with_params(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    query = urllib.parse.urlencode(dict(params), doseq=True)
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def _decode_json(raw: bytes) -> Any:
    text = raw.decode("utf-8")
    return json.loads(text)


def _redact_with_secrets(value: Any, secret_values: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            redacted[key_text] = "<redacted>" if _is_sensitive_adapter_key(key_text) else _redact_with_secrets(item, secret_values)
        return redacted
    if isinstance(value, list):
        return [_redact_with_secrets(item, secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_with_secrets(item, secret_values) for item in value)
    if isinstance(value, str):
        text = redact_provider_text(value)
        for secret in secret_values:
            if secret and len(secret) >= 4:
                text = text.replace(secret, "<redacted>")
        return text
    return value


def _is_sensitive_adapter_key(key: str) -> bool:
    normalized = "".join(ch for ch in str(key or "").lower() if ch.isalnum())
    return normalized in {
        "accesstoken",
        "apikey",
        "authorization",
        "bearer",
        "clientsecret",
        "idtoken",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "sessiontoken",
        "token",
    }


def _assert_read_only_url(url: str) -> None:
    lowered = str(url or "").lower()
    if not lowered.startswith("https://"):
        raise ValueError("provider HTTP adapters require https URLs")
    for fragment in _PRIVATE_ENDPOINT_FRAGMENTS:
        if fragment in lowered:
            raise ValueError(f"provider HTTP adapter refused private endpoint fragment: {fragment}")
