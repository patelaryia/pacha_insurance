variable "aws_region" { type = string }
variable "ecs_cluster_arn" { type = string }
variable "subnet_ids" { type = list(string) }
variable "vpc_id" { type = string }
variable "vpc_dns_resolver_cidr" { type = string }
variable "temporal_cloud_cidr_blocks" { type = set(string) }
variable "provider_https_cidr_blocks" {
  type    = set(string)
  default = []
}
variable "aws_endpoint_security_group_ids" { type = set(string) }
variable "s3_prefix_list_id" { type = string }
variable "rds_security_group_id" { type = string }
variable "image_uri" { type = string }
variable "otel_collector_image_uri" { type = string }
variable "worker_dependencies_factory" { type = string }
variable "build_id" { type = string }
variable "temporal_address" { type = string }
variable "temporal_namespace" { type = string }
variable "temporal_region" { type = string }
variable "temporal_tls_cert_secret_arn" { type = string }
variable "temporal_tls_key_secret_arn" { type = string }
variable "temporal_kms_key_arn" { type = string }
variable "database_secret_arn" { type = string }
variable "database_secret_value_from" { type = string }
variable "application_secret_arns" { type = map(set(string)) }
variable "rds_db_user_arns" { type = map(set(string)) }
variable "s3_object_arns" { type = map(set(string)) }
variable "s3_bucket_arns" { type = map(set(string)) }
variable "application_kms_key_arns" { type = map(set(string)) }
variable "task_execution_role_arn" { type = string }
variable "alarm_sns_topic_arns" { type = list(string) }
variable "log_retention_days" {
  type    = number
  default = 90
}
variable "tags" {
  type    = map(string)
  default = {}
}
