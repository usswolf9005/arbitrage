import json
from pathlib import Path

import pytest

from arbitrage.providers.base import ProviderSpec, SUPPORTED_CAPABILITIES, SUPPORTED_CAPABILITY_SET
from arbitrage.providers.registry import ProviderRegistry, load_provider_specs, validate_provider_spec
from arbitrage.providers.secrets import EnvSecretResolver


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "arbitrage" / "providers" / "provider_registry.json"

EXPECTED_PROVIDER_KEYS = {
    "dexscreener",
    "defillama",
    "coingecko",
    "coinmarketcap",
    "alchemy",
    "etherscan",
    "upbit_public",
    "bithumb_public",
    "binance_public",
    "okx_public",
    "bybit_public",
    "zerox",
    "kyberswap",
    "lifi",
    "goplus",
    "tenderly",
}

EXPECTED_CAPABILITIES = {
    "dex_pool",
    "dex_pool_price",
    "dex_pair_search",
    "cex_orderbook",
    "krw_orderbook",
    "fx_rate",
    "rpc_freshness",
    "rpc_block_freshness",
    "swap_quote",
    "swap_build_tx",
    "bridge_quote",
    "bridge_build_tx",
    "risk_check",
    "explorer_history",
    "coin_price",
}


def _provider_spec(
    provider_key: str,
    *,
    kind: str = "coin_price",
    capabilities: tuple[str, ...] = ("coin_price",),
    priority: int = 10,
    auth_type: str = "public",
    required_env: tuple[str, ...] = (),
    enabled_by_default: bool = True,
) -> ProviderSpec:
    return ProviderSpec(
        provider_key=provider_key,
        kind=kind,
        capabilities=capabilities,
        auth_type=auth_type,
        required_env=required_env,
        priority=priority,
        enabled_by_default=enabled_by_default,
    )


def test_load_provider_specs_reads_required_metadata() -> None:
    specs = load_provider_specs(REGISTRY_PATH)
    by_key = {spec.provider_key: spec for spec in specs}

    assert EXPECTED_PROVIDER_KEYS.issubset(by_key)
    assert all(isinstance(spec, ProviderSpec) for spec in specs)
    assert by_key["dexscreener"].auth_type == "public"
    assert by_key["dexscreener"].required_env == ()
    assert by_key["coinmarketcap"].auth_type == "api_key"
    assert by_key["coinmarketcap"].required_env == ("COINMARKETCAP_API_KEY",)

    for spec in specs:
        assert spec.provider_key
        assert spec.kind
        assert spec.capabilities
        assert isinstance(spec.priority, int)
        assert isinstance(spec.enabled_by_default, bool)


def test_supported_capability_set_is_deterministic_and_complete() -> None:
    assert isinstance(SUPPORTED_CAPABILITIES, tuple)
    assert len(SUPPORTED_CAPABILITIES) == len(set(SUPPORTED_CAPABILITIES))
    assert set(SUPPORTED_CAPABILITIES) == EXPECTED_CAPABILITIES
    assert SUPPORTED_CAPABILITY_SET == frozenset(EXPECTED_CAPABILITIES)

    specs = load_provider_specs(REGISTRY_PATH)
    declared_capabilities = {capability for spec in specs for capability in spec.capabilities}
    assert declared_capabilities.issubset(SUPPORTED_CAPABILITY_SET)
    assert {
        "dex_pool",
        "dex_pool_price",
        "cex_orderbook",
        "krw_orderbook",
        "fx_rate",
        "rpc_freshness",
        "rpc_block_freshness",
        "swap_quote",
        "swap_build_tx",
        "bridge_quote",
        "bridge_build_tx",
        "risk_check",
        "explorer_history",
        "coin_price",
    }.issubset(declared_capabilities)


def test_provider_registry_contains_no_secret_values() -> None:
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    for provider in raw["providers"]:
        assert "api_key" not in provider
        assert "secret" not in provider
        assert "token" not in provider
        for env_name in provider["required_env"]:
            assert env_name == env_name.upper()
            assert " " not in env_name


def test_validate_provider_spec_accepts_valid_metadata() -> None:
    spec = validate_provider_spec(
        {
            "provider_key": "example_public",
            "kind": "dex",
            "capabilities": ["dex_pool_price"],
            "auth_type": "public",
            "required_env": [],
            "priority": 10,
            "enabled_by_default": True,
        }
    )

    assert spec == ProviderSpec(
        provider_key="example_public",
        kind="dex",
        capabilities=("dex_pool_price",),
        auth_type="public",
        required_env=(),
        priority=10,
        enabled_by_default=True,
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "missing required field 'provider_key'"),
        (
            {
                "provider_key": "Bad-Key",
                "kind": "dex",
                "capabilities": ["dex_pool_price"],
                "auth_type": "public",
                "required_env": [],
                "priority": 1,
                "enabled_by_default": True,
            },
            "field 'provider_key' must use lowercase",
        ),
        (
            {
                "provider_key": "bad_kind",
                "kind": "news",
                "capabilities": ["dex_pool_price"],
                "auth_type": "public",
                "required_env": [],
                "priority": 1,
                "enabled_by_default": True,
            },
            "unsupported kind 'news'",
        ),
        (
            {
                "provider_key": "bad_capability",
                "kind": "dex",
                "capabilities": ["not_supported"],
                "auth_type": "public",
                "required_env": [],
                "priority": 1,
                "enabled_by_default": True,
            },
            "unsupported capability",
        ),
        (
            {
                "provider_key": "bad_auth",
                "kind": "dex",
                "capabilities": ["dex_pool_price"],
                "auth_type": "oauth",
                "required_env": [],
                "priority": 1,
                "enabled_by_default": True,
            },
            "unsupported auth_type 'oauth'",
        ),
        (
            {
                "provider_key": "bad_public_env",
                "kind": "dex",
                "capabilities": ["dex_pool_price"],
                "auth_type": "public",
                "required_env": ["SHOULD_NOT_BE_REQUIRED"],
                "priority": 1,
                "enabled_by_default": True,
            },
            "auth_type public but declares required_env",
        ),
        (
            {
                "provider_key": "bad_api_key_env",
                "kind": "dex",
                "capabilities": ["dex_pool_price"],
                "auth_type": "api_key",
                "required_env": [],
                "priority": 1,
                "enabled_by_default": True,
            },
            "auth_type api_key but no required_env",
        ),
        (
            {
                "provider_key": "bad_priority",
                "kind": "dex",
                "capabilities": ["dex_pool_price"],
                "auth_type": "public",
                "required_env": [],
                "priority": True,
                "enabled_by_default": True,
            },
            "field 'priority' must be an integer",
        ),
        (
            {
                "provider_key": "bad_enabled",
                "kind": "dex",
                "capabilities": ["dex_pool_price"],
                "auth_type": "public",
                "required_env": [],
                "priority": 1,
                "enabled_by_default": "yes",
            },
            "field 'enabled_by_default' must be a boolean",
        ),
    ],
)
def test_validate_provider_spec_rejects_malformed_metadata(raw: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_provider_spec(raw)


def test_load_provider_specs_rejects_duplicate_provider_keys(tmp_path: Path) -> None:
    registry_path = tmp_path / "provider_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider_key": "duplicate",
                        "kind": "dex",
                        "capabilities": ["dex_pool_price"],
                        "auth_type": "public",
                        "required_env": [],
                        "priority": 1,
                        "enabled_by_default": True,
                    },
                    {
                        "provider_key": "duplicate",
                        "kind": "dex",
                        "capabilities": ["dex_pair_search"],
                        "auth_type": "public",
                        "required_env": [],
                        "priority": 2,
                        "enabled_by_default": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate provider_key: duplicate"):
        load_provider_specs(registry_path)


def test_secret_resolver_satisfies_public_provider_without_fake_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_KEY_REQUIRED", raising=False)
    specs = load_provider_specs(REGISTRY_PATH)
    dexscreener = next(spec for spec in specs if spec.provider_key == "dexscreener")

    result = EnvSecretResolver().resolve(dexscreener.required_env)

    assert result.satisfied is True
    assert result.present_env == ()
    assert result.missing_env == ()
    assert result.diagnostics == ()


def test_secret_resolver_reports_missing_env_with_redacted_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)

    result = EnvSecretResolver().resolve(["COINGECKO_API_KEY"])

    assert result.satisfied is False
    assert result.present_env == ()
    assert result.missing_env == ("COINGECKO_API_KEY",)
    assert result.diagnostics == ("missing_env:COINGECKO_API_KEY",)


def test_secret_resolver_reports_present_env_names_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COINGECKO_API_KEY", "cg_test_secret_value")

    result = EnvSecretResolver().resolve(["COINGECKO_API_KEY"])

    assert result.satisfied is True
    assert result.present_env == ("COINGECKO_API_KEY",)
    assert result.missing_env == ()
    assert result.diagnostics == ("env:COINGECKO_API_KEY:present",)


def test_secret_resolver_never_exposes_raw_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_secret = "redaction_fixture_secret_registry_123"
    monkeypatch.setenv("COINGECKO_API_KEY", raw_secret)
    monkeypatch.delenv("COINMARKETCAP_API_KEY", raising=False)

    result = EnvSecretResolver().resolve(["COINGECKO_API_KEY", "COINMARKETCAP_API_KEY"])
    rendered_result = repr(result)

    assert raw_secret not in rendered_result
    assert raw_secret not in " ".join(result.diagnostics)
    assert result.present_env == ("COINGECKO_API_KEY",)
    assert result.missing_env == ("COINMARKETCAP_API_KEY",)
    assert result.diagnostics == (
        "env:COINGECKO_API_KEY:present",
        "missing_env:COINMARKETCAP_API_KEY",
    )


def test_provider_registry_returns_enabled_providers_by_priority_then_key() -> None:
    registry = ProviderRegistry(
        (
            _provider_spec("low_public", kind="cex", capabilities=("cex_orderbook",), priority=10),
            _provider_spec("same_priority_b", kind="cex", capabilities=("cex_orderbook",), priority=50),
            _provider_spec("same_priority_a", kind="cex", capabilities=("cex_orderbook",), priority=50),
            _provider_spec(
                "disabled_high",
                kind="cex",
                capabilities=("cex_orderbook",),
                priority=100,
                enabled_by_default=False,
            ),
        ),
        secret_resolver=EnvSecretResolver({}),
    )

    providers = registry.providers_for("cex_orderbook")

    assert [provider.provider_key for provider in providers] == [
        "same_priority_a",
        "same_priority_b",
        "low_public",
    ]
    assert registry.status_for("disabled_high").enabled is False
    assert registry.status_for("disabled_high").reason == "disabled_by_default"


def test_provider_registry_missing_secret_disables_only_that_provider() -> None:
    registry = ProviderRegistry(
        (
            _provider_spec("public_price", priority=70),
            _provider_spec(
                "coingecko_keyed",
                priority=90,
                auth_type="api_key",
                required_env=("COINGECKO_API_KEY",),
            ),
        ),
        secret_resolver=EnvSecretResolver({}),
    )

    providers = registry.providers_for("coin_price")
    public_status = registry.status_for("public_price")
    keyed_status = registry.status_for("coingecko_keyed")

    assert [provider.provider_key for provider in providers] == ["public_price"]
    assert public_status.enabled is True
    assert public_status.diagnostics == ()
    assert keyed_status.enabled is False
    assert keyed_status.missing_env == ("COINGECKO_API_KEY",)
    assert keyed_status.reason == "missing_env:COINGECKO_API_KEY"
    assert keyed_status.diagnostics == ("missing_env:COINGECKO_API_KEY",)


def test_provider_registry_enables_api_key_provider_when_env_is_present() -> None:
    raw_secret = "cg_secret_value_that_must_not_leak"
    registry = ProviderRegistry(
        (
            _provider_spec(
                "coingecko_keyed",
                priority=90,
                auth_type="api_key",
                required_env=("COINGECKO_API_KEY",),
            ),
        ),
        secret_resolver=EnvSecretResolver({"COINGECKO_API_KEY": raw_secret}),
    )

    providers = registry.providers_for("coin_price")
    status = registry.status_for("coingecko_keyed")

    assert [provider.provider_key for provider in providers] == ["coingecko_keyed"]
    assert status.enabled is True
    assert status.reason == "enabled"
    assert status.diagnostics == ("env:COINGECKO_API_KEY:present",)
    assert raw_secret not in repr(status)


def test_provider_registry_public_provider_enabled_without_env() -> None:
    registry = ProviderRegistry(
        (_provider_spec("dexscreener_public", kind="dex", capabilities=("dex_pool_price",), priority=90),),
        secret_resolver=EnvSecretResolver({}),
    )

    v1_providers = registry.providers_for("dex_pool")
    providers = registry.providers_for("dex_pool_price")
    status = registry.status_for("dexscreener_public")

    assert [provider.provider_key for provider in v1_providers] == ["dexscreener_public"]
    assert [provider.provider_key for provider in providers] == ["dexscreener_public"]
    assert status.enabled is True
    assert status.missing_env == ()
    assert status.diagnostics == ()


def test_provider_registry_unsupported_capability_returns_empty_list() -> None:
    registry = ProviderRegistry(
        (_provider_spec("public_price"),),
        secret_resolver=EnvSecretResolver({}),
    )

    assert registry.providers_for("not_supported") == ()
