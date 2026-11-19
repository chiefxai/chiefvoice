output "backend_external_ip" { value = google_compute_address.backend.address }
output "backend_https_url" { value = "https://${var.public_domain}" }
output "artifact_registry_repository" { value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}" }
output "post_call_tasks_queue" { value = google_cloud_tasks_queue.post_call.id }
output "post_call_task_invoker_service_account" { value = google_service_account.task_invoker.email }
output "functions_vpc_connector" { value = google_vpc_access_connector.functions.name }
output "object_storage_bucket" { value = google_storage_bucket.objects.name }
output "object_storage_endpoint" { value = "https://storage.googleapis.com" }
output "object_storage_signing_region" { value = "auto" }
output "vm_backend_service_account" { value = google_service_account.backend.email }
output "post_call_function_service_account" { value = google_service_account.post_call_function.email }
output "cloud_build_service_account" { value = google_service_account.cloud_build.email }
output "github_deploy_service_account" { value = google_service_account.github_deploy.email }
output "github_workload_identity_provider" {
  value = "projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/providers/${google_iam_workload_identity_pool_provider.github.workload_identity_pool_provider_id}"
}
