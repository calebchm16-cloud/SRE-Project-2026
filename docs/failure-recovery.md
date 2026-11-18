# Isolated failure and recovery demonstration

The demo follows healthy, cached-failure, partial, unavailable and recovered phases using
an external weather/marine mock and a **SIMULATED DELIVERY** Telegram API receiver.
It preserves the baseline application's manifests and volumes and deletes only its
owned temporary namespace. Real Telegram acceptance is a separate final test.

## Execution environment

Run inside an authorized Linux operator environment connected to the deployed K3s
cluster, such as an EC2 administrative root SSM shell. Do not run the demo against an
unrelated cluster. The baseline app and all three monitoring services must be Ready.
The helper uses explicit Docker/Kubernetes contexts; it does not provision a workstation VM.

The browser demo needs Python 3.14, uv, Chromium and the frozen development dependencies
in addition to the production guest's Python tools. Install uv in an isolated tools
environment, then run from `/opt/cahuita/release`:

```sh
export CAHUITA_CONTEXT=cahuita-server
export KUBECTL_BIN=/usr/local/bin/kubectl
export KUBECONFIG=/etc/cahuita/kubeconfig.yaml
uv sync --frozen
uv run playwright install --with-deps chromium
python3 scripts/prometheus.py status
python3 scripts/grafana.py status
python3 scripts/alertmanager.py status
```

The demo adds temporary application, provider, Prometheus, Grafana, Alertmanager and
receiver workloads. Allow adequate CPU/RAM and startup time; the tight accelerated
phase windows continue advancing while commands and captures run. The included reference
captures are from a prior verified run and do not establish acceptance on your VM.

## Automatic run

```sh
python3 scripts/failure_demo.py run
```

Allow approximately 5–8 minutes, including startup, cache expiration, notifications and
Chromium captures. The command prints the run ID immediately and removes its namespace
after success, failure, Ctrl-C or SIGTERM. Full artifacts are retained under
`artifacts/failure-demo/<run-id>/`, which is ignored by Git. A completed run prints:

```text
Complete run: <run-id>. Publish with publish-evidence --run-id <run-id>
```

Publication is a separate, local operation:

```sh
python3 scripts/failure_demo.py publish-evidence --run-id <run-id>
```

This copies exactly twelve PNGs plus a sanitized English summary and provenance manifest
to `docs/evidence/failure-recovery/`. A run must have all five verified stages, successful
cleanup and unchanged real workloads. Failed or incomplete runs cannot be published as
successful evidence. Publication verifies run/namespace provenance and PNG SHA256 hashes.
It excludes passwords, Kubernetes Secrets, browser cookies and raw authentication headers.

## Manual walkthrough

```sh
python3 scripts/failure_demo.py start
```

Record the printed `RUN_ID`. Start this command in another terminal and leave it running:

```sh
python3 scripts/failure_demo.py access --run-id <run-id>
```

| Interface | Local URL |
| --- | --- |
| Application | <http://127.0.0.1:18080/?location=cahuita> |
| Forecast API | <http://127.0.0.1:18080/api/v1/locations/cahuita/forecast> |
| Readiness | <http://127.0.0.1:18080/health/ready> |
| Metrics | <http://127.0.0.1:18080/metrics> |
| Prometheus alerts | <http://127.0.0.1:19090/alerts> |
| Alertmanager | <http://127.0.0.1:19093> |
| Grafana | <http://127.0.0.1:13000> |

All forwards bind only to `127.0.0.1`. The application forward targets the pod directly,
so its API and metrics remain reachable when Kubernetes marks it NotReady. Ctrl-C in
the access terminal stops only its forwards. The normal ports 8080/9090/9093/3000 are free
for the real deployment. Other demo operations use short-lived random localhost ports.

Retrieve the temporary Grafana login only when needed:

```sh
python3 scripts/failure_demo.py credentials --run-id <run-id>
```

Keep that output out of screenshots and recordings. Login is `admin`; dashboards are in
**Cahuita Conditions - Failure Demo**. Their copies show demo freshness thresholds of
15 and 120 seconds and query only this run's Prometheus. The real dashboards are unchanged.

Advance the stages in order, in the first terminal:

```sh
python3 scripts/failure_demo.py phase cached --run-id <run-id>
python3 scripts/failure_demo.py phase partial --run-id <run-id>
python3 scripts/failure_demo.py phase unavailable --run-id <run-id>
python3 scripts/failure_demo.py phase recovered --run-id <run-id>
```

| Phase | Change and expected observation | Typical wait after command |
| --- | --- | --- |
| Healthy | Both downloads work, forecast `ok`, API/readiness 200, 48 hours per source, no firing alerts | Startup 30–90s |
| Cached | Marine HTTP 503, last valid marine download unchanged, API 200 `degraded`, cached data still usable; repeated-refresh warning and accepted simulated delivery | 15–35s |
| Partial | Marine cache expires: marine coverage/availability zero, weather still usable, API/readiness 200; source-unusable warning | Until 120s after marine's last success, plus evaluation/delivery |
| Unavailable | Weather also fails and expires: forecast/readiness 503, liveness/metrics 200, exporter `up=1`; critical alert and accepted simulated delivery | Approximately 130s |
| Recovered | Both providers restored, usable forecasts/coverage and 200 responses return, firing conditions clear, critical RESOLVED receipt accepted | 10–30s plus history captures |

Only the mock's control file changes. Weather and marine URLs remain
`http://mock-provider:9000` throughout, preserving the cache configuration fingerprint.
The application has no fault-control endpoints. Refresh is every 1s; cached data becomes
stale at 15s and unusable at 120s. Scraping is every 5s, rule evaluation every 1s and alert
pending duration 5s. Repeated failures require three failed refresh increments in 1m and
no successful refresh in 10s. HTTP retries are not counted as separate refreshes.
Alertmanager uses a 1s group wait and 10s group interval, with the normal inhibition rules.
Prometheus initially loads an empty rule bundle. Once discovery and scraping report both
sources usable, the helper installs the rules and sends SIGHUP to the temporary Prometheus.
This keeps HTTP reload/admin APIs disabled and avoids target-absence alerts during startup.
`rule-activation.json` records the successful scrapes before activation; healthy startup
must have zero notification receipts to qualify for publication.

The clock keeps advancing while you talk or inspect the UI. Run `partial` soon after
capturing `cached`; the cached state cannot be held indefinitely. If a stage has already
changed, capture refuses to label it incorrectly. Use the saved screenshots to explain
an earlier stage. `recovered` is allowed early to restore sources, but skipping stages
makes the evidence incomplete and prevents publication.

```sh
python3 scripts/failure_demo.py status --run-id <run-id>
python3 scripts/failure_demo.py capture --run-id <run-id>
```

`status` reports the live state and the last captured stage. `capture` rechecks and records
that stage only if it is still observed. For a completed manual walkthrough, clean up
before publication:

```sh
python3 scripts/failure_demo.py cleanup --run-id <run-id>
python3 scripts/failure_demo.py publish-evidence --run-id <run-id>
```

Manual sessions remain running after a phase error or interruption so they can be
inspected, recovered or explicitly cleaned. If the automatic process is forcibly killed
with SIGKILL or the operator process loses its connection, run `cleanup` with its printed ID after reconnecting to the cluster. Cleanup checks namespace ownership and UID before deletion, is idempotent, and
never deletes the real namespaces or their PVCs. Do not remove temporary namespaces by
wildcard. Only one mutating operation per run is allowed at a time.

## Evidence and interpretation

Each phase stores API bodies and status codes, status/health/metrics responses, exact
PromQL queries and returned samples, firing alerts, Alertmanager state, mock receipts,
completed notification counters, Kubernetes readiness, pod identity and UTC timestamps.
Rendered Prometheus rules/configuration, temporary Grafana provisioning and screenshot
metadata are also kept in the run directory. The run state records demo timings, image,
constant provider URLs and before/after real Deployment specs, pod UIDs and PVC UIDs.

Useful queries against the **temporary** Prometheus include:

```promql
up{job="cahuita",namespace="cahuita-alert-validation-<run-id>"}
forecast_cache_age_seconds{job="cahuita",namespace="cahuita-alert-validation-<run-id>"}
forecast_data_usable{job="cahuita",namespace="cahuita-alert-validation-<run-id>"}
forecast_available_hours{job="cahuita",namespace="cahuita-alert-validation-<run-id>"}
increase(forecast_refresh_total{job="cahuita",namespace="cahuita-alert-validation-<run-id>",outcome!="success"}[1m])
ALERTS{alertstate="firing"}
```

`up=1` proves successful metrics collection, even with readiness 503; it does not mean
usable forecasts exist. Download age is time since the last valid fetch, not the age
of the meteorological model. API and metrics are sampled moments apart, and scraping
adds up to 5 seconds of lag. The checks wait for the relevant alert and observed state.

The five application screenshots use Chromium with JavaScript disabled. Two Grafana
screenshots use a fixed range spanning the same incident. Prometheus and Alertmanager
screenshots use their actual interfaces. Two Bootstrap 3 receipt pages show recorded
critical FIRING and RESOLVED messages, HTTP acceptance and run correlation. They are
explicitly labeled **SIMULATED DELIVERY** and do not imitate Telegram. Resolution refers
to the indicated condition rather than claiming the entire system is healthy.

See the [selected evidence and manifest](evidence/failure-recovery/README.md). Full artifacts
remain local; only the whitelisted selected evidence is intended for Git. A real Telegram
acceptance test remains the final separate step in [Alertmanager operations](monitoring.md).

