locals {
  app_name = "chiefvoice"
  enabled_apis = toset([
    "aiplatform.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com", "cloudtasks.googleapis.com", "compute.googleapis.com",
    "cloudfunctions.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com",
    "run.googleapis.com", "secretmanager.googleapis.com", "serviceusage.googleapis.com",
    "storage.googleapis.com", "vpcaccess.googleapis.com",
  ])
}

resource "google_project_service" "apis" {
  for_each = local.enabled_apis
  project = var.project_id
  service = each.value
  disable_on_destroy = false
}
resource "random_password" "database" { length = 40
  special = false
}
resource "random_bytes" "app_keys" {
  for_each = toset(["credentials", "post-call", "recordings", "backup"])
  length = 32
}
resource "random_password" "vobiz_secrets" {
  for_each = toset(["webhook", "route-token"])
  length = 48
  special = false
}
locals {
  vm_secret_names = toset(["database", "credentials", "post-call", "recordings", "backup", "vobiz-webhook", "vobiz-route", "object-access-id", "object-secret"])
  vm_secret_values = {
    database = random_password.database.result
    credentials = random_bytes.app_keys["credentials"].base64
    "post-call" = random_bytes.app_keys["post-call"].base64
    recordings = random_bytes.app_keys["recordings"].base64
    backup = random_bytes.app_keys["backup"].base64
    "vobiz-webhook" = random_password.vobiz_secrets["webhook"].result
    "vobiz-route" = random_password.vobiz_secrets["route-token"].result
    "object-access-id" = google_storage_hmac_key.backend.access_id
    "object-secret" = google_storage_hmac_key.backend.secret
  }
}

resource "google_compute_network" "main" {
  name = "chiefvoice-vpc"
  auto_create_subnetworks = true
  depends_on = [google_project_service.apis]
}
resource "google_compute_address" "backend" {
  name = "chiefvoice-backend-ip"
  region = var.region
}
resource "google_compute_firewall" "backend" {
  name = "chiefvoice-api-ingress"
  network = google_compute_network.main.name
  allow {
    protocol = "tcp"
    ports = ["80", "443"]
  }
  source_ranges = ["0.0.0.0/0"]
  target_tags = ["chiefvoice-backend"]
}
resource "google_compute_firewall" "iap_ssh" {
  name = "chiefvoice-iap-ssh"
  network = google_compute_network.main.name
  allow {
    protocol = "tcp"
    ports = ["22"]
  }
  source_ranges = ["35.235.240.0/20"]
  target_tags = ["chiefvoice-backend"]
}
resource "google_compute_firewall" "postgres_from_tasks" {
  name = "chiefvoice-postgres-from-serverless"
  network = google_compute_network.main.name
  allow {
    protocol = "tcp"
    ports = ["5432"]
  }
  source_ranges = ["10.8.0.0/28"]
  target_tags = ["chiefvoice-backend"]
}
resource "google_vpc_access_connector" "functions" {
  name = "chiefvoice-functions"
  region = var.region
  network = google_compute_network.main.name
  ip_cidr_range = "10.8.0.0/28"
  machine_type = "e2-micro"
  min_instances = 2
  max_instances = 3
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "objects" {
  name = var.object_storage_bucket_name
  location = var.region
  uniform_bucket_level_access = true
  public_access_prevention = "enforced"
  force_destroy = false
  labels = var.labels
}
resource "google_artifact_registry_repository" "images" {
  location = var.region
  repository_id = "chiefvoice"
  description = "Chiefvoice API container images"
  format = "DOCKER"
  labels = var.labels
  depends_on = [google_project_service.apis]
}
resource "google_service_account" "backend" {
  account_id = "chiefvoice-backend"
  display_name = "Chiefvoice backend VM and tenant project provisioner"
}
resource "google_service_account" "post_call_function" {
  account_id = "chiefvoice-post-call"
  display_name = "Chiefvoice post-call function runtime"
}
resource "google_service_account" "task_invoker" {
  account_id = "chiefvoice-task-invoker"
  display_name = "Cloud Tasks OIDC caller for post-call function"
}
resource "google_project_iam_member" "backend_tasks" {
  project = var.project_id
  role = "roles/cloudtasks.enqueuer"
  member = "serviceAccount:${google_service_account.backend.email}"
}
resource "google_artifact_registry_repository_iam_member" "backend_ar_reader" {
  location = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role = "roles/artifactregistry.reader"
  member = "serviceAccount:${google_service_account.backend.email}"
}
resource "google_storage_bucket_iam_member" "backend_objects" {
  bucket = google_storage_bucket.objects.name
  role = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.backend.email}"
}
resource "google_storage_hmac_key" "backend" {
  project = var.project_id
  service_account_email = google_service_account.backend.email
  depends_on = [google_project_service.apis]
}
resource "google_cloud_tasks_queue" "post_call" {
  name = "chiefvoice-post-call"
  location = var.region
  rate_limits {
    max_dispatches_per_second = 10
    max_concurrent_dispatches = 10
  }
  retry_config {
    max_attempts = 5
    min_backoff = "10s"
    max_backoff = "300s"
    max_doublings = 5
  }
  depends_on = [google_project_service.apis]
}
resource "google_project_service_identity" "cloud_tasks" {
  project = var.project_id
  service = "cloudtasks.googleapis.com"
  depends_on = [google_project_service.apis]
}
resource "google_service_account_iam_member" "backend_can_use_invoker" {
  service_account_id = google_service_account.task_invoker.name
  role = "roles/iam.serviceAccountUser"
  member = "serviceAccount:${google_service_account.backend.email}"
}
resource "google_service_account_iam_member" "tasks_agent_can_mint_token" {
  service_account_id = google_service_account.task_invoker.name
  role = "roles/iam.serviceAccountTokenCreator"
  member = "serviceAccount:${google_project_service_identity.cloud_tasks.email}"
}

resource "google_project_iam_member" "backend_vertex_user" {
  project = var.project_id
  role = "roles/aiplatform.user"
  member = "serviceAccount:${google_service_account.backend.email}"
  depends_on = [google_project_service.apis]
}
resource "google_project_iam_member" "function_vertex_user" {
  project = var.project_id
  role = "roles/aiplatform.user"
  member = "serviceAccount:${google_service_account.post_call_function.email}"
  depends_on = [google_project_service.apis]
}
resource "google_project_iam_member" "backend_log_writer" {
  project = var.project_id
  role = "roles/logging.logWriter"
  member = "serviceAccount:${google_service_account.backend.email}"
}
resource "google_project_iam_member" "function_log_writer" {
  project = var.project_id
  role = "roles/logging.logWriter"
  member = "serviceAccount:${google_service_account.post_call_function.email}"
}

# Store function-only secrets centrally. VM secrets are injected into a root-only
# env file by startup metadata; Terraform state must therefore remain protected.
resource "google_secret_manager_secret" "post_call_key" {
  secret_id = "chiefvoice-post-call-encryption-key"
  replication { auto {} }
  depends_on = [google_project_service.apis]
}
resource "google_secret_manager_secret_version" "post_call_key" {
  secret = google_secret_manager_secret.post_call_key.id
  secret_data = random_bytes.app_keys["post-call"].base64
}
resource "google_secret_manager_secret" "function_database_url" {
  secret_id = "chiefvoice-function-database-url"
  replication { auto {} }
  depends_on = [google_project_service.apis]
}
resource "google_secret_manager_secret_version" "function_database_url" {
  secret = google_secret_manager_secret.function_database_url.id
  secret_data = "postgresql+asyncpg://chiefvoice:${urlencode(random_password.database.result)}@${google_compute_instance.backend.network_interface[0].network_ip}:5432/chiefvoice"
}
resource "google_secret_manager_secret" "vm_runtime" {
  for_each = local.vm_secret_names
  secret_id = "chiefvoice-${each.key}"
  replication { auto {} }
  depends_on = [google_project_service.apis]
}
resource "google_secret_manager_secret_version" "vm_runtime" {
  for_each = local.vm_secret_names
  secret = google_secret_manager_secret.vm_runtime[each.key].id
  secret_data = local.vm_secret_values[each.key]
}
resource "google_secret_manager_secret_iam_member" "vm_runtime_access" {
  for_each = local.vm_secret_names
  secret_id = google_secret_manager_secret.vm_runtime[each.key].id
  role = "roles/secretmanager.secretAccessor"
  member = "serviceAccount:${google_service_account.backend.email}"
}
resource "google_secret_manager_secret_iam_member" "function_key_access" {
  secret_id = google_secret_manager_secret.post_call_key.id
  role = "roles/secretmanager.secretAccessor"
  member = "serviceAccount:${google_service_account.post_call_function.email}"
}
resource "google_secret_manager_secret_iam_member" "function_db_access" {
  secret_id = google_secret_manager_secret.function_database_url.id
  role = "roles/secretmanager.secretAccessor"
  member = "serviceAccount:${google_service_account.post_call_function.email}"
}

resource "google_service_account_iam_member" "cloud_build_function_act_as" {
  service_account_id = google_service_account.post_call_function.name
  role = "roles/iam.serviceAccountUser"
  member = "serviceAccount:${var.cloud_build_service_account_email}"
}
resource "google_project_iam_member" "cloud_build_function_developer" {
  project = var.project_id
  role = "roles/cloudfunctions.developer"
  member = "serviceAccount:${var.cloud_build_service_account_email}"
}
resource "google_project_iam_member" "cloud_build_run_admin" {
  project = var.project_id
  role = "roles/run.admin"
  member = "serviceAccount:${var.cloud_build_service_account_email}"
}
resource "google_artifact_registry_repository_iam_member" "cloud_build_ar_writer" {
  location = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role = "roles/artifactregistry.writer"
  member = "serviceAccount:${var.cloud_build_service_account_email}"
}
resource "google_project_iam_member" "cloud_build_compute_admin" {
  project = var.project_id
  role = "roles/compute.instanceAdmin.v1"
  member = "serviceAccount:${var.cloud_build_service_account_email}"
}
resource "google_project_iam_member" "cloud_build_iap_tunnel" {
  project = var.project_id
  role = "roles/iap.tunnelResourceAccessor"
  member = "serviceAccount:${var.cloud_build_service_account_email}"
}
resource "google_project_iam_member" "cloud_build_os_login" {
  project = var.project_id
  role = "roles/compute.osAdminLogin"
  member = "serviceAccount:${var.cloud_build_service_account_email}"
}
resource "google_project_iam_member" "cloud_build_service_usage" {
  project = var.project_id
  role = "roles/serviceusage.serviceUsageAdmin"
  member = "serviceAccount:${var.cloud_build_service_account_email}"
}

resource "google_compute_instance" "backend" {
  name = "chiefvoice-backend"
  machine_type = var.vm_machine_type
  zone = var.zone
  tags = ["chiefvoice-backend"]
  boot_disk {
    initialize_params {
      image = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
      size = var.vm_disk_gb
      type = "pd-ssd"
    }
  }
  network_interface {
    network = google_compute_network.main.id
    access_config { nat_ip = google_compute_address.backend.address }
  }
  service_account {
    email = google_service_account.backend.email
    scopes = ["cloud-platform"]
  }
  metadata_startup_script = templatefile("${path.module}/startup.sh.tftpl", {
    object_bucket = google_storage_bucket.objects.name
    secret_ids = { for key, secret in google_secret_manager_secret.vm_runtime : key => secret.secret_id }
    tasks_queue = google_cloud_tasks_queue.post_call.id
    function_service_account = google_service_account.task_invoker.email
    public_domain = var.public_domain
    vertex_project_id = var.project_id
    vertex_location = var.vertex_location
    vertex_model = var.vertex_model
    post_call_model = var.post_call_model
    oidc_issuer = var.oidc_issuer
    oidc_audience = var.oidc_audience
    oidc_jwks_url = var.oidc_jwks_url
    platform_admin_subjects = join(",", var.platform_admin_subjects)
    artifact_registry_host = "${var.region}-docker.pkg.dev"
  })
  labels = var.labels
  depends_on = [
    google_project_iam_member.backend_tasks,
    google_storage_bucket_iam_member.backend_objects,
    google_secret_manager_secret_iam_member.vm_runtime_access,
    google_secret_manager_secret_version.vm_runtime,
  ]
}
