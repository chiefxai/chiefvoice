locals {
  required_apis = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudfunctions.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudsql.googleapis.com",
    "iam.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "aiplatform.googleapis.com",
    "speech.googleapis.com",
    "storage.googleapis.com",
  ])
  secret_ids = toset([
    "chiefvoice-database-url",
    "chiefvoice-credentials-encryption-key",
    "chiefvoice-post-call-encryption-key",
    "chiefvoice-recordings-encryption-key",
    "chiefvoice-backup-encryption-key",
    "chiefvoice-vobiz-webhook-secret",
    "chiefvoice-vobiz-route-token",
    "chiefvoice-object-storage-access-key-id",
    "chiefvoice-object-storage-secret-access-key",
  ])
}

resource "google_project_service" "required" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "app" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repository
  description   = "Chiefvoice container images"
  format        = "DOCKER"
  labels        = var.labels
  depends_on    = [google_project_service.required]
}

resource "google_sql_database_instance" "postgres" {
  project             = var.project_id
  name                = "chiefvoice-postgres"
  region              = var.database_region
  database_version    = "POSTGRES_16"
  deletion_protection = true
  settings {
    tier              = var.database_tier
    availability_type = "REGIONAL"
    disk_type         = "PD_SSD"
    disk_size         = var.database_disk_gb
    disk_autoresize   = true
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 14
        retention_unit   = "COUNT"
      }
    }
    ip_configuration {
      ipv4_enabled                                  = true
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }
    insights_config {
      query_insights_enabled = true
    }
    user_labels = var.labels
  }
  depends_on = [google_project_service.required]
}

resource "random_password" "database" {
  length  = 40
  special = false
}

resource "google_sql_database" "app" {
  project  = var.project_id
  name     = var.database_name
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "app" {
  project  = var.project_id
  name     = var.database_user
  instance = google_sql_database_instance.postgres.name
  password = random_password.database.result
}

resource "google_storage_bucket" "objects" {
  project                     = var.project_id
  name                        = var.object_storage_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels = var.labels
}

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "chiefvoice-api"
  display_name = "Chiefvoice API runtime"
}

resource "google_service_account" "migrations" {
  project      = var.project_id
  account_id   = "chiefvoice-migrations"
  display_name = "Chiefvoice schema migration job"
}

resource "google_service_account" "post_call" {
  project      = var.project_id
  account_id   = var.post_call_function_service_account
  display_name = "Chiefvoice post-call function runtime"
}

resource "google_folder_iam_member" "tenant_project_creator" {
  count  = startswith(var.tenant_project_parent, "folders/") ? 1 : 0
  folder = trimprefix(var.tenant_project_parent, "folders/")
  role   = "roles/resourcemanager.projectCreator"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_organization_iam_member" "tenant_project_creator" {
  count  = startswith(var.tenant_project_parent, "organizations/") ? 1 : 0
  org_id = trimprefix(var.tenant_project_parent, "organizations/")
  role   = "roles/resourcemanager.projectCreator"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_billing_account_iam_member" "project_billing_user" {
  billing_account_id = trimprefix(var.tenant_billing_account_id, "billingAccounts/")
  role               = "roles/billing.user"
  member             = "serviceAccount:${google_service_account.api.email}"
}

resource "google_billing_account_iam_member" "budget_manager" {
  billing_account_id = trimprefix(var.tenant_billing_account_id, "billingAccounts/")
  role               = "roles/billing.costsManager"
  member             = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_service" "budget_api" {
  project            = var.budget_api_project_id
  service            = "billingbudgets.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_iam_member" "budget_api_consumer" {
  project = var.budget_api_project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "cloudsql" {
  for_each = {
    api        = google_service_account.api.email
    migrations = google_service_account.migrations.email
    post_call  = google_service_account.post_call.email
  }
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${each.value}"
}

resource "google_project_iam_member" "vertex" {
  for_each = {
    api       = google_service_account.api.email
    post_call = google_service_account.post_call.email
  }
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${each.value}"
}

resource "google_project_iam_member" "post_call_speech_client" {
  project    = var.project_id
  role       = "roles/speech.client"
  member     = "serviceAccount:${google_service_account.post_call.email}"
  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "runtime" {
  bucket = google_storage_bucket.objects.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_storage_hmac_key" "api" {
  project               = var.project_id
  service_account_email = google_service_account.api.email
  depends_on            = [google_project_service.required]
}

resource "google_secret_manager_secret" "runtime" {
  for_each  = local.secret_ids
  project   = var.project_id
  secret_id = each.value
  replication {
    auto {}
  }
  labels     = var.labels
  depends_on = [google_project_service.required]
}

resource "random_bytes" "fernet" {
  for_each    = toset(["credentials", "post-call", "recordings", "backup"])
  length      = 32
  keepers     = { purpose = each.key }
}

resource "random_password" "vobiz_secrets" {
  for_each = toset(["webhook", "route-token"])
  length   = 48
  special  = false
}

resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.runtime["chiefvoice-database-url"].id
  secret_data = "postgresql+asyncpg://${var.database_user}:${urlencode(random_password.database.result)}@/${var.database_name}?host=/cloudsql/${google_sql_database_instance.postgres.connection_name}"
  depends_on  = [google_sql_user.app, google_sql_database.app]
}

resource "google_secret_manager_secret_version" "fernet" {
  for_each    = random_bytes.fernet
  secret      = google_secret_manager_secret.runtime["chiefvoice-${each.key}-encryption-key"].id
  secret_data = each.value.base64
}

resource "google_secret_manager_secret_version" "vobiz" {
  for_each    = random_password.vobiz_secrets
  secret      = google_secret_manager_secret.runtime["chiefvoice-vobiz-${each.key == "webhook" ? "webhook-secret" : "route-token"}"].id
  secret_data = each.value.result
}

resource "google_secret_manager_secret_version" "storage_access_key" {
  secret      = google_secret_manager_secret.runtime["chiefvoice-object-storage-access-key-id"].id
  secret_data = google_storage_hmac_key.api.access_id
}

resource "google_secret_manager_secret_version" "storage_secret_key" {
  secret      = google_secret_manager_secret.runtime["chiefvoice-object-storage-secret-access-key"].id
  secret_data = google_storage_hmac_key.api.secret
}

resource "google_secret_manager_secret_iam_member" "api" {
  for_each  = google_secret_manager_secret.runtime
  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "migration_database" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.runtime["chiefvoice-database-url"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrations.email}"
}

resource "google_secret_manager_secret_iam_member" "function_secrets" {
  for_each  = toset(["chiefvoice-database-url", "chiefvoice-post-call-encryption-key"])
  project   = var.project_id
  secret_id = google_secret_manager_secret.runtime[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.post_call.email}"
}

resource "google_logging_metric" "post_call_function_failures" {
  project     = var.project_id
  name        = "chiefvoice-post-call-function-failures"
  description = "Post-call Cloud Function failures emitted as structured JSON logs"
  filter      = "resource.type=\"cloud_run_revision\" AND jsonPayload.event=\"post_call_function_failed\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "post_call_function_failures" {
  project      = var.project_id
  display_name = "Chiefvoice post-call function errors"
  combiner     = "OR"
  enabled      = true
  notification_channels = var.monitoring_notification_channels
  conditions {
    display_name = "Post-call function failure count"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/chiefvoice-post-call-function-failures\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  alert_strategy { auto_close = "604800s" }
}
