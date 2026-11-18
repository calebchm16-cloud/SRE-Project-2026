# Cahuita Conditions

A Python weather and marine forecast demonstration for Cahuita, Costa Rica. FastAPI
serves traditional Bootstrap 3 HTML, read-only JSON APIs, health checks and Prometheus
metrics. Open-Meteo forecasts are cached in SQLite. All application functions work
without JavaScript; API documentation uses Swagger UI separately.

<!-- deployment:start -->
## Laboratory deployment

[![CI](https://github.com/calebchm16-cloud/SRE-Project-2026/actions/workflows/ci.yml/badge.svg)](https://github.com/calebchm16-cloud/SRE-Project-2026/actions/workflows/ci.yml)

Status: **Running when last verified** (2026-11-18 06:38:26 UTC).

- Application: [http://100.54.133.50/](http://100.54.133.50/)
- API documentation: [http://100.54.133.50/docs](http://100.54.133.50/docs)
- Readiness: [http://100.54.133.50/health/ready](http://100.54.133.50/health/ready)
- Source: [`cd845debb671`](https://github.com/calebchm16-cloud/SRE-Project-2026/commit/cd845debb67154a1f39fb566b19de1bb1a3b26f0)

- Monitoring access and Grafana credentials: [Open access page](http://100.54.133.50:3000/access)
- Grafana: [Open dashboards](http://100.54.133.50:3000/)
- Prometheus: [Open query interface](http://100.54.133.50:9090/)

Grafana uses the Viewer username/password listed on the public access page. Prometheus does not require login. No AWS account, SSO or tunnel is needed to view these services.

Alertmanager uses a private Systems Manager tunnel; see [AWS operation](docs/aws.md).

This is an ephemeral, read-only HTTP demonstration. The address can change after stop/start. This timestamped snapshot is not a live uptime check.
<!-- deployment:end -->

## Features

- Independent Open-Meteo weather and marine forecasts for configurable locations.
- Up to 48 hourly forecast intervals, metric units and location-local HTML timestamps.
- Persistent last-valid SQLite snapshots, partial responses and explicit freshness states.
- Traditional GET forms, compact bordered tables and locally served Bootstrap 3.4.1 CSS.
- JSON APIs, separate liveness/readiness checks, Prometheus metrics and JSON logs.
- Optional OpenTelemetry tracing and container support for AMD64 and ARM64.
- Terraform infrastructure, Ansible configuration and a K3s monitoring laboratory.

## Architecture

One FastAPI process serves HTML, JSON, health and metrics. A background task fetches
weather and marine data independently, validates each response and persists successful
downloads. Requests read the cache and never trigger provider calls. Failed downloads
preserve the last valid snapshot until it expires or has no usable future intervals.

The deployment uses one Ubuntu EC2 instance running K3s, a dedicated VPC/public subnet,
security group and encrypted EBS disk. Ansible configures Docker/K3s and deploys the
application, Prometheus, Grafana and Alertmanager. An auto-assigned public IPv4 is printed
by Terraform; it can change after stop/start. There is no Elastic IP, EKS or NAT Gateway.

| Component | Purpose | Access |
| --- | --- | --- |
| Application | Forecast HTML, JSON APIs and telemetry | Public read-only HTTP |
| Prometheus | Scraping, query history and alert evaluation | Private SSM tunnel |
| Grafana | Application Traffic and Forecast Operations dashboards | Private SSM tunnel and login |
| Alertmanager | Grouping, inhibition and FIRING/RESOLVED routing | Private SSM tunnel |
| Terraform / Ansible | Infrastructure and guest configuration | GitHub deployment workflow |

The application uses one Kubernetes replica with a Recreate deployment strategy and a
persistent `/data/cache.sqlite3` file. Prometheus, Grafana and Alertmanager each have
their own volume. Data survives pod replacement and instance stop/start; destroying
the laboratory removes the instance disk and its data.

## Deploy and operate

Complete the [AWS and GitHub setup](docs/aws.md), then run
**Actions → AWS laboratory → Run workflow → deploy** from the default branch. ARM64 is
the default architecture; AMD64 is also supported. GitHub-hosted runners execute the
jobs, and the application continues running on EC2 after the workflow finishes.

After verification, the generated section above contains the current application URL,
source revision and verification time. The same workflow provides `start`, `stop` and
`destroy` operations. Grafana, Prometheus and Alertmanager are accessed using private
Systems Manager tunnels as described in the AWS guide. No workstation Kubernetes
runtime is required.

## Workflows

| Workflow | Behavior |
| --- | --- |
| `ci.yml` | Frozen dependencies, Ruff, offline/API/browser tests, monitoring validation, controlled PromQL cases, Terraform mock tests and Ansible syntax; builds and executes AMD64 and ARM64 images |
| `laboratory.yml` | Manual deploy/start/stop/destroy using AWS OIDC; deploy consumes the image tested in the same run, checks the stack and updates README/tag metadata |

Actions retain test reports and short-lived image artifacts. Raw Terraform state/plans,
administrative logs, credentials and caches are excluded from published artifacts.
The final deployment tag points at the tested source commit. A README status timestamp
records successful verification; it is not a live uptime check.

## APIs

| GET path | Purpose |
| --- | --- |
| `/` | Forecast page, GET location selector and compact hourly table |
| `/api/v1/locations` | Configured locations |
| `/api/v1/locations/{location_id}/forecast` | Cached combined forecast: 200 if any source is usable; 503 otherwise; 404 for unknown location |
| `/api/v1/status` | Source, storage and updater status |
| `/health/live` | Process response |
| `/health/ready` | Functional forecast availability |
| `/metrics` | Prometheus metrics |
| `/docs`, `/openapi.json` | API documentation and schema |

Each forecast includes its location, generation timestamp, overall state and separate
weather/marine sections. Each section reports requested/model-grid coordinates, units,
last valid download, age, refresh result, coverage and hourly records. JSON timestamps
use UTC; HTML uses the configured location timezone. Missing values are null in JSON
and dashes in HTML. Records are aligned for display without interpolation.

An overall state is `ok`, `degraded` or `unavailable`. A source is `fresh`, `stale` or
`unavailable`. The page continues showing its form and source status when the forecast
API returns 503. Liveness checks process responsiveness; readiness additionally requires
working storage, a running updater and at least one usable source for every location.
Partial or permitted stale data can remain Ready.

## Configuration and freshness

The AWS deployment refreshes weather/marine every **30 seconds**; Prometheus scrapes every
**5 seconds** and Grafana refreshes every **10 seconds**. The HTML page reloads every
60 seconds. Cache becomes stale at 30 minutes and unusable after six hours, or earlier
when no future hours remain. Download age is distinct from model-run age.

| Setting | Application default | AWS laboratory |
| --- | --- | --- |
| `CAHUITA_DATABASE_PATH` | `data/cache.sqlite3`; `/data/cache.sqlite3` in the image | `/data/cache.sqlite3` on a PVC |
| `CAHUITA_REFRESH_SECONDS` | `900` | `30` |
| `CAHUITA_STALE_SECONDS` | `1800` | `1800` |
| `CAHUITA_EXPIRE_SECONDS` | `21600` | `21600` |
| `CAHUITA_REQUEST_TIMEOUT_SECONDS` | `10` | `10` |
| `CAHUITA_WEATHER_BASE_URL` | `https://api.open-meteo.com` | Same |
| `CAHUITA_MARINE_BASE_URL` | `https://marine-api.open-meteo.com` | Same |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Unset; no trace export | Unset |

See [.env.example](.env.example) for additional settings and
[the location configuration](config/locations.example.json) for the JSON schema example.
Cahuita starts at approximately 9.74, -82.84 in `America/Costa_Rica`. Configuration
changes require a process restart. Keep `refresh < stale < expire`.

Alert pending windows and cache expiry have their own timers: a shorter provider poll
does not guarantee an alert within one minute. See [monitoring and Telegram](docs/monitoring.md)
and the [isolated failure demonstration](docs/failure-recovery.md).

## Observability

Prometheus collects request counts/latency, provider attempts, refresh results, last
successful downloads, cache age, usability and available forecast hours. A target with
`up=1` can still have `forecast_data_usable=0`: successful scraping and usable forecasts
are separate conditions.

Grafana provisions two dashboards from repository files:

- **Application Traffic:** requests, separate 4xx/5xx errors and mean/p95/p99 latency,
  including Kubernetes probes.
- **Forecast Operations:** weather/marine refresh outcomes, download age, availability,
  coverage and upstream latency.

Alertmanager groups and inhibits alerts for application reachability/readiness, repeated
refresh failures and unusable forecasts. A private Telegram bot can receive FIRING and
RESOLVED messages once configured. Delivery remains disabled until configuration, and
real-channel acceptance requires a real alert/recovery test. See
[monitoring and Telegram](docs/monitoring.md) for queries, rules and operation.

Request and refresh logs are JSON on stdout, with UTC timestamps, request IDs and
trace/span IDs when present. Optional tracing instruments HTTP requests, provider calls
and background refreshes; health/metrics are excluded. Export failures do not determine
readiness.

## Tests and evidence

Routine tests use controlled clocks and simulated HTTP responses. Chromium tests exercise
the application with JavaScript disabled. CI starts the actual image without provider
connectivity and verifies liveness/HTML/metrics, unavailable-cache readiness, and orderly
shutdown. Kubernetes and live AWS acceptance are checked during deployment.

[Reference screenshots](docs/evidence/failure-recovery/README.md) document a prior
implementation test with explicitly simulated notification delivery. Their run metadata
is retained. They are separate from deployment evidence generated by this repository's
workflows and from real Telegram acceptance. The failure-demo guide describes how to
produce a new, isolated run.

For development and verification commands, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Repository layout

- `src/`: Python application, Jinja templates and original local Bootstrap 3.4.1 CSS.
- `tests/`: application, telemetry, alerting, AWS orchestration and evidence tests.
- `.github/workflows/`: hosted CI, image builds and AWS laboratory delivery.
- `infrastructure/`: laboratory resources, private state backend and scoped GitHub OIDC role.
- `deployment/ansible/`: Docker, K3s, application and monitoring configuration.
- `k8s/`, `monitoring/`: Kustomize resources and provisioned dashboards/rules.
- `scripts/`: shared CI/EC2 deployment, validation and controlled failure-demo helpers.
- `docs/`: setup, monitoring and failure/recovery operation.

## Data and licenses

Application code uses the [MIT license](LICENSE.md). Bootstrap's original MIT notice is
preserved at `src/cahuita_conditions/static/vendor/BOOTSTRAP-LICENSE.txt`.
Forecasts: [Open-Meteo](https://open-meteo.com/), data under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Credit the
[weather](https://open-meteo.com/en/docs) and
[marine model providers](https://open-meteo.com/en/docs/marine-weather-api#data_sources).
Model-grid marine values are regional forecasts, not beach observations or activity-safety
ratings. Provider service terms and commercial access are separate from the data license;
this repository is an academy demonstration.
