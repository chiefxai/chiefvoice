resource "google_project_service" "apis" {
  for_each = toset(["compute.googleapis.com", "storage.googleapis.com", "cloudtasks.googleapis.com", "cloudfunctions.googleapis.com", "run.googleapis.com", "iam.googleapis.com", "vpcaccess.googleapis.com"])
  project = var.project_id
  service = each.value
  disable_on_destroy = false
}
resource "random_password" "database" { length = 40
  special = false
}
resource "google_compute_network" "main" {
  name = "chiefvoice-vpc"
  auto_create_subnetworks = true
  depends_on = [google_project_service.apis]
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
resource "google_service_account" "backend" {
  account_id = "chiefvoice-backend"
  display_name = "Chiefvoice backend VM"
}
resource "google_project_iam_member" "backend_tasks" {
  project = var.project_id
  role = "roles/cloudtasks.enqueuer"
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
resource "google_service_account" "task_invoker" {
  account_id = "chiefvoice-task-invoker"
  display_name = "Cloud Tasks OIDC caller for post-call function"
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
resource "google_project_iam_member" "task_invoker_function_access" {
  project = var.project_id
  role = "roles/run.invoker"
  member = "serviceAccount:${google_service_account.task_invoker.email}"
}
resource "google_cloud_tasks_queue_iam_member" "function_invoker" {
  name = google_cloud_tasks_queue.post_call.id
  role = "roles/cloudtasks.enqueuer"
  member = "serviceAccount:${google_service_account.backend.email}"
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
    access_config {}
  }
  service_account {
    email = google_service_account.backend.email
    scopes = ["cloud-platform"]
  }
  metadata_startup_script = templatefile("${path.module}/startup.sh.tftpl", { api_image_uri = var.api_image_uri, database_password = random_password.database.result, object_bucket = google_storage_bucket.objects.name, object_access_id = google_storage_hmac_key.backend.access_id, object_secret = google_storage_hmac_key.backend.secret, tasks_queue = google_cloud_tasks_queue.post_call.id, function_url = var.post_call_function_url, function_service_account = google_service_account.task_invoker.email })
  labels = var.labels
  depends_on = [google_project_iam_member.backend_tasks, google_storage_bucket_iam_member.backend_objects]
}
