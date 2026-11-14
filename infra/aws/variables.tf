variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "vm_ami_id" {
  type        = string
  description = "Ubuntu 24.04 LTS AMI in the selected region."
}
variable "vm_instance_type" { type = string
  default = "t3.large"
}
variable "vm_disk_gb" { type = number
  default = 100
}
variable "ssh_allowed_cidr" {
  type        = string
  description = "Admin IP CIDR; use a narrow range."
}
variable "ssh_key_name" { type = string
  default = null
}
variable "api_image_uri" {
  type        = string
  description = "Chiefvoice API image URI."
}
variable "post_call_lambda_image_uri" {
  type        = string
  description = "ARM64 Lambda image URI."
}
variable "object_storage_bucket_name" { type = string }
variable "database_name" { type = string
  default = "chiefvoice"
}
variable "database_username" { type = string
  default = "chiefvoice"
}
variable "tags" {
  type    = map(string)
  default = { Application = "chiefvoice", ManagedBy = "terraform" }
}
