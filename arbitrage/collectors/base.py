from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal


CollectorStatus = Literal["OK", "DEGRADED"]

STATUS_OK: CollectorStatus = "OK"
STATUS_DEGRADED: CollectorStatus = "DEGRADED"

_SUPPORTED_STATUSES = frozenset((STATUS_OK, STATUS_DEGRADED))
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|client[_-]?secret|password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(r"\b(?:sk|pk|rk|gh[pousr]|xox[baprs])_[A-Za-z0-9_=\-]{6,}\b")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|client[_-]?secret|password|private[_-]?key|secret|token)"
    r"\s*[:=]\s*[^,\s}\]]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]+")


@dataclass(frozen=True, slots=True)
class CollectorResult:
    provider_key: str
    scope_key: str
    cursor_before: str
    cursor_after: str
    status: CollectorStatus
    inserted_count: int = 0
    deadletter_count: int = 0

    def __post_init__(self) -> None:
        provider_key = self.provider_key.strip()
        scope_key = self.scope_key.strip()
        status = self.status.strip().upper()

        if not provider_key:
            raise ValueError("provider_key must be a non-empty string")
        if not scope_key:
            raise ValueError("scope_key must be a non-empty string")
        if status not in _SUPPORTED_STATUSES:
            raise ValueError(f"unsupported collector status: {self.status}")
        if self.inserted_count < 0:
            raise ValueError("inserted_count must be non-negative")
        if self.deadletter_count < 0:
            raise ValueError("deadletter_count must be non-negative")

        object.__setattr__(self, "provider_key", provider_key)
        object.__setattr__(self, "scope_key", scope_key)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "cursor_before", str(self.cursor_before))
        object.__setattr__(self, "cursor_after", str(self.cursor_after))

    @property
    def cursor_advanced(self) -> bool:
        return self.cursor_after != self.cursor_before

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def current_observed_at_ms() -> int:
    return int(time.time() * 1000)


def normalize_observed_at_ms(value: Any = None, *, now_ms: int | None = None) -> int:
    if value is None or value == "":
        return int(now_ms if now_ms is not None else current_observed_at_ms())

    if isinstance(value, bool):
        raise ProviderPayloadError(
            provider_key="unknown",
            scope_key="unknown",
            error_code="invalid_observed_at_ms",
            message="observed_at_ms must be a timestamp, not a boolean",
            field_path="observed_at_ms",
        )

    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return int(now_ms if now_ms is not None else current_observed_at_ms())
        numeric = _parse_numeric_timestamp(stripped)
        if numeric is None:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ProviderPayloadError(
                    provider_key="unknown",
                    scope_key="unknown",
                    error_code="invalid_observed_at_ms",
                    message="observed_at_ms string must be numeric milliseconds, seconds, or ISO-8601",
                    field_path="observed_at_ms",
                ) from exc
            dt = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        return _normalize_numeric_timestamp(numeric)

    if isinstance(value, (int, float)):
        return _normalize_numeric_timestamp(float(value))

    raise ProviderPayloadError(
        provider_key="unknown",
        scope_key="unknown",
        error_code="invalid_observed_at_ms",
        message=f"observed_at_ms has unsupported type {type(value).__name__}",
        field_path="observed_at_ms",
    )


def monotonic_cursor_value(cursor_before: str, candidate: int | str) -> str:
    candidate_text = str(candidate)
    cursor_before = str(cursor_before or "")
    if not cursor_before:
        return candidate_text

    try:
        before_number = int(cursor_before)
        candidate_number = int(candidate_text)
    except ValueError:
        return candidate_text

    return candidate_text if candidate_number >= before_number else cursor_before


class ProviderPayloadError(ValueError):
    def __init__(
        self,
        *,
        provider_key: str,
        scope_key: str,
        error_code: str,
        message: str,
        field_path: str = "",
        payload: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> None:
        self.provider_key = str(provider_key or "unknown")
        self.scope_key = str(scope_key or "unknown")
        self.error_code = str(error_code or "invalid_provider_payload")
        self.message = redact_provider_text(str(message or "invalid provider payload"))
        self.field_path = str(field_path or "")
        self.payload = redact_provider_payload({} if payload is None else payload)
        super().__init__(str(self))

    def __str__(self) -> str:
        parts = [self.provider_key, self.scope_key, self.error_code]
        if self.field_path:
            parts.append(self.field_path)
        return f"{':'.join(parts)}: {self.message}"

    def to_deadletter_payload(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "scope_key": self.scope_key,
            "error_code": self.error_code,
            "message": self.message,
            "field_path": self.field_path,
            "payload": self.payload,
        }


def provider_payload_error(
    *,
    provider_key: str,
    scope_key: str,
    error_code: str,
    message: str,
    field_path: str = "",
    payload: Mapping[str, Any] | Sequence[Any] | None = None,
) -> ProviderPayloadError:
    return ProviderPayloadError(
        provider_key=provider_key,
        scope_key=scope_key,
        error_code=error_code,
        message=message,
        field_path=field_path,
        payload=payload,
    )


def ensure_payload_mapping(
    payload: Any,
    *,
    provider_key: str,
    scope_key: str,
    field_path: str = "payload",
) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    raise ProviderPayloadError(
        provider_key=provider_key,
        scope_key=scope_key,
        error_code="invalid_provider_payload",
        message="provider payload must be an object",
        field_path=field_path,
        payload={"received_type": type(payload).__name__},
    )


def redact_provider_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY_RE.search(key_text):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = redact_provider_payload(item)
        return redacted

    if isinstance(value, (list, tuple)):
        return [redact_provider_payload(item) for item in value]

    if isinstance(value, str):
        return redact_provider_text(value)

    return value


def redact_provider_text(value: str) -> str:
    redacted = _BEARER_RE.sub("Bearer <redacted>", value)
    redacted = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    return _SECRET_VALUE_RE.sub("<redacted>", redacted)


def _parse_numeric_timestamp(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _normalize_numeric_timestamp(value: float) -> int:
    if value <= 0:
        raise ProviderPayloadError(
            provider_key="unknown",
            scope_key="unknown",
            error_code="invalid_observed_at_ms",
            message="observed_at_ms must be positive",
            field_path="observed_at_ms",
        )
    if value < 10_000_000_000:
        return int(value * 1000)
    return int(value)
