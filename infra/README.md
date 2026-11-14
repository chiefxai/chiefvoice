# Terraform infrastructure

The Terraform roots are provider-specific and intentionally use separate state:

- `infra/gcp` provisions the GCP project foundation used by the existing Cloud Build release pipeline: Artifact Registry, PostgreSQL, private object storage, secrets, runtime identities, and IAM.
- `infra/aws` provisions an AWS-hosted API and Lambda stack: ECS/Fargate behind an HTTPS ALB, private RDS PostgreSQL, S3-compatible object storage, Secrets Manager, and the post-call Lambda.

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

The GCP root owns base infrastructure only. `cloudbuild.yaml` owns GCP container releases, database migration execution, and the optional Gen 2 post-call function. Export Terraform's `cloud_build_substitutions` output into Cloud Build trigger substitutions. Do not use another tool to manage those same Cloud Run services.

The AWS root expects an existing VPC with at least two public and two private subnets across availability zones. Private subnets need NAT or VPC endpoints for ECR, CloudWatch Logs, Secrets Manager, and external Google Vertex AI. The Lambda concurrency cap defaults to 10 to limit database connection pressure. Ensure the target group and ACM certificate are in the ALB region.

The AWS deployment uses GCP Vertex project isolation. Supply GCP project/billing configuration and a Google credential secret ARN, or configure workload identity federation and adapt the credential loading path. Give that GCP identity the project creation, billing, API enablement, IAM, and budget permissions described in the main README. AWS Terraform does not grant IAM in your GCP organization or billing account.

Retention and backup policy lists default to empty, which disables those app workers. Set them deliberately after tenant IDs and data requirements are known. Object versioning and automatic expiration are disabled so app-managed deletion can remove the stored object and respect retention/legal-hold decisions. Database backup/PITR is enabled separately. Do not attach bucket expiration rules that can delete legal-hold or pending recovery data.

Terraform validation and cloud deployment require provider credentials and are not performed automatically by these templates. Review the plan, costs, IAM scope, database sizing, network egress, and deletion protection before applying to a real account.
