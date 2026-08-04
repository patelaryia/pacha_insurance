variable "environment" {
  description = "Pacha deployment environment. Temporal Cloud is mandatory here."
  type        = string

  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be staging or prod"
  }
}

variable "aws_region" {
  description = "AWS region containing the Pacha data plane."
  type        = string
}

variable "ecs_cluster_arn" {
  description = "Existing ECS cluster ARN."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet ids used by every Worker service."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "at least two private subnets are required"
  }
}

variable "vpc_id" {
  description = "VPC in which the Worker security group is created."
  type        = string
}

variable "vpc_dns_resolver_cidr" {
  description = "Exact VPC resolver CIDR, normally the VPC base plus two."
  type        = string
}

variable "temporal_cloud_cidr_blocks" {
  description = "Owner-approved Temporal Cloud endpoint CIDRs; never 0.0.0.0/0."
  type        = set(string)

  validation {
    condition = (
      length(var.temporal_cloud_cidr_blocks) > 0 &&
      !contains(var.temporal_cloud_cidr_blocks, "0.0.0.0/0")
    )
    error_message = "supply explicit Temporal Cloud CIDRs; world-open egress is refused"
  }
}

variable "provider_https_cidr_blocks" {
  description = "Owner-approved non-AWS provider HTTPS CIDRs required by docintel/effects."
  type        = set(string)
  default     = []

  validation {
    condition     = !contains(var.provider_https_cidr_blocks, "0.0.0.0/0")
    error_message = "world-open provider egress is refused"
  }
}

variable "aws_endpoint_security_group_ids" {
  description = "Security groups on required interface VPC endpoints (Secrets, KMS, logs and ECR)."
  type        = set(string)

  validation {
    condition     = length(var.aws_endpoint_security_group_ids) > 0
    error_message = "at least one scoped AWS interface-endpoint security group is required"
  }
}

variable "s3_prefix_list_id" {
  description = "AWS-managed S3 gateway endpoint prefix-list id."
  type        = string
}

variable "rds_security_group_id" {
  description = "Security group attached to the authoritative PostgreSQL service."
  type        = string
}

variable "image_uri" {
  description = "Immutable application image URI, pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image_uri))
    error_message = "image_uri must be immutable and end in @sha256:<64 lowercase hex>"
  }
}

variable "otel_collector_image_uri" {
  description = "Immutable AWS Distro for OpenTelemetry collector image URI."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.otel_collector_image_uri))
    error_message = "otel_collector_image_uri must be immutable and digest-pinned"
  }
}

variable "worker_dependencies_factory" {
  description = "Code-owned module:attribute factory returning strict WorkerDependencies."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$", var.worker_dependencies_factory))
    error_message = "worker_dependencies_factory must use module:attribute"
  }
}

variable "build_id" {
  description = "The 40-character git SHA built into image_uri."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.build_id))
    error_message = "build_id must be a full lowercase git SHA"
  }
}

variable "temporal_address" {
  description = "Temporal Cloud namespace endpoint including port."
  type        = string
}

variable "temporal_namespace" {
  description = "Approved Temporal Cloud namespace for this environment."
  type        = string
}

variable "temporal_region" {
  description = "Approved telemetry/DPIA region label paired with the namespace."
  type        = string
}

variable "temporal_tls_cert_secret_arn" {
  description = "Secrets Manager ARN containing the mTLS certificate PEM."
  type        = string
}

variable "temporal_tls_key_secret_arn" {
  description = "Secrets Manager ARN containing the mTLS private-key PEM."
  type        = string
}

variable "temporal_kms_key_arn" {
  description = "Immutable KMS key ARN used by the Temporal Payload Codec."
  type        = string
}

variable "database_secret_value_from" {
  description = "ECS secret valueFrom reference that resolves DATABASE_URL."
  type        = string
}

variable "database_secret_arn" {
  description = "Exact Secrets Manager ARN containing DATABASE_URL; used for IAM scope."
  type        = string
}

variable "application_secret_arns" {
  description = "Additional exact application secret ARNs keyed by Worker role."
  type        = map(set(string))

  validation {
    condition     = toset(keys(var.application_secret_arns)) == toset(["control", "docintel", "effects", "ledger"])
    error_message = "application_secret_arns must declare exactly the four Worker roles"
  }
}

variable "rds_db_user_arns" {
  description = "Exact rds-db user ARNs keyed by Worker role."
  type        = map(set(string))

  validation {
    condition = (
      toset(keys(var.rds_db_user_arns)) == toset(["control", "docintel", "effects", "ledger"]) &&
      alltrue([for resources in values(var.rds_db_user_arns) : length(resources) > 0])
    )
    error_message = "each of the four Worker roles requires at least one exact rds-db user ARN"
  }
}

variable "s3_object_arns" {
  description = "Exact S3 object-prefix ARNs keyed by Worker role."
  type        = map(set(string))

  validation {
    condition     = toset(keys(var.s3_object_arns)) == toset(["control", "docintel", "effects", "ledger"])
    error_message = "s3_object_arns must declare exactly the four Worker roles"
  }
}

variable "s3_bucket_arns" {
  description = "Exact S3 bucket ARNs keyed by Worker role."
  type        = map(set(string))

  validation {
    condition     = toset(keys(var.s3_bucket_arns)) == toset(["control", "docintel", "effects", "ledger"])
    error_message = "s3_bucket_arns must declare exactly the four Worker roles"
  }
}

variable "application_kms_key_arns" {
  description = "Additional immutable KMS key ARNs keyed by Worker role."
  type        = map(set(string))

  validation {
    condition     = toset(keys(var.application_kms_key_arns)) == toset(["control", "docintel", "effects", "ledger"])
    error_message = "application_kms_key_arns must declare exactly the four Worker roles"
  }
}

variable "task_execution_role_arn" {
  description = "Existing ECS execution role for image pulls and awslogs delivery."
  type        = string
}

variable "task_cpu" {
  description = "Fargate CPU units per Worker task."
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "Fargate memory MiB per Worker task."
  type        = number
  default     = 2048
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 90
}

variable "alarm_sns_topic_arns" {
  description = "SNS topics notified by every Temporal operational alarm."
  type        = list(string)

  validation {
    condition     = length(var.alarm_sns_topic_arns) > 0
    error_message = "at least one alarm notification topic is required"
  }
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}
