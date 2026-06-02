from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EnvSecretResolution:
    satisfied: bool
    present_env: tuple[str, ...]
    missing_env: tuple[str, ...]
    diagnostics: tuple[str, ...]


class EnvSecretResolver:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def resolve(self, required_env: Sequence[str]) -> EnvSecretResolution:
        if isinstance(required_env, (str, bytes)):
            raise TypeError("required_env must be a sequence of env var names, not a string")

        present_env: list[str] = []
        missing_env: list[str] = []
        diagnostics: list[str] = []

        for env_name in required_env:
            if not isinstance(env_name, str) or not env_name:
                raise ValueError("required_env must contain non-empty env var names")

            if self._environ.get(env_name):
                present_env.append(env_name)
                diagnostics.append(f"env:{env_name}:present")
            else:
                missing_env.append(env_name)
                diagnostics.append(f"missing_env:{env_name}")

        return EnvSecretResolution(
            satisfied=not missing_env,
            present_env=tuple(present_env),
            missing_env=tuple(missing_env),
            diagnostics=tuple(diagnostics),
        )
