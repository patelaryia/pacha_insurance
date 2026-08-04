output "temporal_worker_services" {
  value = module.temporal_workers.service_arns
}

output "temporal_worker_task_definitions" {
  value = module.temporal_workers.task_definition_arns
}

output "temporal_worker_log_groups" {
  value = module.temporal_workers.log_group_names
}
