"""Secrets Manager client that relies on the Databricks cluster's IAM role (instance profile)."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
    ReadTimeoutError,
)
from botocore.session import get_session as _get_botocore_session
from botocore.utils import InstanceMetadataRegionFetcher

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

logger = logging.getLogger("dbx_aws_secrets")

_NOT_FOUND_CODES = {"ResourceNotFoundException"}
_ACCESS_DENIED_CODES = {
    "AccessDeniedException",
    "AccessDenied",
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "ExpiredTokenException",
}
_DECRYPTION_CODES = {"DecryptionFailure", "KMSAccessDeniedException", "KMSDisabledException"}

_CacheKey = tuple[str, str | None, str | None]


class SecretsClient:
    """Thread-safe, caching reader for AWS Secrets Manager.

    Credentials come from the standard AWS credential chain. On a Databricks cluster with an
    instance profile that means short-lived credentials from the EC2 instance metadata service:
    nothing to store, rotate or leak. Optionally, ``role_arn`` assumes a further role.

    Example::

        client = SecretsClient(region="us-west-2")
        password = client.get_secret_json("prod/crm/db", key="password")
    """

    def __init__(
        self,
        config: SecretsConfig | None = None,
        *,
        session: boto3.Session | None = None,
        **overrides: Any,
    ) -> None:
        if config is not None and overrides:
            raise ConfigurationError("Pass either a SecretsConfig or keyword overrides, not both.")
        self._config = config or SecretsConfig.from_env(**overrides)
        self._base_session = session
        self._region: str | None = None
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._cache: dict[_CacheKey, tuple[float, str | bytes]] = {}
        self._cache_lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            f"SecretsClient(region={self._config.region or self._region!r}, "
            f"role_arn={self._config.role_arn!r}, "
            f"cache_ttl_seconds={self._config.cache_ttl_seconds})"
        )

    @property
    def config(self) -> SecretsConfig:
        return self._config

    @property
    def region(self) -> str:
        """The resolved AWS region (resolved lazily on first use)."""
        if self._region is None:
            self._region = _resolve_region(self._config.region, self._base_session)
        return self._region

    # ------------------------------------------------------------------ public API

    def get_secret(
        self,
        secret_id: str,
        *,
        version_id: str | None = None,
        version_stage: str | None = None,
        use_cache: bool = True,
    ) -> str:
        """Return a text secret (``SecretString``).

        ``secret_id`` may be the secret name or its full ARN (use the ARN for cross-account
        secrets).
        """
        value = self._get(secret_id, version_id, version_stage, use_cache)
        if isinstance(value, bytes):
            raise SecretFormatError(
                f"Secret '{secret_id}' is binary; use get_secret_binary() instead."
            )
        return value

    def get_secret_binary(
        self,
        secret_id: str,
        *,
        version_id: str | None = None,
        version_stage: str | None = None,
        use_cache: bool = True,
    ) -> bytes:
        """Return a binary secret (``SecretBinary``)."""
        value = self._get(secret_id, version_id, version_stage, use_cache)
        if isinstance(value, str):
            raise SecretFormatError(f"Secret '{secret_id}' is text; use get_secret() instead.")
        return value

    def get_secret_json(
        self,
        secret_id: str,
        key: str | None = None,
        *,
        version_id: str | None = None,
        version_stage: str | None = None,
        use_cache: bool = True,
    ) -> Any:
        """Parse a JSON text secret. With ``key``, return just that top-level field.

        Secrets created as "key/value" pairs in the AWS console are stored as JSON.
        """
        raw = self.get_secret(
            secret_id, version_id=version_id, version_stage=version_stage, use_cache=use_cache
        )
        try:
            data = json.loads(raw)
        except ValueError:
            # Do not chain the JSONDecodeError: its message would include part of the secret.
            raise SecretFormatError(f"Secret '{secret_id}' is not valid JSON.") from None
        if key is None:
            return data
        if not isinstance(data, dict):
            raise SecretFormatError(f"Secret '{secret_id}' is not a JSON object.")
        if key not in data:
            raise SecretFormatError(f"Secret '{secret_id}' has no key '{key}'.")
        return data[key]

    def clear_cache(self, secret_id: str | None = None) -> None:
        """Drop cached values (all, or only those for ``secret_id``)."""
        with self._cache_lock:
            if secret_id is None:
                self._cache.clear()
            else:
                for k in [k for k in self._cache if k[0] == secret_id]:
                    del self._cache[k]

    # ------------------------------------------------------------------ internals

    def _get(
        self,
        secret_id: str,
        version_id: str | None,
        version_stage: str | None,
        use_cache: bool,
    ) -> str | bytes:
        if not secret_id:
            raise ValueError("secret_id must be a non-empty string")

        key: _CacheKey = (secret_id, version_id, version_stage)
        ttl = self._config.cache_ttl_seconds
        if use_cache and ttl > 0:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit and hit[0] > time.monotonic():
                    logger.debug("Cache hit for secret '%s'", secret_id)
                    return hit[1]

        value = self._fetch(secret_id, version_id, version_stage)

        if ttl > 0:
            with self._cache_lock:
                self._cache[key] = (time.monotonic() + ttl, value)
        return value

    def _fetch(
        self, secret_id: str, version_id: str | None, version_stage: str | None
    ) -> str | bytes:
        kwargs: dict[str, str] = {"SecretId": secret_id}
        if version_id:
            kwargs["VersionId"] = version_id
        if version_stage:
            kwargs["VersionStage"] = version_stage

        logger.debug("Fetching secret '%s' from AWS Secrets Manager", secret_id)
        try:
            response = self._get_client().get_secret_value(**kwargs)
        except ClientError as exc:
            raise _map_client_error(exc, secret_id) from None
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise CredentialsError(
                "No AWS credentials found. On Databricks, attach an instance profile to the "
                "cluster and use dedicated (single user) access mode."
            ) from exc
        except NoRegionError as exc:
            raise ConfigurationError("AWS region could not be determined.") from exc
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
            raise AwsSecretsError(
                f"Could not reach AWS Secrets Manager in {self.region}. In a private VPC, check "
                "the 'secretsmanager' (and, with role_arn, 'sts') interface VPC endpoints, their "
                "security group (HTTPS 443 from the cluster subnets) and private DNS."
            ) from exc
        except BotoCoreError as exc:
            raise AwsSecretsError(
                f"AWS SDK error while reading secret '{secret_id}': {exc}"
            ) from exc

        if "SecretString" in response and response["SecretString"] is not None:
            return response["SecretString"]
        if "SecretBinary" in response and response["SecretBinary"] is not None:
            return bytes(response["SecretBinary"])
        raise SecretFormatError(f"Secret '{secret_id}' has no value.")

    def _get_client(self) -> Any:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = self._build_client()
        return self._client

    def _build_client(self) -> Any:
        cfg = self._config
        region = self.region
        botocore_config = Config(
            region_name=region,
            retries={"mode": "standard", "max_attempts": cfg.max_attempts},
            connect_timeout=cfg.connect_timeout,
            read_timeout=cfg.read_timeout,
            user_agent_extra="dbx-aws-secrets",
        )
        session = self._base_session or boto3.Session()
        if cfg.role_arn:
            session = _assume_role_session(session, cfg, region, botocore_config)
        return session.client(
            "secretsmanager",
            region_name=region,
            endpoint_url=cfg.endpoint_url,
            config=botocore_config,
        )


# ---------------------------------------------------------------------- helpers


def _resolve_region(explicit: str | None, session: boto3.Session | None) -> str:
    if explicit:
        return explicit
    # boto3 already honours AWS_REGION / AWS_DEFAULT_REGION / ~/.aws/config.
    region = (session or boto3.Session()).region_name
    if region:
        return region
    # Databricks clusters don't set AWS_REGION; ask the EC2 instance metadata service (IMDSv2).
    region = InstanceMetadataRegionFetcher(timeout=1, num_attempts=2).retrieve_region()
    if region:
        return region
    raise ConfigurationError(
        "AWS region could not be determined. Pass region=..., or set AWS_REGION or "
        "DBX_AWS_SECRETS_REGION."
    )


def _regional_sts_endpoint(region: str) -> str:
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    return f"https://sts.{region}.{suffix}"


def _assume_role_session(
    base: boto3.Session, cfg: SecretsConfig, region: str, botocore_config: Config
) -> boto3.Session:
    """Return a session whose credentials come from (and auto-refresh via) sts:AssumeRole."""
    # Always use the regional STS endpoint: the global one is not reachable through a VPC endpoint.
    sts = base.client(
        "sts",
        region_name=region,
        endpoint_url=cfg.sts_endpoint_url or _regional_sts_endpoint(region),
        config=botocore_config,
    )

    def refresh() -> dict[str, str]:
        kwargs: dict[str, Any] = {
            "RoleArn": cfg.role_arn,
            "RoleSessionName": cfg.role_session_name,
            "DurationSeconds": cfg.role_duration_seconds,
        }
        if cfg.external_id:
            kwargs["ExternalId"] = cfg.external_id
        try:
            creds = sts.assume_role(**kwargs)["Credentials"]
        except ClientError as exc:
            raise _map_client_error(exc, f"role {cfg.role_arn}") from None
        logger.debug("Assumed role %s", cfg.role_arn)
        return {
            "access_key": creds["AccessKeyId"],
            "secret_key": creds["SecretAccessKey"],
            "token": creds["SessionToken"],
            "expiry_time": creds["Expiration"].isoformat(),
        }

    # Called lazily: the client (and thus this first AssumeRole) is only built on first use.
    credentials = RefreshableCredentials.create_from_metadata(
        metadata=refresh(), refresh_using=refresh, method="sts-assume-role"
    )
    botocore_session = _get_botocore_session()
    botocore_session._credentials = credentials  # standard pattern for injecting refreshable creds
    botocore_session.set_config_variable("region", region)
    return boto3.Session(botocore_session=botocore_session)


def _map_client_error(exc: ClientError, what: str) -> AwsSecretsError:
    error = exc.response.get("Error", {})
    code = error.get("Code", "Unknown")
    message = error.get("Message", "")
    text = f"{code} for '{what}': {message}".rstrip(": ")
    if code in _NOT_FOUND_CODES:
        return SecretNotFoundError(text)
    if code in _DECRYPTION_CODES:
        return SecretDecryptionError(
            text + " (check that the IAM role has kms:Decrypt on the secret's KMS key)"
        )
    if code in _ACCESS_DENIED_CODES:
        return SecretAccessDeniedError(
            text + " (check the IAM role policy, the secret's resource policy and the "
            "VPC endpoint policy)"
        )
    if code == "InvalidRequestException" and "deleted" in message.lower():
        return SecretNotFoundError(text)
    return AwsSecretsError(text)
