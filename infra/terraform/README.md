# Temporal Cloud Worker infrastructure

`modules/temporal_worker` provisions the binding T09 Worker topology.
`environments/staging` and `environments/prod` are intentionally value-free:
approved account, network, namespace, credential and endpoint values enter via
the deployment system and must never be committed in a tfvars file.

## State and validation

Initialise each root with reviewed S3 backend arguments. State encryption,
locking and access policy belong to the existing account bootstrap; this repo
does not invent bucket or role names.

```sh
terraform -chdir=infra/terraform/environments/staging init \
  -backend-config=<reviewed-staging-backend.hcl>
terraform -chdir=infra/terraform/environments/staging validate
terraform -chdir=infra/terraform/environments/staging plan \
  -var-file=<approved-staging-inputs.tfvars>
```

Never commit either referenced input file. The same commands apply to `prod`
only after T10 and the separate go-live approval.

## Required existing infrastructure

The module deliberately does not create broad shared infrastructure. Inputs
must identify an existing ECS cluster, private subnets, RDS security group,
interface VPC endpoints, immutable application/ADOT images, execution role,
alarm topics, exact RDS/S3/KMS/Secrets resources, and approved Temporal/provider
CIDRs. A code-owned `module:attribute` dependency factory in the immutable
application image must return `orchestration.runtime.WorkerDependencies`.

## Bootstrap

The `bootstrap_task_definition_arn` output points at the control task
definition. Run it once with an ECS container command override of:

```text
python -m orchestration.bootstrap
```

Wait for exit code zero. A second run must also exit zero and create nothing;
any Schedule drift exits non-zero and blocks deployment.
