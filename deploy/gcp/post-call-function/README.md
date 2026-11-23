# Google Cloud Functions post-call worker

This is an authenticated HTTP Cloud Run function (Cloud Functions 2nd gen). The production Cloud Build pipeline prepares and deploys it, then grants the dedicated Cloud Tasks invoker service account `roles/run.invoker`. The function is not publicly invokable.

The handler accepts only `{ "schema_version": 1, "job_id": "..." }` and reuses the shared post-call processor. PostgreSQL remains the durable source of truth; the API dispatcher retries dispatch failures and redispatches jobs that were not claimed. The function itself claims the job with the same Postgres lease, so duplicate requests do not repeat completed work.

The function needs access to the same encrypted recordings as the API. Configure `RECORDINGS_ENABLED=true`, `RECORDINGS_STORAGE_BACKEND=gcs`, `OBJECT_STORAGE_PROVIDER=gcs`, and the object-storage bucket. The Google Cloud Storage SDK uses Application Default Credentials from the function's attached service account; do not configure HMAC keys or S3 credentials. Without these settings, unclear or multilingual transcripts cannot use recording-based STT/audio review and are marked `*_recording_unavailable`.

Use the Terraform-managed dedicated function service account. It connects to PostgreSQL on the backend VM through the VPC connector; it does not use Cloud SQL. Give it Secret Manager access to the database URL, post-call key, and recordings key, plus `roles/storage.objectAdmin` on the recordings bucket and Vertex AI access on each tenant project. The infrastructure output sync supplies `_FUNCTION_SERVICE_ACCOUNT` and related Cloud Build settings. The function service account does not need project creation, billing, or project IAM administration.

Cloud Tasks creates an OIDC-authenticated request using the dedicated task-invoker service account. Cloud Build grants that identity `roles/run.invoker` on the function. The VM API identity is allowed to create tasks using that service account. The function has a 240-second request timeout, while model analysis has a shorter Postgres lease-based timeout; adjust those together if model execution limits change.

The source staging helper copies the worker package into a temporary build directory. `requirements.txt` is intentionally limited to dependencies needed by the asynchronous post-call handler and does not install Pipecat or telephony packages.
