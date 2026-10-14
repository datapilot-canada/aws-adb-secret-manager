from __future__ import annotations

import json

import boto3
import pytest
from botocore.exceptions import ClientError

import dbx_aws_secrets
from dbx_aws_secrets import (
    AwsSecretsError,
    ConfigurationError,
    CredentialsError,
    SecretAccessDeniedError,
    SecretDecryptionError,
    SecretFormatError,
    SecretNotFoundError,
    SecretsClient,
    SecretsConfig,
)
from dbx_aws_secrets import client as client_module

REGION = "us-west-2"
DB_SECRET = {"username": "svc_reader", "password": "s3cr3t-P@ss"}


# ----------------------------------------------------------------------------- reading


def test_get_secret_text(aws):
    aws.create_secret(Name="app/api-key", SecretString="abc123")
    assert SecretsClient().get_secret("app/api-key") == "abc123"


def test_get_secret_by_arn(aws):
    arn = aws.create_secret(Name="app/api-key", SecretString="abc123")["ARN"]
    assert SecretsClient().get_secret(arn) == "abc123"


def test_get_secret_json_whole_and_key(aws):
    aws.create_secret(Name="db", SecretString=json.dumps(DB_SECRET))
    client = SecretsClient()
    assert client.get_secret_json("db") == DB_SECRET
    assert client.get_secret_json("db", key="password") == "s3cr3t-P@ss"


def test_get_secret_json_missing_key(aws):
    aws.create_secret(Name="db", SecretString=json.dumps(DB_SECRET))
    with pytest.raises(SecretFormatError, match="no key 'port'"):
        SecretsClient().get_secret_json("db", key="port")


def test_get_secret_json_invalid_json_does_not_leak_value(aws):
    aws.create_secret(Name="plain", SecretString="not-json-TOPSECRET")
    with pytest.raises(SecretFormatError) as info:
        SecretsClient().get_secret_json("plain")
    assert "TOPSECRET" not in str(info.value)
    assert info.value.__cause__ is None


def test_get_secret_json_key_on_non_object(aws):
    aws.create_secret(Name="list", SecretString="[1, 2]")
    with pytest.raises(SecretFormatError, match="not a JSON object"):
        SecretsClient().get_secret_json("list", key="a")


def test_binary_secret(aws):
    aws.create_secret(Name="cert", SecretBinary=b"\x00\x01binary")
    client = SecretsClient()
    assert client.get_secret_binary("cert") == b"\x00\x01binary"
    with pytest.raises(SecretFormatError, match="binary"):
        client.get_secret("cert")


def test_text_secret_requested_as_binary(aws):
    aws.create_secret(Name="txt", SecretString="hello")
    with pytest.raises(SecretFormatError, match="text"):
        SecretsClient().get_secret_binary("txt")


def test_version_stage(aws):
    aws.create_secret(Name="rot", SecretString="v1")
    aws.put_secret_value(SecretId="rot", SecretString="v2")
    client = SecretsClient(cache_ttl_seconds=0)
    assert client.get_secret("rot") == "v2"
    assert client.get_secret("rot", version_stage="AWSPREVIOUS") == "v1"


def test_empty_secret_id_rejected(aws):
    with pytest.raises(ValueError):
        SecretsClient().get_secret("")


# ----------------------------------------------------------------------------- errors


def test_not_found(aws):
    with pytest.raises(SecretNotFoundError):
        SecretsClient().get_secret("does/not/exist")


def test_deleted_secret_is_not_found(aws):
    aws.create_secret(Name="gone", SecretString="x")
    aws.delete_secret(SecretId="gone", RecoveryWindowInDays=7)
    with pytest.raises(SecretNotFoundError):
        SecretsClient().get_secret("gone")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ResourceNotFoundException", SecretNotFoundError),
        ("AccessDeniedException", SecretAccessDeniedError),
        ("AccessDenied", SecretAccessDeniedError),
        ("ExpiredTokenException", SecretAccessDeniedError),
        ("DecryptionFailure", SecretDecryptionError),
        ("InternalServiceError", AwsSecretsError),
    ],
)
def test_client_error_mapping(code, expected):
    err = ClientError({"Error": {"Code": code, "Message": "boom"}}, "GetSecretValue")
    mapped = client_module._map_client_error(err, "my/secret")
    assert type(mapped) is expected
    assert "my/secret" in str(mapped)


def test_no_credentials(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var)
    with pytest.raises(CredentialsError, match="instance profile"):
        SecretsClient(region=REGION).get_secret("x")


def test_unreachable_endpoint():
    client = SecretsClient(
        region=REGION,
        endpoint_url="http://127.0.0.1:9",
        max_attempts=1,
        connect_timeout=0.5,
        read_timeout=0.5,
    )
    with pytest.raises(AwsSecretsError, match="VPC endpoint"):
        client.get_secret("x")


# ----------------------------------------------------------------------------- caching


def test_cache_returns_cached_value_until_cleared(aws):
    aws.create_secret(Name="c", SecretString="old")
    client = SecretsClient()
    assert client.get_secret("c") == "old"
    aws.put_secret_value(SecretId="c", SecretString="new")
    assert client.get_secret("c") == "old"
    assert client.get_secret("c", use_cache=False) == "new"
    client.clear_cache("c")
    assert client.get_secret("c") == "new"


def test_cache_expires(aws, monkeypatch):
    aws.create_secret(Name="c", SecretString="old")
    now = [1000.0]
    monkeypatch.setattr(client_module.time, "monotonic", lambda: now[0])
    client = SecretsClient(cache_ttl_seconds=60)
    assert client.get_secret("c") == "old"
    aws.put_secret_value(SecretId="c", SecretString="new")
    now[0] += 61
    assert client.get_secret("c") == "new"


def test_cache_disabled(aws):
    aws.create_secret(Name="c", SecretString="old")
    client = SecretsClient(cache_ttl_seconds=0)
    client.get_secret("c")
    aws.put_secret_value(SecretId="c", SecretString="new")
    assert client.get_secret("c") == "new"


def test_repr_has_no_secret_values(aws):
    aws.create_secret(Name="c", SecretString="TOPSECRET")
    client = SecretsClient()
    client.get_secret("c")
    assert "TOPSECRET" not in repr(client)


# ----------------------------------------------------------------------------- assume role


def test_assume_role(aws):
    iam = boto3.client("iam", region_name=REGION)
    role_arn = iam.create_role(RoleName="team-x", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    aws.create_secret(Name="team-x/secret", SecretString="xyz")

    client = SecretsClient(role_arn=role_arn, external_id="ext-1")
    assert client.get_secret("team-x/secret") == "xyz"


def test_regional_sts_endpoint():
    assert (
        client_module._regional_sts_endpoint("us-west-2") == "https://sts.us-west-2.amazonaws.com"
    )
    assert client_module._regional_sts_endpoint("cn-north-1").endswith(".amazonaws.com.cn")


# ----------------------------------------------------------------------------- region


def test_region_explicit_wins(monkeypatch):
    assert SecretsClient(region="eu-west-1").region == "eu-west-1"


def test_region_from_package_env(monkeypatch):
    monkeypatch.setenv("DBX_AWS_SECRETS_REGION", "eu-central-1")
    assert SecretsClient().region == "eu-central-1"


def test_region_from_aws_env():
    assert SecretsClient().region == REGION


def test_region_from_instance_metadata(monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    monkeypatch.setattr(
        client_module.InstanceMetadataRegionFetcher, "retrieve_region", lambda self: "ap-south-1"
    )
    assert SecretsClient().region == "ap-south-1"


def test_region_missing(monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    monkeypatch.setattr(
        client_module.InstanceMetadataRegionFetcher, "retrieve_region", lambda self: None
    )
    with pytest.raises(ConfigurationError, match="region"):
        _ = SecretsClient().region


# ----------------------------------------------------------------------------- config


def test_config_from_env_coerces_types(monkeypatch):
    monkeypatch.setenv("DBX_AWS_SECRETS_CACHE_TTL_SECONDS", "30")
    monkeypatch.setenv("DBX_AWS_SECRETS_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("DBX_AWS_SECRETS_ROLE_ARN", "arn:aws:iam::123456789012:role/r")
    cfg = SecretsConfig.from_env(max_attempts=7)
    assert cfg.cache_ttl_seconds == 30.0
    assert cfg.max_attempts == 7
    assert cfg.role_arn == "arn:aws:iam::123456789012:role/r"


def test_config_invalid_env(monkeypatch):
    monkeypatch.setenv("DBX_AWS_SECRETS_MAX_ATTEMPTS", "many")
    with pytest.raises(ConfigurationError, match="MAX_ATTEMPTS"):
        SecretsConfig.from_env()


@pytest.mark.parametrize(
    "kwargs",
    [{"cache_ttl_seconds": -1}, {"max_attempts": 0}, {"role_duration_seconds": 60}],
)
def test_config_validation(kwargs):
    with pytest.raises(ConfigurationError):
        SecretsConfig(**kwargs)


def test_config_and_overrides_are_exclusive():
    with pytest.raises(ConfigurationError):
        SecretsClient(SecretsConfig(), region=REGION)


# ----------------------------------------------------------------------------- module API


def test_module_level_functions(aws):
    aws.create_secret(Name="db", SecretString=json.dumps(DB_SECRET))
    aws.create_secret(Name="bin", SecretBinary=b"\x01")
    assert dbx_aws_secrets.get_secret_json("db", "username") == "svc_reader"
    assert dbx_aws_secrets.get_secret("db") == json.dumps(DB_SECRET)
    assert dbx_aws_secrets.get_secret_binary("bin") == b"\x01"
    dbx_aws_secrets.clear_cache()


def test_configure_replaces_default_client(aws):
    client = dbx_aws_secrets.configure(region="eu-west-1", cache_ttl_seconds=0)
    assert dbx_aws_secrets.get_default_client() is client
    assert client.region == "eu-west-1"
