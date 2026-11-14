# AWS infrastructure root

This root provisions a public HTTPS ALB forwarding to private ECS/Fargate tasks, private RDS PostgreSQL with encryption and automated backups, an encrypted private S3 bucket, Secrets Manager values, and an image-based post-call Lambda. API and worker access to PostgreSQL is limited by security-group references. ECS tasks use task roles for object storage and Lambda invocation; secrets are injected through ECS or fetched by the Lambda role at runtime.

Use an existing VPC with at least two public and two private subnets in separate availability zones. Private subnet egress must reach ECR, CloudWatch, Secrets Manager, and Vertex AI. Use a VPC endpoint/NAT design appropriate for your account and data policy. Provide an ACM certificate in the ALB region. Push both application images before applying. Build the Lambda image for `linux/arm64` to match the configured Lambda architecture.

Terraform creates the database password, encryption keys, and Vobiz secrets and writes them to Secrets Manager. Its state therefore contains secrets. Configure a remote encrypted and locked backend before applying. The generated Google service-account credential is expected to be supplied separately through `vertex_credentials_secret_arn`; never place its content in a `.tfvars` file.

The API service starts with `api_desired_count = 0` so it cannot boot against an unmigrated database. After the first apply, run the one-off migration task with the generated task definition in the existing private subnets and the API security group, wait for exit code 0, then apply again with `api_desired_count = 2` (or your chosen capacity). For example:

```sh
aws ecs run-task \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --task-definition "$(terraform output -raw migration_task_definition_arn)" \
  --launch-type FARGATE \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-private-a,subnet-private-b],securityGroups=['"$(terraform output -raw api_security_group_id)"'],assignPublicIp=DISABLED}'
```

Wait for the returned task ARN with `aws ecs wait tasks-stopped`, then inspect its container `exitCode` using `aws ecs describe-tasks`. Apply again with the API count only after exit code 0. Repeat the migration task before each API rollout that changes the schema. Keep the same order as the GCP Cloud Build release: migrate successfully, then roll out API workers.

The template caps Lambda concurrency to protect database connections. Set the cap against RDS `max_connections`, ECS task count, and the queue dispatch/recovery behavior. The API image requires outbound access to Google Vertex and tenant project provisioning APIs. Grant the GCP identity in `gcp_runtime_service_account` the required organization/folder, billing, Service Usage, IAM, and budget roles outside AWS.

The target group health check uses `/readyz`. Make sure the OIDC provider values and Vobiz callback URL are configured before accepting customer traffic. Schema migrations are still a deployment step; run `alembic upgrade head` once against the new database before starting the API and Lambda worker.
