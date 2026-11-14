# Terraform infrastructure

The primary Terraform roots use one VM for the API and PostgreSQL, with provider-native async compute, queues, and object storage. Keep separate remote state for each cloud:

- `infra/aws` provisions EC2 + local PostgreSQL, Lambda + SQS, and S3.
- `infra/gcp` provisions Compute Engine + local PostgreSQL, a Cloud Function target + Cloud Tasks, and Cloud Storage via its S3-compatible HMAC endpoint.
- `infra/scalable-infra/aws` and `infra/scalable-infra/gcp` retain the earlier ECS/Cloud Run + managed PostgreSQL designs for future scaling options.

Create the state backend before initializing either root: a private, versioned GCS bucket for `infra/gcp`, and a private, versioned S3 bucket plus DynamoDB lock table for `infra/aws`. The roots declare the matching remote backend. Use separate state paths, restrict state readers, and never commit state, plan files, or real `terraform.tfvars` files. Both roots generate database and encryption secrets, and provider state may include HMAC and secret versions; encrypt and tightly restrict state access.

Example workflow:

```sh
cd infra/gcp # or infra/aws
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl
terraform init -backend-config=backend.hcl
terraform fmt -recursive
terraform validate
terraform plan
terraform apply
```

The primary GCP VM template creates a Serverless VPC Access connector for private worker-to-database access. The Cloud Build pipeline still contains steps for the secondary Cloud Run/Cloud SQL deployment; update the release trigger and function deployment settings before using it with the VM root.

The AWS VM template creates its VPC, a public backend subnet, a private Lambda subnet, and a NAT gateway for Lambda egress to Vertex. NAT gateways incur ongoing charges. PostgreSQL is on the VM's EBS volume; take and test backups before production.

The AWS deployment uses GCP Vertex project isolation. Supply GCP project/billing configuration and a Google credential secret ARN, or configure workload identity federation and adapt the credential loading path. Give that GCP identity the project creation, billing, API enablement, IAM, and budget permissions described in the main README. AWS Terraform does not grant IAM in your GCP organization or billing account.

Retention and backup policy lists default to empty, which disables those app workers. Set them deliberately after tenant IDs and data requirements are known. Object versioning and automatic expiration are disabled so app-managed deletion can remove the stored object and respect retention/legal-hold decisions. Database backup/PITR is enabled separately. Do not attach bucket expiration rules that can delete legal-hold or pending recovery data.

Terraform validation and cloud deployment require provider credentials and are not performed automatically by these templates. Review the plan, costs, IAM scope, database sizing, network egress, and deletion protection before applying to a real account.
