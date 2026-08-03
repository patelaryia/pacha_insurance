output "service_arns" {
  description = "ECS service ARNs keyed by Worker role."
  value       = { for role, service in aws_ecs_service.worker : role => service.id }
}

output "task_definition_arns" {
  description = "Immutable task-definition revisions keyed by Worker role."
  value       = { for role, definition in aws_ecs_task_definition.worker : role => definition.arn }
}

output "task_role_arns" {
  description = "Least-privilege task roles keyed by Worker role."
  value       = { for role, role_resource in aws_iam_role.worker : role => role_resource.arn }
}

output "worker_security_group_id" {
  description = "No-ingress Worker security group."
  value       = aws_security_group.worker.id
}

output "log_group_names" {
  description = "CloudWatch log groups keyed by Worker role."
  value       = { for role, group in aws_cloudwatch_log_group.worker : role => group.name }
}

output "bootstrap_task_definition_arn" {
  description = "Run the control task definition once with command override: python -m orchestration.bootstrap."
  value       = aws_ecs_task_definition.worker["control"].arn
}
