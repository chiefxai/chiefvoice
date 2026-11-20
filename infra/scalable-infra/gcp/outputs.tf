output "database_connection_name" {
  value = google_sql_database_instance.postgres.connection_name
}

output "database_secret_id" {
  value = google_secret_manager_secret.runtime["chiefvoice-database-url"].secret_id
}

output "artifact_repository_url" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}"
}

output "api_service_account" {
  value = google_service_account.api.email
}

output "migration_service_account" {
  value = google_service_account.migrations.email
}

output "post_call_function_service_account" {
  value = google_service_account.post_call.email
}

output "object_storage_bucket" {
  value = google_storage_bucket.objects.name
}

output "object_storage_endpoint" {
  value = "https://storage.googleapis.com"
}

output "object_storage_signing_region" {
  value = "auto"
}

output "cloud_build_substitutions" {
  value = {
    _OBJECT_STORAGE_BUCKET              = google_storage_bucket.objects.name
    _CLOUD_SQL_INSTANCE                 = google_sql_database_instance.postgres.connection_name
    _RUNTIME_SERVICE_ACCOUNT            = google_service_account.api.email
    _MIGRATION_SERVICE_ACCOUNT          = google_service_account.migrations.email
    _POST_CALL_FUNCTION_SERVICE_ACCOUNT = google_service_account.post_call.email
    _POST_CALL_TRANSCRIPTION_PROJECT_ID = var.project_id
    _POST_CALL_TRANSCRIPTION_LOCATION   = var.post_call_transcription_location
    _POST_CALL_TRANSCRIPTION_MODEL      = var.post_call_transcription_model
    _DATABASE_URL_SECRET                = google_secret_manager_secret.runtime["chiefvoice-database-url"].secret_id
    _CREDENTIALS_ENCRYPTION_KEY_SECRET  = google_secret_manager_secret.runtime["chiefvoice-credentials-encryption-key"].secret_id
    _POST_CALL_ENCRYPTION_KEY_SECRET    = google_secret_manager_secret.runtime["chiefvoice-post-call-encryption-key"].secret_id
    _VOBIZ_WEBHOOK_SECRET               = google_secret_manager_secret.runtime["chiefvoice-vobiz-webhook-secret"].secret_id
    _VOBIZ_ROUTE_TOKEN_SECRET           = google_secret_manager_secret.runtime["chiefvoice-vobiz-route-token"].secret_id
    _GCP_PROJECT_PARENT                 = var.tenant_project_parent
    _GCP_BILLING_ACCOUNT_ID             = trimprefix(var.tenant_billing_account_id, "billingAccounts/")
    _GCP_BUDGET_API_PROJECT_ID          = var.budget_api_project_id
    _GCP_VERTEX_MONTHLY_SPEND_CAP       = var.vertex_monthly_spend_cap
  }
}

output "secret_ids" {
  value = { for key, secret in google_secret_manager_secret.runtime : key => secret.secret_id }
}
