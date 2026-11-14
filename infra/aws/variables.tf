variable "aws_region" { type = string
  default = "us-east-1" }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
variable "certificate_arn" { type = string
  description = "ACM certificate for the HTTPS load balancer." }
variable "api_image_uri" { type = string
  description = "API container image in ECR or another supported registry." }
variable "post_call_lambda_image_uri" { type = string
  description = "Post-call Lambda container image URI." }
variable "oidc_issuer" { type = string }
variable "oidc_audience" { type = string }
variable "oidc_jwks_url" { type = string }
variable "vobiz_public_url" {
  type        = string
  description = "HTTPS public base URL used to generate Vobiz callbacks, including the configured DNS name."
}
variable "vertex_project_id" { type = string }
variable "vertex_location" { type = string
  default = "us-central1" }
variable "gcp_project_parent" { type = string }
variable "gcp_billing_account_id" { type = string }
variable "gcp_runtime_service_account" { type = string }
variable "gcp_budget_api_project_id" { type = string }
variable "gcp_vertex_monthly_spend_cap" { type = string
  default = "100" }
variable "vertex_credentials_secret_arn" {
  type        = string
  default     = ""
  description = "Optional Secrets Manager ARN containing the Google workload credential JSON. Prefer workload identity federation."
}
variable "api_desired_count" { type = number
  default = 0 }
variable "api_max_capacity" { type = number
  default = 10 }
variable "lambda_reserved_concurrency" { type = number
  default = 10 }
variable "alarm_sns_topic_arn" {
  type        = string
  default     = ""
  description = "Optional SNS topic for CloudWatch alarm notifications."
}
variable "database_instance_class" { type = string
  default = "db.t4g.medium" }
variable "database_multi_az" {
  type        = bool
  default     = true
  description = "Use a standby database instance for availability; increases database cost."
}
variable "database_engine_version" {
  type        = string
  default     = null
  description = "Optional pinned PostgreSQL minor version. Null uses the AWS account's current default."
}
variable "database_storage_gb" { type = number
  default = 50 }
variable "database_name" { type = string
  default = "chiefvoice" }
variable "database_username" { type = string
  default = "chiefvoice" }
variable "object_storage_bucket_name" { type = string }
variable "recordings_enabled" { type = bool
  default = false }
variable "retention_policies_json" { type = string
  default = "[]" }
variable "backup_policies_json" { type = string
  default = "[]" }
variable "tags" {
  type = map(string)
  default = { Application = "chiefvoice", ManagedBy = "terraform" }
}
