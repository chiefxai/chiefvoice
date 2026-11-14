locals {
  name = "chiefvoice"
  runtime_secret_names = toset([
    "credentials-encryption-key",
    "post-call-encryption-key",
    "recordings-encryption-key",
    "backup-encryption-key",
    "vobiz-webhook-secret",
    "vobiz-route-token",
  ])
}

resource "random_password" "database" {
  length  = 40
  special = false
}

resource "random_bytes" "fernet" {
  for_each = toset(["credentials", "post-call", "recordings", "backup"])
  length   = 32
}

resource "random_password" "vobiz" {
  for_each = toset(["webhook", "route-token"])
  length   = 48
  special  = false
}

resource "aws_kms_key" "storage" {
  description             = "Chiefvoice object storage encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_s3_bucket" "objects" {
  bucket        = var.object_storage_bucket_name
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "objects" {
  bucket                  = aws_s3_bucket.objects.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms"
  kms_master_key_id = aws_kms_key.storage.arn }
    bucket_key_enabled = true
  }
}

resource "aws_db_subnet_group" "postgres" {
  name       = "${local.name}-postgres"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "database" {
  name        = "${local.name}-postgres"
  description = "Postgres from Chiefvoice API and post-call Lambda only"
  vpc_id      = var.vpc_id
  ingress {
    description     = "API and post-call worker"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id, aws_security_group.post_call_lambda.id]
  }
  egress { from_port = 0
  to_port = 0
  protocol = "-1"
  cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_db_instance" "postgres" {
  identifier                  = "${local.name}-postgres"
  engine                      = "postgres"
  engine_version              = var.database_engine_version
  instance_class              = var.database_instance_class
  multi_az                    = var.database_multi_az
  allocated_storage           = var.database_storage_gb
  max_allocated_storage       = var.database_storage_gb * 3
  storage_encrypted           = true
  db_name                     = var.database_name
  username                    = var.database_username
  password                    = random_password.database.result
  db_subnet_group_name        = aws_db_subnet_group.postgres.name
  vpc_security_group_ids      = [aws_security_group.database.id]
  publicly_accessible         = false
  backup_retention_period     = 14
  copy_tags_to_snapshot       = true
  auto_minor_version_upgrade  = true
  deletion_protection         = true
  skip_final_snapshot         = false
  final_snapshot_identifier   = "${local.name}-final"
  performance_insights_enabled = true
}

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${local.name}/database-url"
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql+asyncpg://${var.database_username}:${urlencode(random_password.database.result)}@${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}/${var.database_name}?ssl=require"
}

resource "aws_secretsmanager_secret" "runtime" {
  for_each                = local.runtime_secret_names
  name                    = "${local.name}/${each.value}"
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret_version" "fernet" {
  for_each      = random_bytes.fernet
  secret_id     = aws_secretsmanager_secret.runtime["${each.key}-encryption-key"].id
  secret_string = each.value.base64
}

resource "aws_secretsmanager_secret_version" "vobiz" {
  for_each      = random_password.vobiz
  secret_id     = aws_secretsmanager_secret.runtime["vobiz-${each.key == "webhook" ? "webhook-secret" : "route-token"}"].id
  secret_string = each.value.result
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name}-api"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "post_call" {
  name              = "/aws/lambda/${local.name}-post-call"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "migrations" {
  name              = "/ecs/${local.name}-migrations"
  retention_in_days = 30
}

resource "aws_ecs_cluster" "api" {
  name = "${local.name}-api"
  setting { name = "containerInsights"
  value = "enabled" }
}

resource "aws_security_group" "load_balancer" {
  name   = "${local.name}-alb"
  vpc_id = var.vpc_id
  ingress { from_port = 80
  to_port = 80
  protocol = "tcp"
  cidr_blocks = ["0.0.0.0/0"] }
  ingress { from_port = 443
  to_port = 443
  protocol = "tcp"
  cidr_blocks = ["0.0.0.0/0"] }
  egress { from_port = 0
  to_port = 0
  protocol = "-1"
  cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "api" {
  name   = "${local.name}-api"
  vpc_id = var.vpc_id
  ingress { from_port = 8080
  to_port = 8080
  protocol = "tcp"
  security_groups = [aws_security_group.load_balancer.id] }
  egress { from_port = 0
  to_port = 0
  protocol = "-1"
  cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "post_call_lambda" {
  name   = "${local.name}-post-call"
  vpc_id = var.vpc_id
  egress { from_port = 0
  to_port = 0
  protocol = "-1"
  cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_lb" "api" {
  name               = "${local.name}-api"
  load_balancer_type = "application"
  internal           = false
  subnets            = var.public_subnet_ids
  security_groups    = [aws_security_group.load_balancer.id]
  idle_timeout       = 4000
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name}-api"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check { path = "/readyz"
  matcher = "200" }
  deregistration_delay = 30
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action { type = "redirect"
  redirect { port = "443"
  protocol = "HTTPS"
  status_code = "HTTP_301" } }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  default_action { type = "forward"
  target_group_arn = aws_lb_target_group.api.arn }
}

resource "aws_iam_role" "task_execution" {
  name = "${local.name}-ecs-execution"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name = "${local.name}-read-runtime-secrets"
  role = aws_iam_role.task_execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = concat([aws_secretsmanager_secret.database_url.arn], [for secret in aws_secretsmanager_secret.runtime : secret.arn], var.vertex_credentials_secret_arn == "" ? [] : [var.vertex_credentials_secret_arn]) }] })
}

resource "aws_iam_role" "api_task" {
  name = "${local.name}-api-task"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}

resource "aws_iam_role_policy" "api_task" {
  name = "${local.name}-api-runtime"
  role = aws_iam_role.api_task.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = [aws_s3_bucket.objects.arn], Condition = { StringLike = { "s3:prefix" = ["chiefvoice/*"] } } },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = ["${aws_s3_bucket.objects.arn}/chiefvoice/*"] },
    { Effect = "Allow", Action = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"], Resource = [aws_kms_key.storage.arn] },
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = [aws_lambda_function.post_call.arn] },
  ] })
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "2048"
  memory                   = "4096"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.api_task.arn
  container_definitions = jsonencode([{
    name = "api", image = var.api_image_uri, essential = true,
    portMappings = [{ containerPort = 8080, protocol = "tcp" }],
    environment = [
      { name = "APP_ENV", value = "production" },
      { name = "REALTIME_AGENT_PROVIDER", value = "vertex" },
      { name = "POST_CALL_AGENT_PROVIDER", value = "langchain_vertex" },
      { name = "POST_CALL_EXECUTION_BACKEND", value = "lambda" },
      { name = "POST_CALL_LAMBDA_FUNCTION_NAME", value = aws_lambda_function.post_call.function_name },
      { name = "POST_CALL_LAMBDA_REGION", value = var.aws_region },
      { name = "VERTEX_PROJECT_ISOLATION_ENABLED", value = "true" },
      { name = "VERTEX_PROJECT_ID", value = var.vertex_project_id },
      { name = "VERTEX_LOCATION", value = var.vertex_location },
      { name = "GCP_PROJECT_PARENT", value = var.gcp_project_parent },
      { name = "GCP_BILLING_ACCOUNT_ID", value = var.gcp_billing_account_id },
      { name = "GCP_RUNTIME_SERVICE_ACCOUNT", value = var.gcp_runtime_service_account },
      { name = "GCP_BUDGET_API_PROJECT_ID", value = var.gcp_budget_api_project_id },
      { name = "GCP_VERTEX_MONTHLY_SPEND_CAP", value = var.gcp_vertex_monthly_spend_cap },
      { name = "OIDC_ISSUER", value = var.oidc_issuer },
      { name = "OIDC_AUDIENCE", value = var.oidc_audience },
      { name = "OIDC_JWKS_URL", value = var.oidc_jwks_url },
      { name = "VOBIZ_PUBLIC_URL", value = var.vobiz_public_url },
      { name = "OBJECT_STORAGE_BUCKET", value = aws_s3_bucket.objects.bucket },
      { name = "OBJECT_STORAGE_REGION", value = var.aws_region },
      { name = "OBJECT_STORAGE_PREFIX", value = "chiefvoice" },
      { name = "RECORDINGS_STORAGE_BACKEND", value = "s3" },
      { name = "BACKUP_STORAGE_BACKEND", value = "s3" },
      { name = "RECORDINGS_ENABLED", value = tostring(var.recordings_enabled) },
      { name = "RETENTION_POLICIES_JSON", value = var.retention_policies_json },
      { name = "BACKUP_POLICIES_JSON", value = var.backup_policies_json },
      { name = "OTEL_SERVICE_NAME", value = "chiefvoice-api" },
    ],
    secrets = concat([
      { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      { name = "CREDENTIALS_ENCRYPTION_KEY", valueFrom = aws_secretsmanager_secret.runtime["credentials-encryption-key"].arn },
      { name = "POST_CALL_ENCRYPTION_KEY", valueFrom = aws_secretsmanager_secret.runtime["post-call-encryption-key"].arn },
      { name = "RECORDINGS_ENCRYPTION_KEY", valueFrom = aws_secretsmanager_secret.runtime["recordings-encryption-key"].arn },
      { name = "BACKUP_ENCRYPTION_KEY", valueFrom = aws_secretsmanager_secret.runtime["backup-encryption-key"].arn },
      { name = "VOBIZ_WEBHOOK_SECRET", valueFrom = aws_secretsmanager_secret.runtime["vobiz-webhook-secret"].arn },
      { name = "VOBIZ_INBOUND_ROUTE_TOKEN", valueFrom = aws_secretsmanager_secret.runtime["vobiz-route-token"].arn },
    ], var.vertex_credentials_secret_arn == "" ? [] : [{ name = "VERTEX_CREDENTIALS_JSON", valueFrom = var.vertex_credentials_secret_arn }]),
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.api.name, "awslogs-region" = var.aws_region, "awslogs-stream-prefix" = "api" } },
    healthCheck = { command = ["CMD-SHELL", "python -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:8080/readyz\", timeout=3)'"], interval = 30, timeout = 5, retries = 3, startPeriod = 60 },
  }])
}

resource "aws_ecs_task_definition" "migrations" {
  family                   = "${local.name}-migrations"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_execution.arn
  container_definitions = jsonencode([{
    name = "migrations", image = var.api_image_uri, essential = true,
    command = ["alembic", "upgrade", "head"],
    secrets = [{ name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn }],
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.migrations.name, "awslogs-region" = var.aws_region, "awslogs-stream-prefix" = "migrations" } },
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.api.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"
  deployment_circuit_breaker { enable = true
  rollback = true }
  network_configuration { subnets = var.private_subnet_ids
  security_groups = [aws_security_group.api.id]
  assign_public_ip = false }
  load_balancer { target_group_arn = aws_lb_target_group.api.arn
  container_name = "api"
  container_port = 8080 }
  depends_on = [aws_lb_listener.https]
}

resource "aws_appautoscaling_target" "api" {
  max_capacity       = var.api_max_capacity
  min_capacity       = var.api_desired_count
  resource_id        = "service/${aws_ecs_cluster.api.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  target_tracking_scaling_policy_configuration { target_value = 65
  predefined_metric_specification { predefined_metric_type = "ECSServiceAverageCPUUtilization" } }
}

resource "aws_iam_role" "lambda" {
  name = "${local.name}-post-call-lambda"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}

resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "lambda_secrets" {
  name = "${local.name}-post-call-secrets"
  role = aws_iam_role.lambda.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = concat([aws_secretsmanager_secret.database_url.arn, aws_secretsmanager_secret.runtime["post-call-encryption-key"].arn], var.vertex_credentials_secret_arn == "" ? [] : [var.vertex_credentials_secret_arn]) }] })
}

resource "aws_lambda_function" "post_call" {
  function_name                  = "${local.name}-post-call"
  package_type                   = "Image"
  image_uri                      = var.post_call_lambda_image_uri
  architectures                  = ["arm64"]
  role                           = aws_iam_role.lambda.arn
  timeout                        = 240
  memory_size                    = 2048
  reserved_concurrent_executions = var.lambda_reserved_concurrency
  vpc_config { subnet_ids = var.private_subnet_ids
  security_group_ids = [aws_security_group.post_call_lambda.id] }
  environment {
    variables = {
      POST_CALL_AGENT_PROVIDER       = "langchain_vertex"
      VERTEX_PROJECT_ISOLATION_ENABLED = "true"
      VERTEX_PROJECT_ID              = var.vertex_project_id
      VERTEX_LOCATION                = var.vertex_location
      POST_CALL_MODEL                = "gemini-2.5-flash"
      DATABASE_URL_SECRET_ARN         = aws_secretsmanager_secret.database_url.arn
      POST_CALL_ENCRYPTION_KEY_SECRET_ARN = aws_secretsmanager_secret.runtime["post-call-encryption-key"].arn
      GOOGLE_CREDENTIALS_SECRET_ARN   = var.vertex_credentials_secret_arn
    }
  }
  depends_on = [aws_cloudwatch_log_group.post_call, aws_iam_role_policy.lambda_secrets]
}

resource "aws_lambda_function_event_invoke_config" "post_call" {
  function_name                = aws_lambda_function.post_call.function_name
  maximum_event_age_in_seconds = 300
  maximum_retry_attempts       = 2
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${local.name}-post-call-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.post_call.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${local.name}-post-call-throttles"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = aws_lambda_function.post_call.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_async_dropped" {
  alarm_name          = "${local.name}-post-call-async-dropped"
  namespace           = "AWS/Lambda"
  metric_name         = "AsyncEventsDropped"
  dimensions          = { FunctionName = aws_lambda_function.post_call.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_async_age" {
  alarm_name          = "${local.name}-post-call-async-age"
  namespace           = "AWS/Lambda"
  metric_name         = "AsyncEventAge"
  dimensions          = { FunctionName = aws_lambda_function.post_call.function_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 120000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}
