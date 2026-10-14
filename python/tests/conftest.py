from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

import dbx_aws_secrets

REGION = "us-west-2"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Make sure tests never touch real AWS accounts or local AWS config."""
    for name in list(os.environ):
        if name.startswith(("AWS_", "DBX_AWS_SECRETS_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(dbx_aws_secrets, "_default_client", None)
    yield


@pytest.fixture
def aws():
    with mock_aws():
        yield boto3.client("secretsmanager", region_name=REGION)
