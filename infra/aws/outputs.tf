output "backend_public_ip" { value = aws_instance.backend.public_ip }
output "post_call_queue_url" { value = aws_sqs_queue.post_call.url }
output "post_call_dlq_url" { value = aws_sqs_queue.post_call_dlq.url }
output "object_storage_bucket" { value = aws_s3_bucket.objects.bucket }
