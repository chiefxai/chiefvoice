# Monitoring setup

Import `grafana/chiefvoice-dashboard.json` into Grafana and select the Prometheus data source for `${DS_PROMETHEUS}`. It expects Chiefvoice's `/metrics` endpoint to be scraped with a Prometheus `job` label. `prometheus/chiefvoice-alerts.yml` contains alerting rules for sustained HTTP 5xx errors, failed outbound call starts, exhausted post-call jobs, exhausted CRM webhook deliveries, telephony provider call errors, failed recording uploads, and failed backup/retention runs.

Example scrape configuration:

```yaml
scrape_configs:
  - job_name: chiefvoice
    metrics_path: /metrics
    static_configs:
      - targets: ["chiefvoice-api:8000"]
```

Load the alert file under Prometheus `rule_files`, and configure an Alertmanager receiver for `service="chiefvoice"`. Thresholds are starter defaults; tune them to call volume and the response times your team supports. The counters report failures and lifecycle events but do not expose queue age or current queue depth, so a database-backed queue-lag gauge remains a useful follow-on metric.
