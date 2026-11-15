# GCP VM deployment (active topology)

This Terraform root is the active GCP deployment: one Compute Engine VM runs the API and PostgreSQL, Cloud Tasks queues post-call work, and an authenticated second-generation Cloud Function processes jobs. PostgreSQL stays on the VM's encrypted persistent disk. Recordings and encrypted database backups use the private Cloud Storage bucket through its S3-compatible endpoint.

## Prerequisites

- Set `public_domain` to a DNS name you control. Point its A record to `backend_external_ip` before the first public deployment so Caddy can obtain/renew its HTTPS certificate. Provider callback URLs are HTTPS.
- Configure OIDC issuer, audience and JWKS URL; `terraform.tfvars.example` shows all required values.
- Set `platform_admin_subjects` to trusted OIDC `sub` values for platform operators. Customer provisioning also requires `platform:customers:write` or `platform:customers:read` in the token scope; an empty allowlist disables customer administration.
- This no-organization deployment uses the hosting project for Vertex AI. Customer records, API keys and call data remain tenant-scoped in PostgreSQL, but Vertex project-level usage and spend caps are shared. The backend and post-call function identities receive Vertex AI access only in this project. Per-customer Vertex project isolation requires a GCP folder/organization or manually provisioned customer projects and a later integration change.
- Terraform creates the `chiefvoice-build` service account. Register the Cloud Build trigger with the `cloud_build_service_account` output. Set `_PUBLIC_DOMAIN`, OIDC values and service account substitutions in `cloudbuild.yaml` to the Terraform outputs. The build identity receives scoped Artifact Registry access and permissions to deploy the function and update the VM over IAP.
- Protect the Terraform remote state: it contains Secret Manager secret versions for the database password, Fernet keys, Vobiz secrets and HMAC object-storage credentials, as well as the rendered startup script. Runtime startup reads values from Secret Manager into root-only files; instance metadata contains secret IDs rather than those values.

## Apply and deploy

```sh
terraform init -backend-config=backend.hcl
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

Terraform reserves a regional static IP; point DNS to the `backend_external_ip` output. VM startup installs Docker, PostgreSQL, Caddy and the Google Cloud CLI, configures Artifact Registry authentication, and starts PostgreSQL. Cloud Build publishes the API image, deploys the post-call function with the Serverless VPC Access connector, binds `roles/run.invoker` only to the Cloud Tasks OIDC service account, obtains the HTTPS function URL, connects to the VM through IAP, waits for PostgreSQL readiness, runs `alembic upgrade head` using the same new image, then starts API and Caddy. The function uses a secret database URL targeting the VM's private address; the connector-only firewall rule allows its PostgreSQL traffic.

Cloud Build builds the API and React console images, deploys the post-call function, applies migrations, and exposes the API and console together on the same HTTPS domain. It also runs unit tests, lint, compilation and public `/healthz`, `/readyz` and `/metrics` probes. Register the trigger after Terraform has created the VM, Artifact Registry repository, connector, queue and service accounts. Later builds repeat migrations under an instance-local deployment lock and only expose the API after migration succeeds.

For Auth0, configure an API audience (for example `https://chiefvoice.elvoryx.in/api`) and a single-page application whose allowed callback and web origin are `https://chiefvoice.elvoryx.in`. Create an Auth0 Action that adds the configured namespaced tenant claim (default `https://chiefvoice.elvoryx.in/tenant_id`) to access tokens. Request the `platform:customers:read` and `platform:customers:write` API permissions. Set the OIDC issuer, audience, JWKS URL, and your Auth0 user `sub` in `terraform.tfvars`; set the frontend authority, client ID, audience, and scope substitutions in `cloudbuild.yaml` before running Cloud Build.

## Backup and restore

The API periodically takes service-scoped PostgreSQL backups with `pg_dump`, encrypts the archive using the dedicated `BACKUP_ENCRYPTION_KEY`, and stores the encrypted object in the private bucket. Policy is one day interval and 30 day retention. The VM service account has object access; the bucket enforces uniform access and public access prevention. Backups require no extra host cron job because the API's backup runner performs the scheduled work.

Check a restore by running `sudo chiefvoice-backup-restore-check` on the VM. It creates a disposable database, verifies and decrypts the newest backup into it, and drops that database on exit. Confirm the `restore_verified` log line and then verify the restored database schema/data before treating a backup as usable. Never restore to the production database. The container image includes PostgreSQL 16 client tools used by the backup and restore provider.

The backend endpoint is `https://<public_domain>`. Cloud Tasks queue retries are bounded by its configured retry policy; monitor Cloud Logging and the queue's oldest task age / retry metrics. `../scalable-infra/gcp` retains the older Cloud Run and Cloud SQL design and is not active.
