# Infrastructure

Terraform roots are owned by the separate [chiefvoice-infra repository](https://github.com/chiefxai/chiefvoice-infra). The `gcp/` root is the active production stack. The `aws/` root describes an independent EC2, PostgreSQL, SQS, and Lambda deployment option; it is not currently deployed and does not migrate GCP data. Do not apply it until an AWS migration and cutover plan is approved.

The authoritative AWS root, its variables, and deployment instructions are in [`chiefvoice-infra/aws`](https://github.com/chiefxai/chiefvoice-infra/tree/main/aws). That repository's CI validates the AWS root on pull requests and pushes to its `main` branch. To validate locally without production credentials or a backend:

```sh
git clone https://github.com/chiefxai/chiefvoice-infra.git
cd chiefvoice-infra/aws
terraform fmt -check -recursive
terraform init -backend=false -input=false
terraform validate
```

For an approved AWS deployment, follow the prerequisites and manual deployment steps in the [AWS root README](https://github.com/chiefxai/chiefvoice-infra/blob/main/aws/README.md). It documents the AWS account inputs, GCP service-account secret ARN and permissions, backend setup, required Terraform variables, and `init`/`plan`/`apply` commands. AWS uses a separate private S3 state backend; do not reuse the GCP GCS state. Terraform state, backend configuration, plans, and production `terraform.tfvars` may contain sensitive values and must not be committed.

The AWS variables example is a starting point, not a complete production configuration. Complete the required values listed in the destination [`aws/variables.tf`](https://github.com/chiefxai/chiefvoice-infra/blob/main/aws/variables.tf), including the AMI, SSH CIDR, API and Lambda image URIs, public domain, OIDC settings, GCP project/billing/service-account settings, Google credential secret ARN, and object bucket name. The backend example also requires a separately provisioned S3 state bucket and DynamoDB lock table. The destination README explains how to prepare these files and the AWS/GCP credentials. AWS has no automated deployment workflow; deployment is manual and must follow an approved migration plan.

The earlier `infra/scalable-infra/aws` and `infra/scalable-infra/gcp` designs remain in this repository for reference; they are not the active GCP root or the AWS deployment option.

Review Terraform plans, costs, IAM scope, database sizing, network egress, and deletion protection before applying to any real account.
