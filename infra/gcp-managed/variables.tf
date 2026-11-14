variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "database_region" {
  type    = string
  default = "us-central1"
}

variable "artifact_repository" {
  type    = string
  default = "chiefvoice"
}

variable "database_name" {
  type    = string
  default = "chiefvoice"
}

variable "database_user" {
  type    = string
  default = "chiefvoice"
}

variable "database_tier" {
  type    = string
  default = "db-custom-2-7680"
}

variable "database_disk_gb" {
  type    = number
  default = 50
}

variable "tenant_project_parent" {
  type        = string
  description = "Organization or folder that owns newly created customer Vertex projects, e.g. folders/123456."
  validation {
    condition     = startswith(var.tenant_project_parent, "folders/") || startswith(var.tenant_project_parent, "organizations/")
    error_message = "tenant_project_parent must be folders/<id> or organizations/<id>."
  }
}

variable "tenant_billing_account_id" {
  type        = string
  description = "Billing account that pays for customer Vertex projects."
}

variable "budget_api_project_id" {
  type        = string
  description = "Existing project used for Cloud Billing Budgets API quota attribution."
}

variable "vertex_monthly_spend_cap" {
  type        = string
  default     = "100"
  description = "Monthly per-tenant Vertex spend cap, in the billing account currency."
}

variable "object_storage_bucket_name" {
  type = string
}

variable "post_call_function_service_account" {
  type    = string
  default = "chiefvoice-post-call"
}

variable "labels" {
  type = map(string)
  default = {
    app        = "chiefvoice"
    managed_by = "terraform"
  }
}

variable "monitoring_notification_channels" {
  type        = list(string)
  default     = []
  description = "Existing Cloud Monitoring notification channel resource names; create and verify a receiver first."
}
