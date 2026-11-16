variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "zone" {
  type    = string
  default = "us-central1-a"
}
variable "vm_machine_type" {
  type    = string
  default = "e2-standard-4"
}
variable "vm_disk_gb" {
  type    = number
  default = 100
}
variable "object_storage_bucket_name" { type = string }
variable "public_domain" {
  type        = string
  description = "DNS name pointed at the static backend_external_ip; Caddy manages HTTPS certificates."
}
variable "oidc_issuer" {
  type        = string
  default     = ""
  description = "Leave empty for infrastructure-only bootstrap; set before deploying the public console/API."
}
variable "oidc_audience" {
  type        = string
  default     = ""
  description = "Leave empty for infrastructure-only bootstrap; set before deploying the public console/API."
}
variable "oidc_jwks_url" {
  type        = string
  default     = ""
  description = "Leave empty for infrastructure-only bootstrap; set before deploying the public console/API."
}
variable "oidc_tenant_claim" {
  type        = string
  default     = "https://chiefvoice.elvoryx.in/tenant_id"
  description = "OIDC claim containing the platform tenant ID. Auth0 custom claims must be namespaced."
}
variable "platform_admin_subjects" {
  type        = list(string)
  default     = []
  description = "OIDC subject IDs allowed to provision and manage customer accounts."
}
variable "vertex_location" {
  type    = string
  default = "us-central1"
}
variable "vertex_model" {
  type    = string
  default = "google/gemini-live-2.5-flash-native-audio"
}
variable "post_call_model" {
  type    = string
  default = "gemini-3.5-flash-lite"
}
variable "post_call_location" {
  type    = string
  default = "global"
}
variable "labels" {
  type    = map(string)
  default = { app = "chiefvoice", managed_by = "terraform" }
}
