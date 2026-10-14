"""Read AWS Secrets Manager secrets from Databricks using the cluster's IAM role.

Quick start (in a notebook on a cluster with an instance profile)::

    from dbx_aws_secrets import get_secret, get_secret_json

    api_key = get_secret("prod/vendor/api-key")
    db_password = get_secret_json("prod/crm/db", key="password")
"""

from __future__ import annotations

import threading
from typing import Any

from .client import SecretsClient
from .config import SecretsConfig
from .exceptions import (
    AwsSecretsError,
    ConfigurationError,
    CredentialsError,
    SecretAccessDeniedError,
    SecretDecryptionError,
    SecretFormatError,
    SecretNotFoundError,
)

__version__ = "0.1.0"

__all__ = [
    "AwsSecretsError",
    "ConfigurationError",
    "CredentialsError",
    "SecretAccessDeniedError",
    "SecretDecryptionError",
    "SecretFormatError",
    "SecretNotFoundError",
    "SecretsClient",
    "SecretsConfig",
    "__version__",
    "clear_cache",
    "configure",
    "get_default_client",
    "get_secret",
    "get_secret_binary",
    "get_secret_json",
]

_default_client: SecretsClient | None = None
_default_lock = threading.Lock()


def configure(config: SecretsConfig | None = None, **overrides: Any) -> SecretsClient:
    """(Re)configure the shared client used by the module-level functions.

    Example: ``configure(region="us-west-2", role_arn="arn:aws:iam::123456789012:role/team-x")``
    """
    global _default_client
    client = SecretsClient(config, **overrides)
    with _default_lock:
        _default_client = client
    return client


def get_default_client() -> SecretsClient:
    """Return the shared client, creating it from ``DBX_AWS_SECRETS_*`` env vars on first use."""
    global _default_client
    if _default_client is None:
        with _default_lock:
            if _default_client is None:
                _default_client = SecretsClient()
    return _default_client


def get_secret(secret_id: str, **kwargs: Any) -> str:
    """Return a text secret. See :meth:`SecretsClient.get_secret`."""
    return get_default_client().get_secret(secret_id, **kwargs)


def get_secret_json(secret_id: str, key: str | None = None, **kwargs: Any) -> Any:
    """Return a parsed JSON secret, or one key of it. See :meth:`SecretsClient.get_secret_json`."""
    return get_default_client().get_secret_json(secret_id, key, **kwargs)


def get_secret_binary(secret_id: str, **kwargs: Any) -> bytes:
    """Return a binary secret. See :meth:`SecretsClient.get_secret_binary`."""
    return get_default_client().get_secret_binary(secret_id, **kwargs)


def clear_cache(secret_id: str | None = None) -> None:
    """Clear the shared client's cache."""
    get_default_client().clear_cache(secret_id)
