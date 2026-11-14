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
variable "api_image_uri" { type = string }
variable "object_storage_bucket_name" { type = string }
variable "post_call_function_url" { type = string
  description = "HTTPS URL of the Cloud Function deployed by Cloud Build."
}
variable "labels" { type = map(string)
  default = { app = "chiefvoice", managed_by = "terraform" }
}
