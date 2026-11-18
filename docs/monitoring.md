# Monitoring and Telegram

Prometheus scrapes the application's metrics every five seconds, including pods that are
Running but NotReady. Kubernetes discovery refreshes pod addresses automatically, using
only read/list/watch access to application pods. Prometheus and Alertmanager also scrape
themselves; APIs for administrative actions and HTTP reload remain disabled.

Grafana provisions **Application Traffic** (`cahuita-application`) and **Forecast Operations**
(`cahuita-forecasts`) from versioned files. Dashboards include probes, distinguish 4xx/5xx,
calculate latency percentiles from rates before aggregation and show absent series as
No data. They use `America/Costa_Rica` and refresh every ten seconds.

## Queries

Filter application queries by `job="cahuita",namespace="cahuita-conditions"`:

```promql
up{job="cahuita",namespace="cahuita-conditions"}
sum by (route,status_code) (rate(http_requests_total{job="cahuita",namespace="cahuita-conditions"}[2m]))
histogram_quantile(0.95,sum by (le) (rate(http_request_duration_seconds_bucket{job="cahuita",namespace="cahuita-conditions"}[2m])))
sum by (source,outcome) (increase(upstream_requests_total{job="cahuita",namespace="cahuita-conditions"}[1h]))
forecast_cache_age_seconds{job="cahuita",namespace="cahuita-conditions"}
time()-forecast_last_success_timestamp_seconds{job="cahuita",namespace="cahuita-conditions"}
forecast_data_usable{job="cahuita",namespace="cahuita-conditions"}
forecast_available_hours{job="cahuita",namespace="cahuita-conditions"}
```

`up=1` means metrics collection works. `forecast_data_usable` reports actual usable data.
Download age is time since the last valid fetch, not model-run age. One source can expire
while the other keeps readiness successful. When both expire, readiness is 503 while the
process and metrics remain live; use the pod-level demo tunnel to inspect this state.

## Alerts

| Rule | Condition | Severity / pending |
| --- | --- | --- |
| CahuitaApplicationUnreachable | All targets unreachable, or expected target absent | Critical / 2m |
| CahuitaApplicationNotReady | Readiness probes 503 without successful probes in the 1m window, exporter accessible | Critical / 2m |
| CahuitaRepeatedRefreshFailures | At least 3 failed refresh increments in 1h, none successful in 30m | Warning / 5m |
| CahuitaForecastSourceUnusable | A present source reports zero usability, exporter accessible | Warning / 5m |
| CahuitaForecastsUnavailable | Both sources present and neither usable, exporter accessible | Critical / 2m |

HTTP retries and forecast-refresh results are different counters. Alerts use stable
namespace/location/source identities. Unreachable inhibits derived alerts; complete loss
inhibits per-source warnings/readiness; an unusable source inhibits its refresh warning.
Normal grouping is 30s initial wait, 1m update interval, 4h repeat. All components share
one VM, so a stopped VM cannot collect metrics or deliver its own alerts.

## Telegram setup and real acceptance

From an administrative SSM root shell in `/opt/cahuita/release`:

1. Create a dedicated bot through official BotFather `/newbot`.
2. Open that bot's private chat and send `/start`.
3. Run `python3 scripts/alertmanager.py configure-telegram` and enter the token at the
   hidden prompt. Select a private destination if several are detected.
4. Run `python3 scripts/alertmanager.py deploy`, then
   `python3 scripts/alertmanager.py test-notification`.

The helper stores token/chat ID in a Kubernetes Secret and uses native file-based Telegram
configuration. It sanitizes errors and does not put credentials in Git or process arguments.
Before setup, delivery is explicitly disabled with an `unconfigured` receiver. Local alert
evaluation still works; previous resolved alerts are not queued for later delivery.

`send_resolved: true` produces FIRING and RESOLVED messages with UTC timestamps and the
condition's scope. The real acceptance test labels its synthetic incident as a test and
requires completed successful delivery requests for both stages. Simulated receipts are
not evidence of a real Telegram message. Inspect active alerts/silences privately through
the Alertmanager tunnel, and keep passwords, tokens and private chat content out of evidence.
