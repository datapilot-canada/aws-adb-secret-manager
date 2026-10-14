"""Client configuration, optionally loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any

from .exceptions import ConfigurationError

ENV_PREFIX = "DBX_AWS_SECRETS_"


@dataclass(frozen=True)
class SecretsConfig:
    """Settings for :class:`~dbx_aws_secrets.SecretsClient`.

    Every field can also be supplied through an environment variable named
    ``DBX_AWS_SECRETS_<FIELD_NAME_UPPERCASE>`` (e.g. ``DBX_AWS_SECRETS_ROLE_ARN``),
    which is handy for setting org-wide defaults in a cluster policy's ``spark_env_vars``.
    """

    region: str | None = None
    """AWS region. Falls back to AWS_REGION / AWS_DEFAULT_REGION, then the EC2 instance metadata."""

    role_arn: str | None = None
    """Optional IAM role to assume on top of the cluster's instance profile
    (cross-account / per-team secrets)."""

    role_session_name: str = "dbx-aws-secrets"
    external_id: str | None = None
    role_duration_seconds: int = 3600

    endpoint_url: str | None = None
    """Override the Secrets Manager endpoint, e.g. a VPC-endpoint-specific DNS name
    when private DNS is disabled on the interface endpoint."""

    sts_endpoint_url: str | None = None
    """Override the STS endpoint. Default: the regional endpoint (reachable via a VPC endpoint)."""

    cache_ttl_seconds: float = 300.0
    """How long to cache secret values in memory. 0 disables caching."""

    max_attempts: int = 5
    connect_timeout: float = 5.0
    read_timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.cache_ttl_seconds < 0:
            raise ConfigurationError("cache_ttl_seconds must be >= 0")
        if self.max_attempts < 1:
            raise ConfigurationError("max_attempts must be >= 1")
        if not 900 <= self.role_duration_seconds <= 43200:
            raise ConfigurationError("role_duration_seconds must be between 900 and 43200")

    @classmethod
    def from_env(cls, **overrides: Any) -> SecretsConfig:
        """Build a config from ``DBX_AWS_SECRETS_*`` env vars; explicit ``overrides`` win."""
        values: dict[str, Any] = {}
        for f in fields(cls):
            raw = os.environ.get(ENV_PREFIX + f.name.upper())
            if raw is None or raw == "":
                continue
            values[f.name] = _coerce(f.name, raw, f.default)
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


def _coerce(name: str, raw: str, default: Any) -> Any:
    try:
        if isinstance(default, bool):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid value for {ENV_PREFIX}{name.upper()}: {raw!r}") from exc
    return raw
