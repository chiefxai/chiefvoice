# Infrastructure

The active GCP Terraform stack is maintained in the separate [chiefvoice-infra repository](https://github.com/chiefxai/chiefvoice-infra). Its `gcp/` root provisions the production VM and PostgreSQL, Cloud Tasks, post-call Cloud Function, Cloud Storage, secrets, and IAM. Keep its Terraform state in the existing private GCS state bucket; never commit state, plan files, backend config, or production `terraform.tfvars`.

The infrastructure retained in this application repository is:

- `infra/aws`: EC2 + local PostgreSQL, Lambda + SQS, and S3.
- `infra/scalable-infra/aws` and `infra/scalable-infra/gcp`: earlier ECS/Cloud Run + managed PostgreSQL designs for future scaling options.

The AWS VM template creates a VPC, public backend subnet, private Lambda subnet, and NAT gateway for Lambda egress to Vertex. NAT gateways incur ongoing charges. PostgreSQL is on the VM's EBS volume; take and test backups before production.

The AWS deployment uses GCP Vertex project isolation. Supply GCP project/billing configuration and a Google credential secret ARN, or configure workload identity federation and adapt the credential loading path. Give that GCP identity the project creation, billing, API enablement, IAM, and budget permissions described in the main README. AWS Terraform does not grant IAM in your GCP organization or billing account.

Retention and backup policy lists default to empty, which disables those app workers. Set them deliberately after tenant IDs and data requirements are known. Object versioning and automatic expiration are disabled so app-managed deletion can remove the stored object and respect retention/legal-hold decisions. Database backup/PITR is enabled separately. Do not attach bucket expiration rules that can delete legal-hold or pending recovery data.

Review Terraform plans, costs, IAM scope, database sizing, network egress, and deletion protection before applying to a real account.
