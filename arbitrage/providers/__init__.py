"""Provider metadata, registry, and read-only adapter helpers."""

from .base import (
    READ_ONLY_HTTP_V1_CAPABILITIES,
    ProviderSpec,
    ProviderStatus,
    SUPPORTED_CAPABILITIES,
    normalize_capability,
)
from .http_adapters import (
    HttpAdapterStatus,
    ProviderAdapterDisabled,
    ProviderHttpError,
    ProviderHttpTimeout,
    ReadOnlyHttpAdapterCatalog,
    ReadOnlyHttpProviderAdapter,
    UrllibJsonHttpClient,
)
from .registry import ProviderRegistry, load_provider_specs, validate_provider_spec
from .secrets import EnvSecretResolution, EnvSecretResolver

__all__ = [
    "EnvSecretResolution",
    "HttpAdapterStatus",
    "EnvSecretResolver",
    "ProviderAdapterDisabled",
    "ProviderHttpError",
    "ProviderHttpTimeout",
    "ProviderSpec",
    "ProviderRegistry",
    "ProviderStatus",
    "READ_ONLY_HTTP_V1_CAPABILITIES",
    "ReadOnlyHttpAdapterCatalog",
    "ReadOnlyHttpProviderAdapter",
    "SUPPORTED_CAPABILITIES",
    "UrllibJsonHttpClient",
    "load_provider_specs",
    "normalize_capability",
    "validate_provider_spec",
]
