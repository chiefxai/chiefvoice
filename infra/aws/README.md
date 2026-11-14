# AWS VM deployment (primary)

This root runs the API and PostgreSQL together on one EC2 VM. Post-call jobs are queued in SQS and processed by an image-based Lambda. Recordings and backups use a private S3 bucket. PostgreSQL data lives on the VM's encrypted EBS root volume; there is no RDS instance in this design.

Provide an Ubuntu 24.04 AMI ID, prebuilt API and Lambda images, a globally unique bucket name, and an admin-only SSH CIDR. The VM security group exposes HTTP/HTTPS and SSH only from that CIDR. A NAT gateway gives the private Lambda subnet access to Vertex and external APIs; it has ongoing hourly and data charges. PostgreSQL port 5432 is allowed only from the Lambda security group.

The VM bootstrap starts PostgreSQL and the API as Docker Compose services. The database volume persists on EBS, but this starter root does not yet configure automated PostgreSQL backups or TLS termination. Add a backup schedule and HTTPS reverse proxy/certificate before production use. Terraform state includes generated database credentials and bootstrap content, so use encrypted, access-restricted remote state.

Set `POST_CALL_EXECUTION_BACKEND=sqs` and `POST_CALL_SQS_QUEUE_URL` in the API environment. Lambda receives one SQS message at a time; failed records are retried and eventually sent to the dead-letter queue. The Lambda container must parse SQS `Records` events and have access to the same database and post-call encryption key.

```sh
terraform init -backend-config=backend.hcl
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

`../scalable-infra/aws` retains the earlier ECS/Fargate plus RDS layout for future use.
