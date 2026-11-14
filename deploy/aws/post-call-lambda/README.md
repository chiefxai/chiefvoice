# Post-call Lambda image

This image packages only the post-call runtime, not the Pipecat telephony stack:

```sh
docker build --platform linux/arm64 -t chiefvoice-post-call:latest -f deploy/aws/post-call-lambda/Dockerfile .
```

Push the image to ECR and create a Lambda container-image function with handler `chiefvoice.agents.post_call.lambda_handler.handler`. Use the same architecture as the image. Set a timeout above the Postgres worker's 150-second model timeout plus cold-start and database setup time; Lambda allows at most 15 minutes. Ensure the configured Lambda timeout and concurrency match the database connection capacity.

Provide `DATABASE_URL`, `POST_CALL_ENCRYPTION_KEY`, `POST_CALL_AGENT_PROVIDER=langchain_vertex`, `POST_CALL_MODEL`, `VERTEX_PROJECT_ID`, and `VERTEX_LOCATION`. Prefer Secrets Manager references or the Lambda execution role's Google Cloud federation/ADC over plaintext deployment files. If using `VERTEX_CREDENTIALS_JSON`, store it as a secret and grant read access only to this function. The API role needs `lambda:InvokeFunction` for this function.

The Lambda function needs network access to Postgres and Google Vertex AI, CloudWatch Logs permissions, and access to credentials used for Vertex AI. If Postgres is private, attach the function to the appropriate VPC subnets/security groups and ensure outbound Vertex connectivity through a NAT gateway or approved egress path. Limit Lambda concurrency to protect Postgres connection capacity; this handler opens one async database engine per invocation.

For the API's S3-compatible fallback spool, grant its role `s3:ListBucket` constrained to the configured object-storage prefix and `s3:GetObject`, `s3:PutObject`, and `s3:DeleteObject` on the `post-call-recovery/` prefix. The recovery objects are application-encrypted with `POST_CALL_ENCRYPTION_KEY`; do not set automatic object expiration while pending jobs may exist. Apply database migrations before starting the API or Lambda worker.
