"""Exception hierarchy. Messages never contain secret values."""

from __future__ import annotations


class AwsSecretsError(Exception):
    """Base class for all errors raised by dbx_aws_secrets."""


class ConfigurationError(AwsSecretsError):
    """Invalid or incomplete configuration (e.g. region could not be determined)."""


class CredentialsError(AwsSecretsError):
    """No AWS credentials were found (e.g. cluster has no instance profile attached)."""


class SecretNotFoundError(AwsSecretsError):
    """The secret (or requested version) does not exist."""


class SecretAccessDeniedError(AwsSecretsError):
    """Denied by IAM, the secret resource policy, the KMS key policy or a VPC endpoint policy."""


class SecretDecryptionError(AwsSecretsError):
    """Secrets Manager could not decrypt the secret with its KMS key."""


class SecretFormatError(AwsSecretsError):
    """The secret value is not in the requested format (text vs. binary, JSON, missing key)."""
