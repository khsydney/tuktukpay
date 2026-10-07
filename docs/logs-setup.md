# Logs: Splunk Cloud Platform (HEC) + Log Observer Connect

This is the path a log line takes and what you have to set up on each side.

```
service (JSON stdout + OTLP log record with trace_id, span_id, service.name, business fields)
   │ OTLP
   ▼
Splunk OTel Collector  ── logs pipeline: transform/logs (source=service, severity) ── splunk_hec ──►  Splunk Cloud Platform
                                                                                                   index=tuktukpay
                                                                                                        ▲
Splunk Observability Cloud (realm us1)  ──  Log Observer Connect (search head API, port 8089, service account) ──┘
   Log Observer · Related Content ("Logs for this trace") · AI Assistant · AI Troubleshooting Agent (Evidence tab)
```

Why this shape: the Observability Cloud org used for the workshop has no Observability Logs entitlement (its
`ingest.<realm>…/v1/log` endpoint answers 404), so logs are indexed in a Splunk Cloud Platform stack and
Observability Cloud reads them in place through Log Observer Connect. That is also how most production
customers run it.

## 1. Splunk Cloud Platform

Everything below needs the `sc_admin` role.

### 1.1 Index

**Settings → Indexes → New Index**: name `tuktukpay`, type *Events*, max raw data size **20 GB**, no additional
storage, searchable retention 30 days (14 is fine too). Sizing: at the baseline 4 payments/s the stack writes
about 0.5 GB/day; a ×10 festival-spike act adds 1–2 GB per hour it runs; multiply by the number of team stacks.
A 1 GB cap fills during the first act and Splunk starts freezing buckets mid-workshop — don't.

### 1.2 HEC token

**Settings → Data Inputs → HTTP Event Collector → New Token** (or edit an existing one):

* Name e.g. `tuktukpay`; source / sourcetype left empty (the collector sets `source=<service>`, `sourcetype=otel`).
* **Selected indexes**: `tuktukpay`; **Default index**: `tuktukpay`.
* **Enable indexer acknowledgement: OFF** — with it on, HEC rejects the collector's requests with
  `{"text":"Data channel is missing","code":10}`.
* Global settings (the **Global Settings** button on the HEC page): *All tokens* enabled.

The endpoint for the collector is **not** the stack's web address:

| Stack hosting | HEC endpoint |
|---|---|
| Splunk Cloud on AWS | `https://http-inputs-<stack>.splunkcloud.com:443/services/collector` |
| Splunk Cloud on GCP | `https://http-inputs.<stack>.splunkcloud.com:443/services/collector` |
| Splunk Enterprise | `https://<hec-host>:8088/services/collector` |

### 1.3 IP allow list

**Settings → Server settings → IP allow list** (Victoria experience; on Classic stacks it is a support ticket).
Two separate lists matter:

| List | Add | Why |
|---|---|---|
| **HEC access for ingestion** | the public IP of every machine running the stack (`curl -4 https://api.ipify.org`) as `/32` | the collector pushes to HEC; an unlisted source is dropped silently — `curl` just times out, there is no 403 |
| **Search head API access** | the Observability Cloud IPs of your realm, e.g. **us1**: `44.230.152.35/32`, `44.231.27.66/32`, `44.225.234.52/32`, `44.230.82.104/32` | Log Observer Connect queries the search head on port 8089 from Observability Cloud |

Other realms are listed on the [Log Observer Connect setup page](https://help.splunk.com/en/splunk-observability-cloud/manage-data/view-splunk-platform-logs/set-up-log-observer-connect-for-splunk-cloud-platform).
Changes take a few minutes to apply. Corporate VPN egress IPs change — if `make hec-test` starts timing out
again, re-check your IP.

### 1.4 Service account for Log Observer Connect

1. **Settings → Roles → New role** `o11y_logs`: *Indexes* tab — deselect *(All internal indexes)*, select
   `tuktukpay` only; *Capabilities* — `search` and `edit_tokens_own` selected, `indexes_list_all` **not** selected;
   search limits: a concurrent-search limit of 4 × the number of Log Observer users (each user search runs four
   back-end searches), max search time range 30 days.
2. **Settings → Users → New user** `o11y_connect` with that role.
3. **Settings → Tokens**: token authentication must be enabled on the stack (the connection uses the user's
   password to mint a token).

## 2. The stack

```bash
# .env (gitignored)
SPLUNK_HEC_URL=https://http-inputs-<stack>.splunkcloud.com:443/services/collector
SPLUNK_HEC_TOKEN=<the token>
SPLUNK_HEC_INDEX=tuktukpay
# LOG_LEVEL=INFO          # DEBUG adds the per-request healthy events

make hec-test             # one test event straight to HEC -> expect {"text":"Success","code":0} [http 200]
docker compose up -d      # (re)creates the collector with the new settings; services unchanged
make logs-status          # otelcol_exporter_sent_log_records{exporter="splunk_hec"} must grow, send_failed must not
```

Then in Splunk: `index=tuktukpay | stats count by source, event` — within a minute you should see every service
(`checkout-api`, `risk-engine`, `payment-router`, `acquirer-sim`, `ledger`, `webhook-dispatcher`, `merchant-sim`,
`merchant-copilot`, `wallet-sim`, `shopping-agent`, `merchant-storefront`).

| Symptom | Cause |
|---|---|
| `make hec-test` times out | your IP is not on the *HEC access for ingestion* allow list, or the wrong hostname form (AWS vs GCP) |
| `{"text":"Invalid token","code":4}` | token mismatch / disabled |
| `{"text":"Incorrect index","code":7}` | `SPLUNK_HEC_INDEX` is not in the token's *Selected indexes* |
| `{"text":"Data channel is missing","code":10}` | indexer acknowledgement is on for the token |
| collector logs `HTTP "/v1/log" 404` | `SPLUNK_HEC_URL` is unset, so the collector fell back to the Observability Cloud endpoint and the org has no Observability Logs entitlement |
| `sending queue is full` / services log `data refused due to high memory usage` | the HEC endpoint is unreachable; the exporter now gives up after 2 minutes per batch so traces are unaffected, but fix the endpoint |

Without `SPLUNK_HEC_URL` the stack still runs; logs stay in `docker compose logs` only.

## 3. Observability Cloud

### 3.1 Connect the stack

**Logs → Logs connections → Add new connection → Splunk Cloud Platform**: a name, the service-account username and
password, the URL **`https://<stack>.splunkcloud.com:8089`**, then choose which Observability Cloud roles may use the
connection. Log Observer now shows `index=tuktukpay` events; the Splunk search bar accepts SPL.

### 3.2 Field mapping (what makes Related Content and the AI features work)

The services already use the field names Observability Cloud expects — `service.name`, `deployment.environment`,
`trace_id`, `span_id`, `host.name` — and the collector adds `severity`. Two settings still have to exist on the
Observability Cloud side:

* **Logs → Log Observer field aliasing**: Observability Cloud resolves logs to entities through aliases. Check
  that `service.name`, `deployment.environment`, `host.name`, `trace_id` (and the `k8s.*` fields for the Kubernetes
  variant) are mapped; create identity aliases if the list is empty. A field that only needs renaming (e.g. a
  third-party log with `trace.id`) is aliased here too.
* **Logs → Logs connections → *(connection)* → Indexes with mappings / Generation policy**: entity-index mappings
  tell Related Content and the Troubleshooting Agent which index holds logs for which `service.name`. The
  automatic mapping runs on a schedule (default every 24 h) and can be triggered manually; run it once after the
  first logs have arrived, and before the workshop day so the first demo is not an `index=*` search.
* **Severity**: Log Observer Connect recognises `severity`, `level` or `otel.log.severity.text`; the collector's
  `severity` field (`info`, `warn`, `error`…) is the one it uses for the histogram colours and the severity filter.

### 3.3 Verify

1. APM → a `checkout-api` trace → **Related Content** → *Logs for this trace*: the lines of every service in that
   trace, from `merchant-storefront` to `webhook-dispatcher`.
2. Infrastructure → the host/pod → Related Content → Logs.
3. AI Assistant: *"Show me the error logs for payment-router in `<environment>` in the last 15 minutes."*
4. Start Act 1 from the chaos panel, wait for the `payment-router p90 latency above 1s` detector, open the alert →
   **AI Troubleshooting Agent → Evidence**: it quotes `event=acquirer.timeout` lines. See
   [ai-troubleshooting.md](ai-troubleshooting.md) for every act.

## 4. Kubernetes and dual-ship variants

* **Kubernetes** (`k8s/values-splunk-otel-collector.yaml`): the chart's `splunkPlatform` block takes the same
  endpoint, token and index; the applications export their own OTLP logs, so container stdout collection is off
  (`logsCollection.containers.enabled: false`) to avoid duplicates. Turn it back on if you want the logs of
  third-party pods (Postgres, Redis) — those lines carry `k8s.*` fields but no `service.name`.
* **Dual-ship** (`docker-compose.dualship.yml`): the contrib collector sends the same log records to both the
  Splunk HEC and the second vendor; the environment variables are identical.

## 5. Good to know

* One line per payment at `checkout-api`, `payment-router` and `acquirer-sim`; the other services log only
  anomalies at `INFO`/`WARN`/`ERROR` and keep the healthy path at `DEBUG`. That is deliberate: a war-room index
  full of "everything is fine" lines hides the one line that matters.
* `event=config.changed` lines are the stack's deploy/feature-flag trail: a root-cause search should start with
  *"what changed right before the symptom?"* — exactly what the Troubleshooting Agent and the AI Assistant do.
* Rotate the HEC token after the workshop; it was pasted into `.env` on several laptops.
