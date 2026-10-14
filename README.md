# Plan – aws-adb-secret-manager

## Decisions (from Q&A)
| Topic | Decision | Azure analogy |
|---|---|---|
| Workspace | Terraform creates a new Databricks workspace end-to-end (customer-managed VPC) | VNet-injected ADB workspace |
| Identity | IAM role + instance profile on clusters | Managed identity on cluster VMs |
| Wheel location | UC Volume, deployed by `databricks bundle deploy` | Wheel in ADLS/UC volume |
| Tooling split | Terraform = infra; DAB (Terraform engine) = wheel + notebook + job | Bicep/TF infra + DAB |
| Unity Catalog | New metastore in us-west-2 | Regional UC metastore |
| Network | Front-end + back-end PrivateLink, public access disabled | Private endpoints + SCC, public network access off |
| Operator access | AWS Client VPN in a transit VPC | VPN Gateway P2S |
| Region | us-west-2 | – |
| Secrets | Customer-managed KMS key, VPC endpoint policy, resource policy (`aws:SourceVpce`) | Key Vault + CMK + private endpoint + firewall |
| Egress | No NAT, no internet; VPC endpoints only | Isolated VNet, no NAT GW |

## Repository layout
```
README.md                         # full beginner guide (AWS ⇄ Azure cross-ref)
databricks.yml                    # bundle root
resources/secret_demo_job.yml     # job on a job cluster with instance profile
notebooks/read_aws_secret.py      # sample notebook using the wheel
python/                           # wheel project (src layout, hatchling)
  pyproject.toml
  src/dbx_aws_secrets/{__init__,client,config,exceptions}.py
  tests/ (pytest + moto)
infra/terraform/
  bootstrap/                      # S3 state bucket (+ native lockfile), KMS
  envs/dev/                       # root module, tfvars.example, backend.hcl.example
  modules/
    network/                      # workspace VPC: 2 private subnets/AZs, SGs, VPC endpoints
                                  #   S3 gateway, STS, Kinesis, Secrets Manager, Databricks REST + SCC relay
    transit_vpn/                  # transit VPC, front-end Databricks VPCe, Client VPN (mutual TLS), DNS
    workspace/                    # cross-account role, root S3 bucket, mws_credentials/storage/networks,
                                  #   mws_vpc_endpoint x2, private_access_settings (public off), workspace
    unity_catalog/                # metastore, assignment, catalog bucket + storage cred + ext location,
                                  #   catalog/schema/volume "libraries", grants
    secrets_access/               # CMK, sample secret, resource policy, IAM role + instance profile,
                                  #   databricks_instance_profile, cluster policy enforcing it
```

## Python wheel (`dbx_aws_secrets`)
- `get_secret(name, *, version_stage="AWSCURRENT", as_json=False)`, `get_secret_json(...)`
- boto3 default credential chain → instance profile via IMDS (no keys, no passwords)
- Region resolution: arg → env `AWS_REGION` → IMDS → error
- Optional `role_arn` to assume role (cross-account / per-team secrets)
- In-process TTL cache, botocore retries, typed exceptions, values never logged; `repr` redaction
- Uses regional endpoint; private DNS on the interface endpoint keeps traffic in the VPC
- No dependencies beyond boto3 (preinstalled in DBR) → works with no egress
- Tests: pytest + moto; build: `python -m build` / bundle `artifacts` section

## Bundle
- `artifacts.default` builds wheel from `python/`
- `workspace.artifact_path: /Volumes/<catalog>/<schema>/libraries` (UC volume)
- Job: DBR 15.4 LTS, **dedicated (single-user) access mode** (standard mode blocks IMDS → instance profile unusable), `aws_attributes.instance_profile_arn`, library = wheel
- Targets: `dev` (host = private workspace URL, reachable over VPN)

## README sections
1. What gets built (diagram) + AWS⇄Azure glossary
2. AWS account setup: root user MFA, IAM Identity Center admin user (≈ Entra ID), budgets/alerts, AWS CLI + SSO profile
3. Databricks account: subscribe (marketplace / direct), enable PrivateLink (Enterprise tier), account-level service principal + OAuth secret
4. Tooling: Terraform, AWS CLI v2, Databricks CLI, Python 3.11, OpenVPN/AWS VPN client, easy-rsa
5. Bootstrap state → deploy infra → generate VPN certs → connect VPN → verify DNS
6. Deploy bundle → run job → expected output
7. Using the wheel org-wide (install from volume, cluster policy, grants)
8. Adding a secret + granting access (IAM policy / tags-based ABAC)
9. Security notes for PII, troubleshooting, teardown, cost notes

## Assumptions / risks
- Databricks account must be **Enterprise tier** for PrivateLink.
- No egress: legacy Hive metastore disabled (UC only); no PyPI installs.
- Client VPN uses mutual TLS certs (simplest); SAML via IAM Identity Center noted as upgrade.
- I cannot run `terraform apply`/bundle deploy here; I'll validate with `terraform fmt/validate`, `pytest`, and `databricks bundle validate` if CLIs are available.
