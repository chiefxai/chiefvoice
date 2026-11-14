# Deploy Chiefvoice to Google Cloud

The root `Dockerfile` builds the API container. `cloudbuild.yaml` builds and tags it, pushes it to Artifact Registry, runs PostgreSQL migrations as a Cloud Run Job, and deploys the API to Cloud Run. Connect the repository to Cloud Build and create a push trigger for the deployment branch, using `cloudbuild.yaml` as the build configuration.

## One-time setup

Create or choose a Google Cloud project for hosting the API, then enable Cloud Build, Artifact Registry, Cloud Run, Secret Manager, Cloud SQL Admin, Cloud Resource Manager, Cloud Billing, Service Usage, and Cloud Billing Budget APIs. Create an Artifact Registry Docker repository matching `_AR_REPOSITORY` (default `chiefvoice`) in `_REGION` (default `us-central1`).

Create a Cloud SQL for PostgreSQL instance and set `_CLOUD_SQL_INSTANCE` to its connection name (`project:region:instance`). Store a `DATABASE_URL` using `/cloudsql/<connection-name>` as the asyncpg host in `chiefvoice-database-url`. The migration job and API both attach to this instance.

Create a runtime service account and set `_RUNTIME_SERVICE_ACCOUNT` to its email. The pipeline also configures this identity as the project provisioning identity. Give the runtime identity:

- Secret Manager Secret Accessor on each runtime secret below.
- `roles/cloudsql.client` for the PostgreSQL instance.
- The project creation, billing-link, service enablement, project IAM, and billing-budget permissions described in the [tenant Vertex project setup](../../README.md#realtime-agent-providers).
- `roles/aiplatform.user` is granted on each tenant project by the provisioning worker.

Create a separate migration service account and set `_MIGRATION_SERVICE_ACCOUNT` to its email. Give it `roles/cloudsql.client` and Secret Manager Secret Accessor only on the database URL secret. The Cloud Build trigger identity needs Artifact Registry Writer on the hosting project, Cloud Run Admin, and Service Account User on both runtime and migration service accounts. Keep the builder identity separate from both runtime identities.

Create these Secret Manager secrets and grant the runtime service account access:

| Secret ID (default) | Contents |
| --- | --- |
| `chiefvoice-database-url` | Async PostgreSQL URL, such as `postgresql+asyncpg://...` |
| `chiefvoice-credentials-encryption-key` | Fernet key for tenant integration credentials |
| `chiefvoice-post-call-encryption-key` | Fernet key for queued post-call data |
| `chiefvoice-vobiz-webhook-secret` | Vobiz signature secret |
| `chiefvoice-vobiz-route-token` | Random inbound route token |

Configure a Cloud Build trigger substitution for every deployment-specific value in `cloudbuild.yaml`, especially `_RUNTIME_SERVICE_ACCOUNT`, `_MIGRATION_SERVICE_ACCOUNT`, `_CLOUD_SQL_INSTANCE`, `_GCP_PROJECT_PARENT`, `_GCP_BILLING_ACCOUNT_ID`, `_GCP_BUDGET_API_PROJECT_ID`, `_GCP_VERTEX_MONTHLY_SPEND_CAP`, `_OIDC_ISSUER`, `_OIDC_AUDIENCE`, and `_OIDC_JWKS_URL`. Change the default placeholder values before creating the trigger. The API is publicly reachable so Vobiz can send call webhooks; application routes still enforce OIDC/API-key auth or provider signature/route-token validation.

## Runtime behavior

The deployment uses Cloud Run with one minimum instance and CPU allocated between requests so its durable PostgreSQL post-call worker can drain jobs. This incurs baseline Cloud Run costs. The request timeout is set to one hour for long calls. Tune CPU, memory, concurrency, and maximum instances for expected simultaneous calls and workload. The same image runs database migrations before each service rollout; keep a database backup and review migrations before production releases.

Project provisioning uses Cloud Run's Application Default Credentials. Do not add a service-account JSON key to the image or build variables. `GCP_VERTEX_MONTHLY_SPEND_CAP` configures a Vertex-only monthly spend cap for each new tenant project; see [Google's spend cap limitations](https://cloud.google.com/billing/docs/how-to/budgets-spend-caps). The cap covers Vertex AI usage in tenant projects. Cloud Run, Cloud SQL, Artifact Registry, and networking costs accrue to the hosting project and need separate budget controls.

After deployment, verify `/healthz`, `/readyz`, and `/metrics`, then create an account and confirm its `vertex_project.status` changes from `pending` to `ready`. Configure the Vobiz callbacks to use the Cloud Run service URL. Cloud Run revisions and build logs are available in Cloud Logging and Cloud Build.
