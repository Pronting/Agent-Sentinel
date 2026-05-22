# AIOps Alert Agent Monitoring

## Enable Metrics

Set the following environment variables:

```env
METRICS_ENABLED=true
APP_PORT=8000
```

The application exposes Prometheus metrics on:

```text
http://<agent-host>:8000/metrics
```

## Prometheus Scrape Example

```yaml
scrape_configs:
  - job_name: agent-sentinel
    metrics_path: /metrics
    static_configs:
      - targets:
          - agent-sentinel:8000
```

## Grafana

1. Add Prometheus as a Grafana datasource.
2. Import `config/grafana-aiops-dashboard.json`.
3. Select the Prometheus datasource in the dashboard import dialog.
4. Use the `Group ID` variable to filter Feishu group level traffic.

## Metrics Notes

- `trace_id` uses `group_id_message_id` and is written to logs.
- `trace_id` is not used as a Prometheus label to avoid high cardinality.
- `group_id` is used as a bounded label for dashboard filtering.
- `prometheus_client` metrics are thread-safe.
