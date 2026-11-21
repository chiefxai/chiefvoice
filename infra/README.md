# Infrastructure

Terraform roots are maintained in the separate [chiefvoice-infra repository](https://github.com/chiefxai/chiefvoice-infra). Its `gcp/` root provisions active production infrastructure. Its `aws/` root is a future alternative only; it creates a separate environment and does not migrate GCP data. Do not apply AWS until a migration and cutover plan is ready. GCP uses the existing private GCS state bucket; AWS must use a separate private S3 backend. Never commit state, plan files, backend config, or production `terraform.tfvars`.

The earlier `infra/scalable-infra/aws` and `infra/scalable-infra/gcp` designs remain here for reference. The AWS EC2/SQS/Lambda root has moved to `chiefvoice-infra/aws`.

The AWS VM template creates a VPC, public backend subnet, private Lambda subnet, and NAT gateway for Lambda egress to Vertex. NAT gateways incur ongoing charges. PostgreSQL is on the VM's EBS volume; take and test backups before production.

The AWS deployment uses GCP Vertex project isolation. Supply GCP project/billing configuration and a Google credential secret ARN, or configure workload identity federation and adapt the credential loading path. Give that GCP identity the project creation, billing, API enablement, IAM, and budget permissions described in the main README. AWS Terraform does not grant IAM in your GCP organization or billing account.

Retention and backup policy lists default to empty, which disables those app workers. Set them deliberately after tenant IDs and data requirements are known. Object versioning and automatic expiration are disabled so app-managed deletion can remove the stored object and respect retention/legal-hold decisions. Database backup/PITR is enabled separately. Do not attach bucket expiration rules that can delete legal-hold or pending recovery data.

Review Terraform plans, costs, IAM scope, database sizing, network egress, and deletion protection before applying to a real account.
