output "backend_external_ip" { value = google_compute_instance.backend.network_interface[0].access_config[0].nat_ip }
output "post_call_tasks_queue" { value = google_cloud_tasks_queue.post_call.id }
output "post_call_task_invoker_service_account" { value = google_service_account.task_invoker.email }
output "functions_vpc_connector" { value = google_vpc_access_connector.functions.name }
output "object_storage_bucket" { value = google_storage_bucket.objects.name }
output "object_storage_endpoint" { value = "https://storage.googleapis.com" }
output "object_storage_signing_region" { value = "auto" }
