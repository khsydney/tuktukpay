# AI troubleshooting with Splunk Observability Cloud — how TukTukPay is wired for it

This document has three parts: what Splunk's AI features do today and what they need from an environment
(verified against the Splunk documentation on 7 October 2026 — re-check before a session), how this stack meets
those needs, and a per-act playbook: which alert fires, what the AI should conclude, which log lines are the
evidence, and what to ask the AI Assistant.

## 1. The features

### AI Assistant in Observability Cloud

A natural-language interface over the whole platform. It can "diagnose issues when the root cause or affected
component is unknown", "investigate errors, latency, and resource utilization anomalies", "generate SignalFlow
queries" and "retrieve relevant traces, logs, alerts, and metrics". It reads APM, Infrastructure, RUM, alerts and
**logs tied to your services** — including Splunk platform logs through Log Observer Connect (log queries count
against the platform's SVC).

* Enable: an admin opts the org in under **Settings → General Organization Settings → AI Assistant Management**
  (entitlement required). Available in us0, us1, us2, us3, eu0, eu1, eu2, au0, jp0, sg0.
* Open: the AI Assistant icon in the right-hand toolbar, from anywhere.
* Prompting rules that matter: name the **service**, the **environment** and a **time range**, and say where to
  look ("look at my APM data and logs"). Good: *"What's wrong with the checkout-api service in tuktukpay-team01
  in the last 15 minutes? Look at APM data and logs."* Poor: *"What's wrong?"*
* Limits: English only, a context limit per chat (start a new chat when prompted), 30-day chat history.
* The same assistant also appears **inside Splunk Cloud Platform** ("AI Assistant in Related Content", from a
  search result's Related Content panel) when the stack and the org are paired through Unified Identity and the
  user holds `o11y_access` + `o11y_read_only|o11y_power|o11y_admin`.

Sources: [AI Assistant in Observability Cloud](https://help.splunk.com/en/splunk-observability-cloud/splunk-ai-assistant/ai-assistant-in-observability-cloud),
[prompt guide](https://help.splunk.com/en/splunk-observability-cloud/splunk-ai-assistant/prompt-guide-and-library-for-ai-assistant-in-observability-cloud),
[AI Assistant in Related Content](https://help.splunk.com/en/splunk-cloud-platform/search/search-manual/9.3.2408/observability/troubleshoot-with-the-ai-assistant-in-related-content).

### AI Troubleshooting Agent and Remediation Plan

Root-cause analysis that runs when an alert fires. It triggers automatically for **alerts related to Splunk APM
services**, **APM business transaction errors** and **Kubernetes alerts in Infrastructure Monitoring**; on any other
alert a user can still press **Run root cause analysis**. It correlates metrics, traces/spans (with their tags),
Kubernetes entities and **logs**, and presents on the alert page:

* **Overview** — alert summary, impact (blast radius), primary root cause.
* **Root Cause Analysis** — suspected causes with a confidence level, links to the evidence, thumbs up/down,
  regenerate.
* **Evidence** — the relevant logs, exemplar traces and services.
* **Action Plan / Remediation Plan** — hypotheses with `kubectl` commands and step tracking; **Kubernetes alerts
  only**.

What it needs, in Splunk's words: standard metrics only ("custom metrics are not supported at this time"); alerts
carrying `service.name`/`sf_service` and `deployment.environment`/`sf_environment`; and logs that "include the
same entity fields used by alerts and telemetry … service name, environment, trace ID, and span ID", mapped into
the entity index (Log Observer Connect's entity-index mappings). Analysis takes several minutes; it runs once per
user per alert (re-opening shows the cached result; *Regenerate* re-runs it); logs the user cannot read are
skipped. "RCA is opt-in by default for an org" — contact Splunk Support to enable automatic runs.

Source: [AI Troubleshooting Agent and Remediation Plan](https://help.splunk.com/en/splunk-observability-cloud/create-alerts-detectors-and-service-level-objectives/create-alerts-and-detectors/ai-troubleshooting-agent-and-remediation-plan).

### AI SRE

The agentic experience across the incident lifecycle (GA June 2026): AI-driven detection, alert grouping into one
incident, probable root cause with an evidence chain, guided remediation, and — since .conf26 — remediation
through Claude Managed Agents that can go from root cause to a pull request. It builds on the same signals as the
Troubleshooting Agent, so the preparation below serves both. Confirm the entitlement for the org before
promising it in a session.

Sources: [AI SRE](https://www.splunk.com/en_us/products/ai-sre.html),
[AI SRE remediation with Claude Managed Agents](https://www.splunk.com/en_us/blog/observability/ai-sre-in-splunk-observability-cloud-can-remediate-incidents-with-claude-managed-agents.html).

### Related Content and Log Observer Connect

Related Content is the glue: from a span to *Logs for this trace*, from a log line to the trace, service, host or
pod, from a service to its logs. It needs exact field names on the logs — `trace_id`, `span_id` (traces),
`service.name` + optional `deployment.environment` (services), `host.name` (hosts), `k8s.cluster.name`,
`k8s.namespace.name`, `k8s.pod.name`, `container.id` (Kubernetes) — or aliases that map your names to them.
Log Observer Connect queries the Splunk Cloud Platform index in place; entity-index mappings (automatic, on a
schedule, or triggered manually) tell it which index to search per service instead of `index=*`.

Sources: [Related Content](https://help.splunk.com/en/splunk-observability-cloud/data-tools/related-content),
[entity-index mappings](https://help.splunk.com/en/splunk-observability-cloud/manage-data/view-splunk-platform-logs/entity-index-mappings),
[field aliasing](https://help.splunk.com/en/splunk-observability-cloud/manage-data/view-splunk-platform-logs/create-field-aliases).

## 2. What the stack does to be "AI-ready"

| Requirement | Where TukTukPay meets it |
|---|---|
| `service.name` + `deployment.environment` on every span, metric and log | `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` per service; the collector upserts `deployment.environment` on all three signals |
| Alerts the Troubleshooting Agent runs on | `splunk/terraform/main.tf`: one **APM service detector** per act (`payment-router p90`, `risk-engine p90`, `merchant-copilot p90`, `checkout-api error rate`, `wallet-sim error rate`, `ledger p90`, `merchant-sim p90`) next to the business-KPI detectors on `tuktukpay.*` metrics, which the agent does not use |
| Logs with `trace_id`, `span_id`, `service.name`, `deployment.environment` | every runtime, see [log-schema.md](log-schema.md); exported over OTLP, indexed in Splunk Cloud Platform, read by Log Observer Connect ([logs-setup.md](logs-setup.md)) |
| Logs that say *why*, not only *that* | `event` names, `peer.service`, `error.type`, response codes, pool statistics, model versions, token counts, mandate verdicts — and `event=config.changed` whenever a feature flag flips, so "what changed?" has an answer |
| Entity-index mappings and severity | field aliases + automatic mapping policy on the connection; the collector's lowercase `severity` field |
| Tags for Tag Spotlight | business span tags indexed as APM MetricSets (`splunk/apm-metricsets.md`) — the same names as the log fields |
| Business Workflow | `checkout-api: POST /v1/payments` → `payment_authorization`; business-transaction errors are another trigger for the agent |

## 3. The acts

For each act: the symptom participants see, the alert that fires (Terraform names without the environment prefix),
what the AI Troubleshooting Agent should conclude and which log lines it should quote as evidence, three prompts
for the AI Assistant, and the debrief line for the "human vs AI" comparison.

### Act 1 — The one-in-ten-thousand decline

*Flags:* `festival_spike` ×10 + `acquirer_kbank_timeout` (BIN 457173, THB, 3.2 s hang).

* **Symptom:** approval rate drops a few points, checkout p90 jumps to ~3 s, `thai-airways` complains.
* **Alerts:** `payment-router p90 latency above 1s` (APM → agent runs), `checkout-api p90 latency above 1.5s`
  (APM), `approval rate below 85%` (KPI).
* **Expected root cause:** acquirer `acq-kbank` stops answering for cards with BIN `457173` in THB; `payment-router`
  waits for its 2.5 s timeout and fails over to `acq-uob`, whose domestic-THB approval is poor (`do_not_honor`).
* **Evidence lines:** `payment-router` `event=acquirer.timeout` ("acquirer acq-kbank timed out after 2504 ms …
  BIN 457173 THB; failing over to acq-uob") and `route.failover`; `acquirer-sim` `event=acquirer.host_unresponsive`
  (`acquirer.host=acq-kbank-auth-host-02`, `error.type=issuer_link_degraded`) and `acquirer.late_response`;
  `checkout-api` `event=payment.declined` with `route.attempt_summary=acq-kbank:timeout:timeout:2504ms,acq-uob:declined:05:…`;
  `acquirer-sim` `event=config.changed feature_flag.name=acquirer_kbank_timeout` and `merchant-storefront`
  `config.changed feature_flag.name=festival_spike` just before the symptom.
* **AI Assistant prompts:**
  1. *"Why is p90 latency high for payment-router in tuktukpay-team01 in the last 15 minutes? Use APM data and logs."*
  2. *"Which acquirer and card BIN are involved in the checkout-api declines for thai-airways in the last 15 minutes?"*
  3. *"Show me the acquirer.timeout log events for payment-router in the last 15 minutes grouped by payment.acquirer and payment.card.bin."*
* **Debrief:** the human path is MetricSet → Tag Spotlight → trace → failover span (4 clicks, if you know them);
  the agent path is open alert → read root cause → check the quoted `acquirer.timeout` line. Same answer; the
  second needs no prior Splunk knowledge.

### Act 2 — The model that drifted

*Flags:* `risk_model_drift` (20 % canary, model v3, CPU-heavy) + `festival_spike` ×3.

* **Symptom:** risk-engine p90 climbs from ~5 ms to >150 ms; approval rate drops for wallet payments in IDR/PHP.
* **Alerts:** `risk-engine p90 latency above 150ms` (APM → agent runs); on Kubernetes also the CPU-throttling
  alert (→ Remediation Plan).
* **Expected root cause:** a canary model version `v3` was enabled for 20 % of scoring; its inference is
  CPU-bound (~200 ms) and it over-declines wallet payments in IDR/PHP with reason `wallet_geo_risk_v3`.
* **Evidence lines:** `risk-engine` `event=config.changed feature_flag.name=risk_model_drift canary_pct=20
  model_version=v3 cpu_work=1200000`, then `event=risk.canary_inference` (`risk.inference_ms≈200`,
  `risk.model_version=v3`), `event=risk.inference_slow`, `event=risk.declined` with `risk.reasons=[wallet_geo_risk_v3]`
  and `payment.method=wallet`, `payment.currency=IDR|PHP`; AlwaysOn Profiling shows `_infer_v3` as the hot frame.
* **AI Assistant prompts:**
  1. *"risk-engine in tuktukpay-team01 has high latency in the last 15 minutes. What changed? Check the logs for config changes."*
  2. *"Compare the decline rate of risk model version v2 and v3 in the last 30 minutes."*
  3. *"Which payment methods and currencies are declined by risk.model_version v3? Use the risk.declined log events."*
* **Debrief:** classic-ML monitoring is a business KPI sliced by model version; the `config.changed` line is what
  turns "the model is slow" into "someone enabled the v3 canary at 10:02".

### Act 3 — The copilot bill

*Flags:* `copilot_tool_loop` (10 iterations) + `copilot_prompt_injection` (every 4th question).

* **Symptom:** copilot answers take 8× longer, token spend ×20; some questions try to trigger a refund.
* **Alerts:** `merchant-copilot p90 latency above 8s` (APM → agent runs), `copilot token burn above 20k tokens/min`
  and `copilot guardrail blocked an action` (KPIs).
* **Expected root cause:** the LLM agent re-calls `get_payment` until the iteration cap (a tool-call loop); in
  parallel, merchant questions carrying injected instructions make the model attempt `refund_payment`, which the
  action guardrail blocks.
* **Evidence lines:** `merchant-copilot` `event=copilot.tool_loop_suspected` (`copilot.iterations=10`,
  `copilot.repeated_tool_calls=10`, `gen_ai.tool.names=[get_payment,…]`, token counts), `event=copilot.max_iterations`,
  `event=guardrail.prompt_injection_suspected` (`guardrail.rules`), `event=guardrail.action_blocked
  gen_ai.tool.name=refund_payment`; `config.changed feature_flag.name=copilot_tool_loop`.
* **AI Assistant prompts:**
  1. *"Why is merchant-copilot slow in tuktukpay-team01? Look at the traces and the copilot logs for tool calls."*
  2. *"How many guardrail.action_blocked events happened in the last hour and for which merchants?"*
  3. *"Estimate the copilot cost per merchant in the last hour from tuktukpay.copilot.cost_usd."*
* **Debrief:** observability *for* AI — the loop, the tokens, the blocked tool and the payment lookups are in one
  trace and one log stream, no separate LLM product.

### Act 4 — Agents are buying

*Flags:* `agent_traffic_surge` (25 %), `agent_mandate_violations` (15 %), `catalog_prompt_injection`, `festival_spike` ×3.

* **Symptom:** more 403s from checkout-api, a new class of declines, a merchant's catalog carries instructions for
  AI agents.
* **Alerts:** `checkout-api error rate above 5%` (APM, if the 403 share is high enough → agent runs),
  `agent mandate blocks above 10% of AMP payments`, `agent-initiated decline rate above 20%` (KPIs).
* **Expected root cause:** AI shopping agents exceed, replay, forge or outlive their task mandates; checkout-api
  blocks each one at mandate verification (`mandate_over_limit`, `mandate_replay`, `mandate_invalid_signature`,
  `mandate_expired`, `mandate_merchant_not_allowed`, `mandate_kya_untrusted`); unverified agents are declined by the
  risk model as `bot_like_pattern`; one listing at `lazada-th` is a poisoned catalog entry.
* **Evidence lines:** `checkout-api` `event=mandate.blocked` (`mandate.block_reason`, `agent.id`, `amp.task_id`,
  `amp.kya.rating`, `mandate.detail`), `event=mandate.verified` for the good ones; `wallet-sim` `event=mandate.issued`;
  `merchant-sim` `event=catalog.untrusted_listing_served catalog.sku=LZ-ANC-HP-04`; `shopping-agent`
  `event=agent.untrusted_content` / `agent.blocked`; `risk-engine` `risk.declined risk.reasons=[bot_like_pattern]`.
* **AI Assistant prompts:**
  1. *"Which agent.id values have the most mandate.blocked events in tuktukpay-team01 in the last 30 minutes, and for which mandate.block_reason?"*
  2. *"Explain the trace for this payment id: <pay_…> — where was it stopped and why?"*
  3. *"Create a detector that alerts when mandate.blocked events for one agent.id exceed 20 in 5 minutes."* (the assistant drafts the SignalFlow)
* **Debrief:** governance evidence: every refusal is a span, a metric and a log line with the agent, the mandate
  and the reason — the audit trail an AI-risk framework asks for.

### Act 4b — The KYA registry goes down

*Flags:* `agent_traffic_surge` + `kya_registry_down`.

* **Symptom:** every AMP agent payment fails with 403 `mandate_kya_unavailable`; wallet-sim error rate spikes.
* **Alerts:** `wallet-sim error rate above 20%` (APM → agent runs), `checkout-api error rate above 5%` (APM),
  `Know-Your-Agent registry unavailable` (KPI).
* **Expected root cause:** the Know-Your-Agent registry behind wallet-sim is unreachable; checkout-api fails
  closed for every agent whose rating it cannot verify.
* **Evidence lines:** `wallet-sim` `event=kya.registry_unavailable` (`peer.service=kya-registry-db`,
  `error.type=RegistryUnavailable`, HTTP 503) and `config.changed feature_flag.name=kya_registry_down`;
  `checkout-api` `event=mandate.blocked mandate.block_reason=mandate_kya_unavailable peer.service=wallet-sim`.
* **AI Assistant prompts:**
  1. *"wallet-sim in tuktukpay-team01 is returning errors. What is the root cause? Use the logs."*
  2. *"Which downstream service is causing the mandate_kya_unavailable declines in checkout-api?"*
  3. *"What is the blast radius: how many payments and which merchants were affected in the last 15 minutes?"*
* **Debrief:** a dependency incident in the AI-governance path: the fix is wallet-sim's registry, not checkout-api.

### Act 5 — Ask, don't dig

*Flags:* `ledger_db_slow` (pool 2, +250 ms per INSERT) + `webhook_merchant_flaky` (lazada-th, 40 % 500s, 2.5 s slow).

* **Symptom:** checkout latency up (the ledger write is on the request path), webhooks to lazada-th retry and
  dead-letter, the webhook backlog grows.
* **Alerts:** `ledger p90 latency above 400ms` and `merchant-sim p90 latency above 1.5s` (APM → agent runs),
  `webhook dead-letters` (KPI).
* **Expected root cause:** two independent problems — the ledger's connection pool was reconfigured to 2
  connections and INSERTs are slow (queueing for connections), and lazada-th's webhook endpoint fails/slows, so
  deliveries exhaust their retries.
* **Evidence lines:** `ledger` `event=db.pool.reconfigured db.pool.max=2`, `event=db.pool.wait`
  (`db.pool.wait_ms`, `db.pool.waiting`), `event=ledger.write_slow`; `checkout-api` `event=ledger.write_slow` /
  `ledger.write_failed`; `merchant-sim` `event=merchant.webhook_error` (`peer.service=lazada-th-order-db`) and
  `merchant.webhook_slow`; `webhook-dispatcher` `event=webhook.delivery_failed`, `webhook.dead_lettered`,
  `webhook.backlog`; Database Query Performance shows the `INSERT INTO payments` statement.
* **AI Assistant prompts:**
  1. *"ledger in tuktukpay-team01 is slow. Is it the database? Check Database Query Performance and the ledger logs."*
  2. *"Why are webhooks to lazada-th being dead-lettered? Use the webhook-dispatcher and merchant-sim logs."*
  3. *"Are the ledger latency and the webhook failures related?"* (expected: no — two root causes)
* **Debrief:** run this act as a race: one half of the room may only use the AI Assistant and the Troubleshooting
  Agent, the other half anything but. Score root cause **and** evidence.

### Kubernetes: the acts that earn a Remediation Plan (k8s/ variant)

Remediation Plans only come with Kubernetes alerts, so the Kubernetes stack (`k8s/kind-up.sh`) adds incidents the cluster
itself notices. Splunk's AutoDetect detectors cover them org-wide ("K8s container restart count is > 0", "K8s pod phase
pending/failed", "K8s cluster deployment is not at spec"); `splunk/terraform/` adds environment-scoped twins with the pod and
namespace dimensions the agent keys on. The playbook per act:

**Act 6 — The leaky ledger** (`ledger_memory_leak`, ×2 traffic)
* **Symptom:** the `ledger` pod's memory climbs ~6 MiB/s to its 384 MiB limit, is **OOMKilled**, restarts, and climbs again;
  checkout-api logs `ledger.write_failed` while it is down; approvals continue (the ledger is off the critical path).
* **Alerts:** *Kubernetes container restarted* (→ agent + Remediation Plan), *container memory above 85% of its limit*.
* **Expected root cause:** a settlement cache in the ledger release does not evict; memory grows until the kernel kills the
  container. Remediation: the plan will propose raising the limit or restarting/rolling back the release — the debrief point is
  that raising the limit only buys minutes; the fix is the release.
* **Evidence lines:** ledger `event=memory.pressure` ("ledger memory 310 MiB RSS of 384 MiB container limit (81%): settlement cache
  holds 240 MiB and is not evicting", `memory.leaked_mb`, `container.memory.limit_mb`, `error.type=memory_limit_near`), then the
  restart (`k8s.container.restarts`), checkout-api `event=ledger.write_failed peer.service=ledger`, ledger `config.changed
  feature_flag.name=ledger_memory_leak`.
* **Prompts:** *"The ledger pod in namespace tuktukpay keeps restarting. Why? Use the Kubernetes data and the ledger logs."* ·
  *"Is the ledger memory growth a leak or load? Compare memory with request rate."* · *"What is the blast radius of the ledger
  restarts on payments?"*

**Act 7 — The bad rollout** (`wallet_crash_loop` + `agent_traffic_surge`)
* **Symptom:** `wallet-sim` exits with code 3 a few seconds after every start → **CrashLoopBackOff**; Know-Your-Agent lookups
  fail and every AMP agent payment is blocked `mandate_kya_unavailable`; human payments unaffected.
* **Alerts:** *Kubernetes container restarted* (→ agent + Remediation Plan), *deployment below desired replicas*, `wallet-sim
  error rate` / `checkout-api error rate` (APM) once traffic hits the dead service.
* **Expected root cause:** release `wallet-sim 1.7.0` fails its startup check (schema migration `kya_agents_v2` missing) and exits;
  Kubernetes restarts it with back-off. Remediation: roll back (`kubectl rollout undo deployment/wallet-sim`).
* **Evidence lines:** wallet-sim `event=service.crashed` ("FATAL: startup check failed after rollout wallet-sim 1.7.0: KYA registry
  schema migration kya_agents_v2 did not apply ... exiting with code 3", `error.type=SchemaMigrationError`, `deployment.release`,
  `process.exit_code=3`); checkout-api `event=mandate.blocked mandate.block_reason=mandate_kya_unavailable peer.service=wallet-sim`.
* **Prompts:** *"wallet-sim in namespace tuktukpay is in CrashLoopBackOff. What is the last thing it logged before exiting?"* ·
  *"Which payments are affected by the wallet-sim restarts and what is the decline reason?"* · *"What is the safest remediation:
  rollback or restart?"*

**Script-driven rollouts** (no chaos flag): `k8s/acts/oversize-rollout.sh` leaves a `risk-engine` pod **Pending** (Insufficient
memory) and `k8s/acts/bad-image.sh` a `merchant-copilot` pod in **ImagePullBackOff** — both keep the old pod serving, so they are
pure Kubernetes incidents: *pod pending or failed* / *deployment below desired replicas* → agent → plan (`k8s/acts/rollback.sh`).

**Act 2 on Kubernetes:** `risk-engine` has a 500m CPU limit; model v3 pins it. The *container CPU at its limit* detector fires next
to the APM `risk-engine p90` alert, and the Remediation Plan proposes the limit change — the fix is still the model.

## 4. Facilitator checklist

Before the day

- [ ] AI Assistant enabled for the org; Troubleshooting Agent automatic runs opted in (Support), or plan to press
      *Run root cause analysis* on each alert.
- [ ] Log Observer Connect connected to the stack; aliases and entity-index mappings generated **after** the first
      hour of logs; severity shows as `info`/`warn`/`error` in Log Observer.
- [ ] Terraform applied for the facilitator environment: the APM detectors must exist, because only APM service
      alerts start the agent automatically.
- [ ] Business span tags indexed (MetricSets) and the `payment_authorization` Business Workflow created.
- [ ] Dry run: start each act, wait for its APM detector, open the alert, confirm the agent's **Evidence** tab
      quotes the lines listed above. Note the run time (minutes) so the run sheet leaves room.

During an act

1. Start the act from the chaos panel; the `config.changed` lines are written at that moment.
2. Wait for the detector (`lasting` is 1–2 minutes); open the alert → Troubleshooting Agent.
3. While it runs, let teams work; then compare the agent's root cause with the room's.
4. Reset (`baseline`) before the next act so the entity index and the alerts are clean.

Known limits to say out loud: detectors on custom metrics do not trigger the agent; Remediation Plans need
Kubernetes alerts (run the facilitator stack on `kind`/EKS for Act 2); the agent takes minutes and answers vary
between runs — that is the point of keeping the log lines as the ground truth.
