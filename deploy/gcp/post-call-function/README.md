# Google Cloud Functions post-call worker

This is an authenticated HTTP Cloud Run function (Cloud Functions 2nd gen). The API's Cloud Build pipeline prepares the source, deploys it when `_POST_CALL_EXECUTION_BACKEND=gcp_function`, and grants the API runtime service account `roles/run.invoker`. The function is not publicly invokable.

The handler accepts only `{ "schema_version": 1, "job_id": "..." }` and reuses the shared post-call processor. PostgreSQL remains the durable source of truth; the API dispatcher retries dispatch failures and redispatches jobs that were not claimed. The function itself claims the job with the same Postgres lease, so duplicate requests do not repeat completed work.

Use a dedicated function service account. Give it `roles/cloudsql.client`, Secret Manager access to the database URL and post-call encryption key, and Vertex AI access on each tenant project. Set `_POST_CALL_FUNCTION_SERVICE_ACCOUNT` in the Cloud Build trigger; the API deploy passes that identity as `GCP_POST_CALL_SERVICE_ACCOUNT`, so new tenant projects receive `roles/aiplatform.user` for the post-call runtime. The function service account does not need project creation, billing, or project IAM administration.

The API runtime service account uses Application Default Credentials to obtain an ID token whose audience is the function URL. Grant it `roles/run.invoker` on the function. The function has a 240-second request timeout, while model analysis has a shorter Postgres lease-based timeout; adjust those together if model execution limits change.

The source staging helper copies the worker package into a temporary build directory. `requirements.txt` is intentionally limited to dependencies needed by the asynchronous post-call handler and does not install Pipecat or telephony packages.
