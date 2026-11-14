# Monitoring setup

Import `grafana/chiefvoice-dashboard.json` into Grafana and select the Prometheus data source for `${DS_PROMETHEUS}`. It expects Chiefvoice's `/metrics` endpoint to be scraped with a Prometheus `job` label. `prometheus/chiefvoice-alerts.yml` contains alerting rules for sustained HTTP 5xx errors, failed outbound call starts, exhausted post-call jobs and CRM webhook deliveries, Lambda dispatch failures, old queued jobs, enqueue fallback failures, telephony provider call errors, failed recording uploads, and failed backup/retention runs.

Example scrape configuration:

```yaml
scrape_configs:
  - job_name: chiefvoice
    metrics_path: /metrics
    static_configs:
      - targets: ["chiefvoice-api:8000"]
```

Load the alert file under Prometheus `rule_files`, and configure an Alertmanager receiver for `service="chiefvoice"`. Thresholds are starter defaults; tune them to call volume and the response times your team supports. The API exposes tenant-free Postgres gauges `chiefvoice_post_call_queue_jobs{status=...}` and `chiefvoice_post_call_queue_oldest_queued_age_seconds`. Each API replica samples the same shared queue, so use `max`/`max by(status)` rather than `sum` when aggregating these gauges across replicas.

When using Lambda, also configure CloudWatch alarms for Lambda `Errors`, `Throttles`, `AsyncEventsDropped`, and `AsyncEventAge`. Those metrics cover invocation/runtime issues; the Postgres gauges and `ChiefvoicePostCallQueueStalled` alert cover end-to-end backlog. A job may be invoked more than once, so Lambda and CRM consumers must remain idempotent.

The GCP Terraform root creates a Cloud Logging metric and Cloud Monitoring alert policy for structured `post_call_function_failed` entries. Add one or more verified notification channels with `monitoring_notification_channels` to deliver paging notifications. Cloud Function/API logs contain event names, error classes, and status only; they must not contain transcripts, caller data, or full job payloads.
