locals {
  roles = {
    control = {
      desired_count = 2
      concurrency   = 20
    }
    docintel = {
      desired_count = 2
      concurrency   = 4
    }
    effects = {
      desired_count = 1
      concurrency   = 5
    }
    ledger = {
      desired_count = 1
      concurrency   = 1
    }
  }

  name = "pacha-${var.environment}-temporal"
  tags = merge(var.tags, {
    Application = "pacha"
    Environment = var.environment
    ManagedBy   = "terraform"
    Runtime     = "temporal"
  })
}

resource "aws_cloudwatch_log_group" "worker" {
  for_each = local.roles

  name              = "/pacha/${var.environment}/temporal/${each.key}"
  retention_in_days = var.log_retention_days
  tags              = merge(local.tags, { WorkerRole = each.key })
}

resource "aws_cloudwatch_log_group" "telemetry" {
  name              = "/pacha/${var.environment}/temporal/telemetry"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_security_group" "worker" {
  name_prefix = "${local.name}-worker-"
  description = "No-ingress, allowlisted egress for Pacha Temporal Workers"
  vpc_id      = var.vpc_id
  tags        = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "temporal" {
  for_each = var.temporal_cloud_cidr_blocks

  security_group_id = aws_security_group.worker.id
  description       = "Temporal Cloud mTLS"
  cidr_ipv4         = each.value
  from_port         = 7233
  to_port           = 7233
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "provider_https" {
  for_each = var.provider_https_cidr_blocks

  security_group_id = aws_security_group.worker.id
  description       = "Approved external provider HTTPS"
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "aws_endpoints" {
  for_each = var.aws_endpoint_security_group_ids

  security_group_id            = aws_security_group.worker.id
  description                  = "Required AWS interface endpoints"
  referenced_security_group_id = each.value
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "s3" {
  security_group_id = aws_security_group.worker.id
  description       = "S3 gateway endpoint"
  prefix_list_id    = var.s3_prefix_list_id
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "postgres" {
  security_group_id            = aws_security_group.worker.id
  description                  = "Authoritative PostgreSQL"
  referenced_security_group_id = var.rds_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "dns_udp" {
  security_group_id = aws_security_group.worker.id
  description       = "VPC resolver UDP"
  cidr_ipv4         = var.vpc_dns_resolver_cidr
  from_port         = 53
  to_port           = 53
  ip_protocol       = "udp"
}

resource "aws_vpc_security_group_egress_rule" "dns_tcp" {
  security_group_id = aws_security_group.worker.id
  description       = "VPC resolver TCP fallback"
  cidr_ipv4         = var.vpc_dns_resolver_cidr
  from_port         = 53
  to_port           = 53
  ip_protocol       = "tcp"
}

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  for_each = local.roles

  name_prefix        = "pacha-${var.environment}-${each.key}-"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = merge(local.tags, { WorkerRole = each.key })
}

data "aws_iam_policy_document" "worker" {
  for_each = local.roles

  statement {
    sid     = "TemporalAndApplicationSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = setunion(
      var.application_secret_arns[each.key],
      toset([
        var.temporal_tls_cert_secret_arn,
        var.temporal_tls_key_secret_arn,
        var.database_secret_arn,
      ])
    )
  }

  statement {
    sid     = "TemporalAndApplicationKms"
    actions = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = setunion(
      var.application_kms_key_arns[each.key],
      toset([var.temporal_kms_key_arn])
    )
  }

  statement {
    sid       = "DatabaseConnect"
    actions   = ["rds-db:connect"]
    resources = var.rds_db_user_arns[each.key]
  }

  statement {
    sid       = "ObjectReadWrite"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = var.s3_object_arns[each.key]
  }

  statement {
    sid       = "ObjectList"
    actions   = ["s3:ListBucket"]
    resources = var.s3_bucket_arns[each.key]
  }

  statement {
    sid       = "OperationalMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Pacha/Temporal"]
    }
  }

  statement {
    sid       = "TelemetryEmfLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.telemetry.arn}:*"]
  }
}

resource "aws_iam_role_policy" "worker" {
  for_each = local.roles

  name   = "pacha-temporal-${each.key}"
  role   = aws_iam_role.worker[each.key].id
  policy = data.aws_iam_policy_document.worker[each.key].json
}

resource "aws_ecs_task_definition" "worker" {
  for_each = local.roles

  family                   = "pacha-${var.environment}-temporal-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = var.task_execution_role_arn
  task_role_arn            = aws_iam_role.worker[each.key].arn

  container_definitions = jsonencode([
    {
      name        = "worker"
      image       = var.image_uri
      essential   = true
      command     = ["python", "-m", "orchestration.runtime"]
      stopTimeout = 120
      environment = [
        { name = "PACHA_ENV", value = var.environment },
        { name = "PACHA_TEMPORAL_MODE", value = "cloud" },
        { name = "PACHA_TEMPORAL_ADDRESS", value = var.temporal_address },
        { name = "PACHA_TEMPORAL_NAMESPACE", value = var.temporal_namespace },
        { name = "PACHA_TEMPORAL_REGION", value = var.temporal_region },
        { name = "PACHA_TEMPORAL_TLS_CERT_SECRET_ARN", value = var.temporal_tls_cert_secret_arn },
        { name = "PACHA_TEMPORAL_TLS_KEY_SECRET_ARN", value = var.temporal_tls_key_secret_arn },
        { name = "PACHA_TEMPORAL_KMS_KEY_ARN", value = var.temporal_kms_key_arn },
        { name = "PACHA_TEMPORAL_QUEUE_PREFIX", value = "pacha-${var.environment}" },
        { name = "PACHA_BUILD_ID", value = var.build_id },
        { name = "PACHA_WORKER_ROLE", value = each.key },
        { name = "PACHA_WORKER_ACTIVITY_CONCURRENCY", value = tostring(each.value.concurrency) },
        { name = "PACHA_WORKER_DEPENDENCIES_FACTORY", value = var.worker_dependencies_factory },
      ]
      secrets = [
        { name = "DATABASE_URL", valueFrom = var.database_secret_value_from },
      ]
      portMappings = []
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.worker[each.key].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "${var.build_id}/${each.key}"
        }
      }
      dockerLabels = {
        "pacha.build_id"    = var.build_id
        "pacha.worker_role" = each.key
      }
    },
    {
      name      = "otel-collector"
      image     = var.otel_collector_image_uri
      essential = true
      command   = ["--config=env:AOT_CONFIG_CONTENT"]
      environment = [
        {
          name = "AOT_CONFIG_CONTENT"
          value = yamlencode({
            receivers = {
              otlp = { protocols = { grpc = { endpoint = "127.0.0.1:4317" } } }
            }
            processors = { batch = {} }
            exporters = {
              awsemf = {
                namespace                        = "Pacha/TemporalSDK"
                log_group_name                   = aws_cloudwatch_log_group.telemetry.name
                log_stream_name                  = "${var.build_id}/${each.key}"
                dimension_rollup_option          = "NoDimensionRollup"
                resource_to_telemetry_conversion = { enabled = true }
              }
            }
            service = {
              pipelines = {
                metrics = {
                  receivers  = ["otlp"]
                  processors = ["batch"]
                  exporters  = ["awsemf"]
                }
              }
            }
          })
        }
      ]
      portMappings = []
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.telemetry.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "collector/${var.build_id}/${each.key}"
        }
      }
    }
  ])

  tags = merge(local.tags, {
    BuildId    = var.build_id
    WorkerRole = each.key
  })
}

resource "aws_ecs_service" "worker" {
  for_each = local.roles

  name            = "pacha-${var.environment}-temporal-${each.key}"
  cluster         = var.ecs_cluster_arn
  task_definition = aws_ecs_task_definition.worker[each.key].arn
  desired_count   = each.value.desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = each.key == "ledger" ? 0 : 50
  deployment_maximum_percent         = each.key == "ledger" ? 100 : 200
  enable_execute_command             = false
  wait_for_steady_state              = true

  network_configuration {
    assign_public_ip = false
    security_groups  = [aws_security_group.worker.id]
    subnets          = var.subnet_ids
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = merge(local.tags, {
    BuildId    = var.build_id
    WorkerRole = each.key
  })

  lifecycle {
    precondition {
      condition     = each.key != "ledger" || each.value.desired_count == 1
      error_message = "ledger must remain a single ECS service task"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "ecs_no_running_tasks" {
  for_each = local.roles

  alarm_name          = "pacha-${var.environment}-temporal-${each.key}-no-running-tasks"
  alarm_description   = "Worker role has no running ECS task"
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  dimensions          = { ClusterName = basename(var.ecs_cluster_arn), ServiceName = aws_ecs_service.worker[each.key].name }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_sns_topic_arns
  ok_actions          = var.alarm_sns_topic_arns
  tags                = merge(local.tags, { WorkerRole = each.key })
}

locals {
  application_alarms = {
    outbox_oldest = {
      metric      = "OutboxOldestAgeSeconds"
      threshold   = 300
      period      = 60
      evaluations = 1
      statistic   = "Maximum"
      comparison  = "GreaterThanThreshold"
    }
    ledger_oldest = {
      metric      = "LedgerOldestAgeSeconds"
      threshold   = 60
      period      = 60
      evaluations = 1
      statistic   = "Maximum"
      comparison  = "GreaterThanThreshold"
    }
    ledger_hash_failure = {
      metric      = "LedgerHashFailureCount"
      threshold   = 0
      period      = 60
      evaluations = 1
      statistic   = "Sum"
      comparison  = "GreaterThanThreshold"
    }
    uncertain_write = {
      metric      = "UncertainWriteCount"
      threshold   = 0
      period      = 60
      evaluations = 1
      statistic   = "Sum"
      comparison  = "GreaterThanThreshold"
    }
    control_schedule_to_start = {
      metric      = "ControlScheduleToStartP95Seconds"
      threshold   = 30
      period      = 60
      evaluations = 10
      statistic   = "Maximum"
      comparison  = "GreaterThanThreshold"
    }
    control_no_poll = {
      metric      = "ControlSecondsSinceLastPoll"
      threshold   = 120
      period      = 60
      evaluations = 1
      statistic   = "Maximum"
      comparison  = "GreaterThanThreshold"
    }
    codec_failure = {
      metric      = "CodecKmsFailureCount"
      threshold   = 0
      period      = 60
      evaluations = 1
      statistic   = "Sum"
      comparison  = "GreaterThanThreshold"
    }
    schedule_action_failure = {
      metric      = "ScheduleActionFailureCount"
      threshold   = 0
      period      = 60
      evaluations = 1
      statistic   = "Sum"
      comparison  = "GreaterThanThreshold"
    }
    workflow_failure_rate = {
      metric      = "WorkflowFailureRatePercent"
      threshold   = 1
      period      = 900
      evaluations = 1
      statistic   = "Average"
      comparison  = "GreaterThanThreshold"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "application" {
  for_each = local.application_alarms

  alarm_name          = "pacha-${var.environment}-temporal-${replace(each.key, "_", "-")}"
  alarm_description   = "Binding Temporal master-plan operational alarm"
  namespace           = "Pacha/Temporal"
  metric_name         = each.value.metric
  dimensions          = { Environment = var.environment }
  statistic           = each.value.statistic
  period              = each.value.period
  evaluation_periods  = each.value.evaluations
  datapoints_to_alarm = each.value.evaluations
  threshold           = each.value.threshold
  comparison_operator = each.value.comparison
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_sns_topic_arns
  ok_actions          = var.alarm_sns_topic_arns
  tags                = local.tags
}
