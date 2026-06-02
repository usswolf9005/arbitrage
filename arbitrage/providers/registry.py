from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .base import (
    ProviderSpec,
    ProviderStatus,
    SUPPORTED_AUTH_TYPES,
    SUPPORTED_CAPABILITY_SET,
    SUPPORTED_PROVIDER_KINDS,
    normalize_capability,
)
from .secrets import EnvSecretResolver


DEFAULT_PROVIDER_REGISTRY_PATH = Path(__file__).with_name("provider_registry.json")
_PROVIDER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ProviderRegistry:
    def __init__(
        self,
        specs: Sequence[ProviderSpec] | None = None,
        *,
        secret_resolver: EnvSecretResolver | None = None,
    ) -> None:
        self._specs = tuple(load_provider_specs() if specs is None else specs)
        self._validate_unique_provider_keys(self._specs)
        self._specs_by_key = {spec.provider_key: spec for spec in self._specs}
        self._secret_resolver = secret_resolver if secret_resolver is not None else EnvSecretResolver()
        self._statuses_by_key = {
            spec.provider_key: self._resolve_status(spec)
            for spec in self._specs
        }

    @classmethod
    def from_path(
        cls,
        path: str | Path = DEFAULT_PROVIDER_REGISTRY_PATH,
        *,
        secret_resolver: EnvSecretResolver | None = None,
    ) -> "ProviderRegistry":
        return cls(load_provider_specs(path), secret_resolver=secret_resolver)

    def providers_for(self, capability: str) -> tuple[ProviderSpec, ...]:
        normalized_capability = normalize_capability(capability)
        if capability not in SUPPORTED_CAPABILITY_SET and normalized_capability not in SUPPORTED_CAPABILITY_SET:
            return ()

        providers = [
            spec
            for spec in self._specs
            if _spec_has_capability(spec, normalized_capability) and self._statuses_by_key[spec.provider_key].enabled
        ]
        return tuple(sorted(providers, key=lambda spec: (-spec.priority, spec.provider_key)))

    def spec_for(self, provider_key: str) -> ProviderSpec:
        try:
            return self._specs_by_key[provider_key]
        except KeyError as exc:
            raise KeyError(f"unknown provider_key: {provider_key}") from exc

    def status_for(self, provider_key: str) -> ProviderStatus:
        try:
            return self._statuses_by_key[provider_key]
        except KeyError as exc:
            raise KeyError(f"unknown provider_key: {provider_key}") from exc

    def all_specs(self) -> tuple[ProviderSpec, ...]:
        return tuple(self._specs)

    def all_statuses(self) -> tuple[ProviderStatus, ...]:
        return tuple(
            self._statuses_by_key[provider_key]
            for provider_key in sorted(self._statuses_by_key)
        )

    def _resolve_status(self, spec: ProviderSpec) -> ProviderStatus:
        if not spec.enabled_by_default:
            return ProviderStatus(
                provider_key=spec.provider_key,
                enabled=False,
                reason="disabled_by_default",
            )

        resolution = self._secret_resolver.resolve(spec.required_env)
        if not resolution.satisfied:
            reason = ",".join(resolution.diagnostics) or "missing_required_env"
            return ProviderStatus(
                provider_key=spec.provider_key,
                enabled=False,
                reason=reason,
                missing_env=resolution.missing_env,
                diagnostics=resolution.diagnostics,
            )

        return ProviderStatus(
            provider_key=spec.provider_key,
            enabled=True,
            reason="enabled",
            diagnostics=resolution.diagnostics,
        )

    @staticmethod
    def _validate_unique_provider_keys(specs: tuple[ProviderSpec, ...]) -> None:
        seen: set[str] = set()
        duplicates: list[str] = []
        for spec in specs:
            if spec.provider_key in seen:
                duplicates.append(spec.provider_key)
            seen.add(spec.provider_key)
        if duplicates:
            duplicate_list = ", ".join(sorted(set(duplicates)))
            raise ValueError(f"provider registry contains duplicate provider_key: {duplicate_list}")


def load_provider_specs(path: str | Path = DEFAULT_PROVIDER_REGISTRY_PATH) -> tuple[ProviderSpec, ...]:
    registry_path = Path(path)
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"provider registry JSON is invalid: {exc.msg}") from exc

    if isinstance(raw, Mapping):
        providers = raw.get("providers")
    elif isinstance(raw, list):
        providers = raw
    else:
        raise ValueError("provider registry must be an object with a providers list")

    if not isinstance(providers, list):
        raise ValueError("provider registry field 'providers' must be a list")

    specs = tuple(validate_provider_spec(item) for item in providers)
    seen: set[str] = set()
    duplicates: list[str] = []
    for spec in specs:
        if spec.provider_key in seen:
            duplicates.append(spec.provider_key)
        seen.add(spec.provider_key)
    if duplicates:
        duplicate_list = ", ".join(sorted(set(duplicates)))
        raise ValueError(f"provider registry contains duplicate provider_key: {duplicate_list}")
    return specs


def validate_provider_spec(raw: Mapping[str, Any]) -> ProviderSpec:
    if not isinstance(raw, Mapping):
        raise ValueError("provider metadata must be an object")

    provider_key = _required_str(raw, "provider_key", provider_key="<unknown>")
    if not _PROVIDER_KEY_RE.fullmatch(provider_key):
        raise ValueError(
            f"provider '{provider_key}' field 'provider_key' must use lowercase letters, digits, and underscores"
        )

    kind = _required_str(raw, "kind", provider_key=provider_key)
    if kind not in SUPPORTED_PROVIDER_KINDS:
        allowed = ", ".join(SUPPORTED_PROVIDER_KINDS)
        raise ValueError(f"provider '{provider_key}' has unsupported kind '{kind}'; supported kinds: {allowed}")

    capabilities = _required_str_tuple(raw, "capabilities", provider_key=provider_key, allow_empty=False)
    duplicate_capabilities = _duplicates(capabilities)
    if duplicate_capabilities:
        raise ValueError(
            f"provider '{provider_key}' field 'capabilities' contains duplicates: "
            f"{', '.join(duplicate_capabilities)}"
        )
    unsupported_capabilities = sorted(set(capabilities) - SUPPORTED_CAPABILITY_SET)
    if unsupported_capabilities:
        raise ValueError(
            f"provider '{provider_key}' declares unsupported capability: "
            f"{', '.join(unsupported_capabilities)}"
        )

    auth_type = _required_str(raw, "auth_type", provider_key=provider_key)
    if auth_type not in SUPPORTED_AUTH_TYPES:
        allowed = ", ".join(SUPPORTED_AUTH_TYPES)
        raise ValueError(f"provider '{provider_key}' has unsupported auth_type '{auth_type}'; supported: {allowed}")

    required_env = _required_str_tuple(raw, "required_env", provider_key=provider_key, allow_empty=True)
    duplicate_env = _duplicates(required_env)
    if duplicate_env:
        raise ValueError(f"provider '{provider_key}' field 'required_env' contains duplicates: {', '.join(duplicate_env)}")
    invalid_env = [name for name in required_env if not _ENV_NAME_RE.fullmatch(name)]
    if invalid_env:
        raise ValueError(
            f"provider '{provider_key}' field 'required_env' contains invalid env var names: "
            f"{', '.join(invalid_env)}"
        )
    if auth_type == "public" and required_env:
        raise ValueError(f"provider '{provider_key}' has auth_type public but declares required_env")
    if auth_type == "api_key" and not required_env:
        raise ValueError(f"provider '{provider_key}' has auth_type api_key but no required_env")

    priority = _required_int(raw, "priority", provider_key=provider_key)
    enabled_by_default = _required_bool(raw, "enabled_by_default", provider_key=provider_key)
    display_name = _optional_str(raw, "display_name", provider_key=provider_key)
    docs_url = _optional_str(raw, "docs_url", provider_key=provider_key)

    return ProviderSpec(
        provider_key=provider_key,
        kind=kind,
        capabilities=capabilities,
        auth_type=auth_type,
        required_env=required_env,
        priority=priority,
        enabled_by_default=enabled_by_default,
        display_name=display_name,
        docs_url=docs_url,
    )


def _required_str(raw: Mapping[str, Any], field: str, *, provider_key: str) -> str:
    if field not in raw:
        raise ValueError(f"provider '{provider_key}' missing required field '{field}'")
    value = raw[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider '{provider_key}' field '{field}' must be a non-empty string")
    return value.strip()


def _optional_str(raw: Mapping[str, Any], field: str, *, provider_key: str) -> str:
    value = raw.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"provider '{provider_key}' field '{field}' must be a string")
    return value.strip()


def _required_str_tuple(
    raw: Mapping[str, Any], field: str, *, provider_key: str, allow_empty: bool
) -> tuple[str, ...]:
    if field not in raw:
        raise ValueError(f"provider '{provider_key}' missing required field '{field}'")
    value = raw[field]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"provider '{provider_key}' field '{field}' must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"provider '{provider_key}' field '{field}' must contain only non-empty strings")
        items.append(item.strip())
    if not allow_empty and not items:
        raise ValueError(f"provider '{provider_key}' field '{field}' must not be empty")
    return tuple(items)


def _required_int(raw: Mapping[str, Any], field: str, *, provider_key: str) -> int:
    if field not in raw:
        raise ValueError(f"provider '{provider_key}' missing required field '{field}'")
    value = raw[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"provider '{provider_key}' field '{field}' must be an integer")
    return value


def _required_bool(raw: Mapping[str, Any], field: str, *, provider_key: str) -> bool:
    if field not in raw:
        raise ValueError(f"provider '{provider_key}' missing required field '{field}'")
    value = raw[field]
    if not isinstance(value, bool):
        raise ValueError(f"provider '{provider_key}' field '{field}' must be a boolean")
    return value


def _duplicates(values: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def _spec_has_capability(spec: ProviderSpec, capability: str) -> bool:
    normalized = normalize_capability(capability)
    return any(normalize_capability(item) == normalized for item in spec.capabilities)
