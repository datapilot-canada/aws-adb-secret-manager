# dbx-aws-secrets

Read **AWS Secrets Manager** secrets from **Databricks on AWS** with no access keys and no passwords.
The library uses the IAM role attached to the cluster (an *instance profile*) and gets
short-lived credentials automatically.

> **Azure mental model:** this is the AWS equivalent of a cluster that uses its **managed identity**
> to read from **Azure Key Vault** (`DefaultAzureCredential` + `SecretClient`).
> | Azure | AWS |
> |---|---|
> | Key Vault | Secrets Manager |
> | Key Vault secret | Secret (name or ARN, e.g. `prod/crm/db`) |
> | Managed identity on the cluster VMs | IAM role attached via an **instance profile** |
> | Key Vault access policy / RBAC | IAM policy on the role **and** the secret's resource policy |
> | Key Vault private endpoint | Interface **VPC endpoint** for `secretsmanager` |
> | Customer-managed key in Key Vault | Customer-managed **KMS** key |
> | `DefaultAzureCredential` | boto3 default credential chain |

```python
from dbx_aws_secrets import get_secret, get_secret_json

api_key  = get_secret("prod/vendor/api-key")                # plain text secret
password = get_secret_json("prod/crm/db", key="password")   # one field of a key/value secret
```

---

## 1. Requirements in an existing workspace

Your AWS / Databricks admin needs to have these in place. Steps 1–4 are a one-time setup per role.

1. **IAM role and instance profile.** The role's trust policy must allow `ec2.amazonaws.com`, and the role
   needs a policy like this (scope `Resource` to your secrets):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ReadSecrets",
         "Effect": "Allow",
         "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
         "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:prod/crm/*"
       },
       {
         "Sid": "DecryptWithCustomerManagedKey",
         "Effect": "Allow",
         "Action": "kms:Decrypt",
         "Resource": "arn:aws:kms:us-west-2:123456789012:key/<key-id>",
         "Condition": {"StringEquals": {"kms:ViaService": "secretsmanager.us-west-2.amazonaws.com"}}
       }
     ]
   }
   ```
   You only need the `kms:Decrypt` statement if the secret uses a customer-managed KMS key.

2. **Workspace can pass the role to clusters.** The workspace's cross-account IAM role needs
   `iam:PassRole` on the role above.
3. **Instance profile is registered in Databricks.** Go to *Settings → Security → Instance profiles → Add*,
   or use the `databricks_instance_profile` Terraform resource.
   Then give users or groups permission to use it.
4. **Private connectivity (recommended for PII).** Create an interface VPC endpoint for
   `com.amazonaws.<region>.secretsmanager` in the workspace VPC, with **private DNS enabled**.
   Its security group must allow inbound HTTPS (443) from the cluster subnets.
   If you use `role_arn` (section 4), also create an `sts` endpoint.
   This works with no NAT gateway and no internet access.
5. **Cluster settings.**
   - **Access mode: Dedicated** (formerly *Single user*). *Standard/Shared* clusters and serverless
     block the instance metadata service, so the instance profile is not reachable from Python code there.
   - *Advanced options → Instance profile*: select the profile from step 3.
   - Databricks Runtime 13.3 LTS or newer. `boto3` is preinstalled, so the wheel needs no internet access.

---

## 2. Install

The wheel is a single file, `dbx_aws_secrets-<version>-py3-none-any.whl`. Upload it to a
Unity Catalog volume, for example `/Volumes/platform/shared/libraries/`. Then use one of these:

| Where | How |
|---|---|
| Notebook (session-scoped) | `%pip install /Volumes/platform/shared/libraries/dbx_aws_secrets-0.1.0-py3-none-any.whl` |
| Cluster library | *Compute → cluster → Libraries → Install new → Volumes →* select the `.whl` |
| Job task | `libraries: [{ whl: /Volumes/platform/shared/libraries/dbx_aws_secrets-0.1.0-py3-none-any.whl }]` |

`boto3` is the only dependency, and it's already installed on every Databricks Runtime, so pip doesn't
download anything.

---

## 3. Usage

```python
from dbx_aws_secrets import get_secret, get_secret_json, get_secret_binary

# Text secret
token = get_secret("prod/vendor/api-key")

# Key/value secret (stored as JSON). Get the whole dict, or one key.
creds = get_secret_json("prod/crm/db")             # {"username": "...", "password": "..."}
pwd   = get_secret_json("prod/crm/db", "password")

# Binary secret (certificates, keystores, ...)
cert = get_secret_binary("prod/crm/client-cert")

# Specific version (e.g. during a rotation)
old = get_secret("prod/crm/db", version_stage="AWSPREVIOUS")

# Bypass the in-memory cache
fresh = get_secret("prod/crm/db", use_cache=False)
```

Example: JDBC read without exposing the password:

```python
db = get_secret_json("prod/crm/db")
df = (spark.read.format("jdbc")
      .option("url", "jdbc:postgresql://crm.internal:5432/crm")
      .option("dbtable", "public.customers")
      .option("user", db["username"])
      .option("password", db["password"])
      .load())
```

### Explicit client

```python
from dbx_aws_secrets import SecretsClient

client = SecretsClient(region="us-west-2", cache_ttl_seconds=60)
client.get_secret_json("prod/crm/db", key="password")
```

> ⚠️ **Unlike `dbutils.secrets`, values are not shown as `[REDACTED]` in notebook output.**
> Never `print()` or `display()` a secret. Pass it straight to the connector that needs it.

---

## 4. Configuration

You can pass every setting to `SecretsClient(...)` or `configure(...)`, or set it as an environment
variable. Environment variables are useful in a **cluster policy** (`spark_env_vars`) to set
defaults for the whole organization.

| Argument | Env var | Default | Purpose |
|---|---|---|---|
| `region` | `DBX_AWS_SECRETS_REGION` | `AWS_REGION` → `AWS_DEFAULT_REGION` → EC2 instance metadata | AWS region of the secrets |
| `role_arn` | `DBX_AWS_SECRETS_ROLE_ARN` | – | Assume this role first (cross-account or per-team secrets) |
| `role_session_name` | `DBX_AWS_SECRETS_ROLE_SESSION_NAME` | `dbx-aws-secrets` | Shows up in CloudTrail |
| `external_id` | `DBX_AWS_SECRETS_EXTERNAL_ID` | – | For roles whose trust policy requires it |
| `role_duration_seconds` | `DBX_AWS_SECRETS_ROLE_DURATION_SECONDS` | `3600` | 900–43200 |
| `endpoint_url` | `DBX_AWS_SECRETS_ENDPOINT_URL` | regional endpoint | Use a VPC-endpoint-specific DNS name if private DNS is off |
| `sts_endpoint_url` | `DBX_AWS_SECRETS_STS_ENDPOINT_URL` | `https://sts.<region>.amazonaws.com` | Regional STS endpoint, so it works through a VPC endpoint |
| `cache_ttl_seconds` | `DBX_AWS_SECRETS_CACHE_TTL_SECONDS` | `300` | In-memory cache, per Python process; `0` disables it |
| `max_attempts` | `DBX_AWS_SECRETS_MAX_ATTEMPTS` | `5` | Retries with backoff (botocore "standard" mode) |
| `connect_timeout` / `read_timeout` | `DBX_AWS_SECRETS_CONNECT_TIMEOUT` / `..._READ_TIMEOUT` | `5` / `10` s | |

```python
import dbx_aws_secrets
dbx_aws_secrets.configure(role_arn="arn:aws:iam::210987654321:role/team-x-secrets-reader")
dbx_aws_secrets.get_secret("arn:aws:secretsmanager:us-west-2:210987654321:secret:team-x/api-AbCdEf")
```

When `role_arn` is set, the target role's trust policy must allow the cluster's instance-profile
role to call `sts:AssumeRole`.

---

## 5. Errors and troubleshooting

Every error derives from `dbx_aws_secrets.AwsSecretsError`. Error messages never contain secret values.

| Exception | Typical cause | Fix |
|---|---|---|
| `CredentialsError` | No instance profile on the cluster, or Standard/Shared/serverless compute | Use a Dedicated cluster with the instance profile attached |
| `ConfigurationError` | Region could not be determined | Pass `region=` or set `DBX_AWS_SECRETS_REGION` |
| `SecretNotFoundError` | Wrong name or region, or the secret is scheduled for deletion | Check the name, or use the full ARN |
| `SecretAccessDeniedError` | IAM policy, secret resource policy or VPC endpoint policy denied the request | See the message. An explicit `Deny` anywhere wins, like Azure deny assignments |
| `SecretDecryptionError` | The role lacks `kms:Decrypt` on the customer-managed key | Add `kms:Decrypt` (see section 1) and check the KMS key policy |
| `SecretFormatError` | Text vs. binary mismatch, invalid JSON, or missing key | Use the matching function or key |
| `AwsSecretsError` mentioning "VPC endpoint" | Network path to Secrets Manager is blocked | Check the endpoint, its security group (443) and private DNS |

Quick check in a notebook:

```python
import boto3
print(boto3.client("sts", region_name="us-west-2",
                   endpoint_url="https://sts.us-west-2.amazonaws.com").get_caller_identity()["Arn"])
# expected: arn:aws:sts::<account>:assumed-role/<instance-profile-role>/i-0abc...
```

Turn on debug logging (logs secret names and cache hits, never values):

```python
import logging; logging.getLogger("dbx_aws_secrets").setLevel(logging.DEBUG)
```

---

## 6. Development

```powershell
cd python
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
pytest                                 # AWS is fully mocked with moto, so no AWS account is needed
ruff check .
python -m build --wheel                # -> dist/dbx_aws_secrets-<version>-py3-none-any.whl
```

To release a new version, bump `__version__` in `src/dbx_aws_secrets/__init__.py`, then build and
upload the wheel to the shared volume.
