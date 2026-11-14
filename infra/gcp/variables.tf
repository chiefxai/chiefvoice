variable "project_id" { type = string }
variable "region" { type = string
  default = "us-central1"
}
variable "zone" { type = string
  default = "us-central1-a"
}
variable "vm_machine_type" { type = string
  default = "e2-standard-4"
}
variable "vm_disk_gb" { type = number
  default = 100
}
variable "object_storage_bucket_name" { type = string }
variable "public_domain" {
  type        = string
  description = "DNS name pointed at the static backend_external_ip; Caddy manages HTTPS certificates."
}
variable "oidc_issuer" { type = string }
variable "oidc_audience" { type = string }
variable "oidc_jwks_url" { type = string }
variable "vertex_location" { type = string
  default = "us-central1"
}
variable "vertex_model" { type = string
  default = "google/gemini-live-2.5-flash-native-audio"
}
variable "post_call_model" { type = string
  default = "gemini-2.5-flash"
}
variable "tenant_project_parent" { type = string }
variable "tenant_billing_account_id" { type = string }
variable "budget_api_project_id" { type = string }
variable "vertex_monthly_spend_cap" { type = string
  default = "100"
}
variable "cloud_build_service_account_email" {
  type        = string
  description = "Service account used by the Cloud Build trigger that publishes the VM image and deploys the post-call function."
}
variable "labels" { type = map(string)
  default = { app = "chiefvoice", managed_by = "terraform" }
}
