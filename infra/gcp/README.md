# GCP VM deployment (primary)

This root runs the API and PostgreSQL together on one Compute Engine VM. Post-call jobs use Cloud Tasks targeting the separately deployed Cloud Function. Recording and backup objects use Cloud Storage through its S3-compatible endpoint (`https://storage.googleapis.com`) with an HMAC key and signing region `auto`.

The VM has an encrypted persistent boot disk and a public IP for the API. The Terraform root also creates a Serverless VPC Access connector and a firewall rule for Cloud Function traffic to PostgreSQL on the VM. Cloud Build must deploy the function with that connector and the VM's internal IP as its database host. The initial root does not yet create automated PostgreSQL backups or HTTPS termination; set those up before production.

Set `POST_CALL_EXECUTION_BACKEND=gcp_cloud_tasks` and configure the queue path, function URL, and task-caller service account. Cloud Tasks retries failed HTTP requests. Ensure the deployed function grants `roles/run.invoker` only to the task-caller service account. Terraform state includes the database password, bootstrap script and S3-compatible HMAC secret; use protected remote state.

```sh
terraform init -backend-config=backend.hcl
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

`../gcp-managed` retains the earlier Cloud Run plus Cloud SQL layout as a secondary reference.
