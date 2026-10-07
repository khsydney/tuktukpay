# TukTukPay log schema

Every service emits the same kind of log record, in two places at once:

* **stdout** — one JSON object per line, for `docker compose logs` and Kubernetes.
* **OTLP** — the same record through the OpenTelemetry Logs SDK of each runtime, with the active span's
  `trace_id`/`span_id` and the service resource attached. The collector forwards it to Splunk Cloud Platform
  over HEC ([setup](logs-setup.md)), where Log Observer Connect, Related Content, the AI Assistant and the
  AI Troubleshooting Agent read it.

The design goal is that a reader — a person in a war room or an AI root-cause analysis — can go from an alert
to the cause with the log lines alone: every line names the payment, the merchant, the dependency that misbehaved,
how long it took and why. The "smoking gun" line of every storyline act is listed in the catalogue below.

## Record layout

| Part | Content |
|---|---|
| body / `message` | One human-readable sentence with the key identifiers inline: `acquirer acq-kbank timed out after 2504 ms for payment pay_6f7f… (merchant lazada-th, BIN 457173 THB); failing over to acq-uob` |
| severity | `DEBUG` (not exported by default), `INFO`, `WARN`, `ERROR`. The collector adds a lowercase `severity` field (`info`, `warn`, `error`…) because that is the key Log Observer Connect recognises. |
| `event` | Dotted event name, the primary search key: `payment.declined`, `acquirer.timeout`, `db.pool.wait`, `config.changed` … |
| resource | `service.name`, `deployment.environment`, `service.version`, `host.name` — set by the SDK from `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES`, the names Related Content and the Troubleshooting Agent need. |
| trace context | `trace_id`, `span_id` — from the active span; empty for background work (pollers, startup). |
| business keys | Same names as the span attributes, so Tag Spotlight and logs agree: `payment.id`, `merchant.id`, `order.id`, `payment.amount`, `payment.currency`, `payment.method`, `payment.channel`, `payment.initiator`, `payment.card.bin`, `payment.card.network`, `payment.wallet.provider`, `payment.acquirer`, `payment.outcome`, `payment.decline_reason`, `acquirer.response_code`, `route.attempts`, `route.failover`, `route.attempt_summary`, `risk.model_version`, `risk.score`, `risk.decision`, `risk.reasons`, `agent.id`, `agent.protocol`, `mandate.verdict`, `mandate.block_reason`, `amp.task_id`, `amp.kya.rating`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.tool.names`, `guardrail.verdict`, `guardrail.rules`, `webhook.attempt`, `db.pool.max`, `db.pool.waiting`, `db.pool.wait_ms` |
| operational keys | `duration_ms`, `peer.service` (the dependency involved: `risk-engine`, `payment-router`, `ledger`, `wallet-sim`, `postgres`, `redis`, an acquirer name…), `http.response.status_code`, `error.type`, `error.message`, `exception.type`, `exception.stacktrace` |
| config changes | `event=config.changed` with `feature_flag.name`, `feature_flag.enabled`, `feature_flag.params` — logged by the service that consumes a flag when the chaos panel flips it. In a real platform this is the deploy / feature-flag / config-change entry that root-cause analysis looks for first. |

PCI: no PAN, no card-holder data, ever. Cards appear as BIN (6 digits) + last4 only. Prompts and model outputs are not
logged (set `COPILOT_CAPTURE_CONTENT=true` to put them on spans for a demo, never in production).

A stdout line from checkout-api:

```json
{"ts":"2026-10-07T01:12:40.512Z","level":"WARN","service.name":"checkout-api","deployment.environment":"tuktukpay-team01",
 "message":"mandate blocked payment pay_9c1e… from agent shopbot.lazada at lazada-th: mandate_over_limit (payment 14270.00 exceeds mandate ceiling 6500.00 THB)",
 "event":"mandate.blocked","payment.id":"pay_9c1e…","merchant.id":"lazada-th","payment.amount":14270,"payment.currency":"THB",
 "payment.initiator":"agent","agent.id":"shopbot.lazada","agent.protocol":"amp/1.0","mandate.verdict":"blocked",
 "mandate.block_reason":"mandate_over_limit","amp.task_id":"task_4f…","amp.kya.rating":"A","payment.outcome":"declined",
 "http.response.status_code":403,"duration_ms":4,"trace_id":"7d8539cb3eb5c7f9924fc32b7ce43bc7","span_id":"d7812b3f7341b236"}
```

In Splunk the same record is one event: the body is `_raw`, every key above is an indexed field (`source` is the
service name, `sourcetype=otel`, `index=tuktukpay`).

## Event catalogue

Levels: `I` info, `W` warn, `E` error, `D` debug (visible only with `LOG_LEVEL=DEBUG`). "Act" marks the storyline
act a line is evidence for; **bold** events are the smoking gun of that act.

### checkout-api (Go)

| event | lvl | when | key fields | act |
|---|---|---|---|---|
| `payment.approved` | I | acquirer approved | acquirer, response code, `route.attempt_summary`, risk fields, `duration_ms` | baseline |
| `payment.declined` | I/W | acquirer declined (I) or risk model declined before authorisation (W) | `payment.decline_reason`, `risk.reasons`, `risk.model_version` | 1, 2, 4 |
| `payment.failed` | E | every acquirer failed | `route.attempt_summary`, `payment.decline_reason` | 1 (failover off) |
| **`mandate.blocked`** | W | an AMP agent's task mandate was refused | `mandate.block_reason`, `mandate.detail`, `agent.id`, `amp.task_id`, `amp.kya.rating`, `peer.service=wallet-sim` for KYA failures | **4, 4b** |
| `mandate.verified` | I | mandate accepted | `amp.task_id`, `amp.kya.rating`, `amp.mandate.use` | 4 |
| `dependency.unavailable` | E | risk-engine or payment-router did not answer (502) | `peer.service`, `error.type`, `error.message` | any outage |
| `ledger.write_failed` / `ledger.write_slow` | E / W | ledger POST failed / took > 500 ms | `peer.service=ledger`, `duration_ms` | 5 |
| `event.publish_failed` | E | Redis stream write failed | `peer.service=redis` | — |
| `payment.rejected` | W | invalid request body (400) | | — |
| `service.started` | I | startup | `peer.services` | — |

### risk-engine (Python)

| event | lvl | when | key fields | act |
|---|---|---|---|---|
| **`risk.inference_slow`** | W | inference over the 50 ms SLO (one line per inference; during Act 2 every v3 inference) | `risk.inference_ms`, `risk.inference_slo_ms`, `risk.model_version=v3`, `risk.canary_pct`, decision, reasons | **2** |
| `risk.canary_inference` | I | the canary scored a payment within the SLO | `risk.model_version`, `risk.inference_ms`, `risk.canary_pct` | 2 |
| `risk.declined` | W | decision `deny` | `risk.reasons` (e.g. `wallet_geo_risk_v3`, `bot_like_pattern`), `payment.initiator` | 2, 4 |
| `risk.review` | I | decision `review` | | — |
| `risk.scored` | D | decision `allow` | | — |
| **`config.changed`** | I | `risk_model_drift` flipped | `feature_flag.name`, `feature_flag.params` (`canary_pct`, `model_version`, `cpu_work`) | **2** |

### payment-router (Java)

| event | lvl | when | key fields | act |
|---|---|---|---|---|
| **`acquirer.timeout`** | W | an acquirer exceeded the 2.5 s timeout | `payment.acquirer`, `acquirer.timeout_ms`, `duration_ms`, `payment.card.bin`, `payment.currency`, `route.backup_acquirer`, `route.failover` | **1** |
| `route.failover` | W | retryable response (91/96/error), trying the backup | `acquirer.response_code`, `route.backup_acquirer` | 1 |
| `acquirer.error` | E | transport error talking to an acquirer | `error.type`, `error.message` | — |
| `route.authorised` / `route.declined` | I | final outcome | `payment.acquirer`, `acquirer.response_code`, `route.attempts`, `route.attempt_summary` | baseline, 1 |
| `route.failed` | E | all acquirers failed (502) | `route.attempt_summary` | 1 (failover off) |
| `config.changed` | I | `router_failover_disabled` flipped | | 1 |

### acquirer-sim (Node.js)

| event | lvl | when | key fields | act |
|---|---|---|---|---|
| **`acquirer.host_unresponsive`** | W | the KBank host hangs for one BIN range | `acquirer.name`, `payment.card.bin`, `acquirer.hang_ms`, `acquirer.host`, `error.type=issuer_link_degraded` | **1** |
| `acquirer.late_response` | W | the hung request finally answers (router already failed over) | `duration_ms`, `acquirer.late` | 1 |
| `acquirer.authorised` / `acquirer.declined` | I | per authorisation | `acquirer.response_code` (ISO 8583 style: 00, 05, 51, 91…), `acquirer.decline_reason`, `acquirer.cross_border`, `duration_ms` | baseline, 1 |
| `config.changed` | I | `acquirer_kbank_timeout` flipped | `feature_flag.params` (`bin_prefix`, `currency`, `hang_ms`) | 1 |

### ledger (Node.js, or .NET with the same events)

| event | lvl | when | key fields | act |
|---|---|---|---|---|
| **`db.pool.wait`** | W | a request waited > 100 ms for a connection | `db.pool.wait_ms`, `db.pool.max`, `db.pool.waiting`, `db.pool.total` | **5** |
| **`db.pool.reconfigured`** | W | pool resized (the `ledger_db_slow` flag) | `db.pool.max`, `db.pool.previous_max` | **5** |
| `ledger.write_slow` | W | INSERT transaction > 500 ms | `duration_ms`, `db.pool.wait_ms` | 5 |
| `ledger.write_failed` / `db.connect_failed` / `ledger.query_failed` | E | database errors | `error.type` (Postgres code), `error.message`, `peer.service=postgres` | — |
| `ledger.entry_recorded` | D | healthy write | | — |
| **`memory.pressure`** | W/E | every 5 s while `ledger_memory_leak` is on (E above 80% of the container limit) | `process.memory.rss_mb`, `memory.leaked_mb`, `container.memory.limit_mb`, `container.memory.utilization_pct`, `error.type` | **6** (Kubernetes) |
| `memory.released` | I | the leak flag was switched off | | 6 |
| `config.changed` | I | `ledger_db_slow` / `ledger_memory_leak` flipped | `sleep_ms`, `pool_size` / `mb_per_second`, `max_mb` | 5, 6 |

### webhook-dispatcher (Python)

| event | lvl | when | key fields | act |
|---|---|---|---|---|
| `webhook.delivery_failed` | W | merchant answered ≥ 300 | `http.response.status_code`, `webhook.attempt`, `webhook.max_attempts`, `merchant.id` | 5 |
| `webhook.delivery_error` | W | transport error / timeout | `error.type`, `duration_ms` | 5 |
| **`webhook.dead_lettered`** | E | retries exhausted, event parked in `payments.events.dlq` | `webhook.attempts`, `merchant.id`, `payment.id` | **5** |
| `webhook.backlog` | W | delivery > 10 s behind the payment stream (at most one line per 30 s) | `messaging.lag_ms` | 5 |
| `webhook.delivered` | I/D | delivered after a retry (I) / first try (D) | `webhook.attempt` | — |
| `redis.unavailable` / `redis.read_failed` / `webhook.processing_failed` | W/E | infrastructure errors | `peer.service=redis` | — |

### merchant-sim (Python)

| event | lvl | when | key fields | act |
|---|---|---|---|---|
| **`merchant.webhook_error`** | E | the merchant's order service "crashes" (500) | `merchant.id`, `peer.service=<merchant>-order-db`, `error.type` | **5** |
| `merchant.webhook_slow` | W | the merchant holds the webhook for `slow_ms` | `duration_ms` | 5 |
| **`catalog.untrusted_listing_served`** | W | a listing whose description addresses AI agents was served | `catalog.sku`, `security.prompt_injection.suspected` | **4** |
| `merchant.webhook_received` | D | healthy delivery | | — |
| `config.changed` | I | `webhook_merchant_flaky` / `catalog_prompt_injection` flipped | | 4, 5 |

### merchant-copilot (Python, LLM agent)

| event | lvl | when | key fields | act |
|---|---|---|---|---|
| `copilot.answered` | I | per question | `gen_ai.request.model`, `copilot.iterations`, `gen_ai.tool.names`, `gen_ai.usage.*`, `copilot.cost_usd`, `duration_ms`, `guardrail.verdict` | baseline, 3 |
| **`copilot.tool_loop_suspected`** | W | ≥ 5 iterations for one question | `copilot.repeated_tool_calls`, `gen_ai.tool.names` | **3** |
| `copilot.max_iterations` | E | the agent gave up | | 3 |
| **`guardrail.prompt_injection_suspected`** | W | the question matched injection patterns | `guardrail.rules`, `security.prompt_injection.suspected` | **3** |
| **`guardrail.action_blocked`** | W | the model tried a blocked tool (`refund_payment`) | `gen_ai.tool.name`, `guardrail.rules` | **3** |
| `copilot.tool_error` / `copilot.llm_error` | W/E | tool or LLM failure | `error.type`, `peer.service` | — |
| `config.changed` | I | `copilot_tool_loop` flipped | `iterations` | 3 |

### shopping-agent and wallet-sim (Python)

| event | lvl | when | key fields | act |
|---|---|---|---|---|
| `agent.step` / `agent.attack_simulated` / `agent.blocked` / `agent.untrusted_content` | I/W | each step of a console run | `agent.task_id`, `agent.id`, `agent.attack_simulation` | 4 |
| `agent.task_failed` | E | a console run crashed | `exception.*` | — |
| `mandate.issued` | I | the wallet issued a task mandate | `amp.task_id`, `agent.id`, `amp.kya.rating`, `amp.mandate.max_amount`, `amp.mandate.merchants`, `amp.mandate.ttl_s` | 4 |
| `mandate.refused` | W | unregistered agent asked for a mandate | `agent.id` | 4 |
| **`kya.registry_unavailable`** | E | Know-Your-Agent lookups fail (503) | `agent.id`, `peer.service=kya-registry-db`, `error.type=RegistryUnavailable` | **4b** |
| `kya.agent_unknown` | W | lookup for an agent nobody registered | | 4 |
| **`service.crashed`** | CRITICAL | `wallet_crash_loop` on: the process exits a few seconds after start (CrashLoopBackOff on Kubernetes) | `error.type=SchemaMigrationError`, `deployment.release`, `process.exit_code` | **7** (Kubernetes) |
| `config.changed` | I | `kya_registry_down` / `wallet_crash_loop` flipped | | 4b, 7 |

### merchant-storefront (load generator)

| event | lvl | when | key fields |
|---|---|---|---|
| `storefront.stats` | I | every 30 s | `storefront.rps`, `storefront.traffic_multiplier`, `storefront.approval_rate_pct`, `storefront.errors`, `storefront.campaign` |
| `storefront.checkout_error` / `storefront.copilot_error` | W | 5xx or transport error from the platform | `http.response.status_code`, `payment.decline_reason`, `peer.service` |
| `storefront.paused` | I | traffic paused from the war-room panel | |
| `config.changed` | I | `festival_spike`, `agent_traffic_surge`, `agent_mandate_violations`, `copilot_prompt_injection`, `traffic_paused` flipped | `feature_flag.params` (`multiplier`, `agent_share`, `rate`…) |

## Search cheat sheet (Splunk Cloud Platform / Log Observer Connect)

```
index=tuktukpay deployment.environment=tuktukpay-team01 severity IN (warn, error) | stats count by source, event
index=tuktukpay event=acquirer.timeout | stats count by payment.acquirer, payment.card.bin, route.backup_acquirer          # Act 1
index=tuktukpay event=config.changed | table _time source feature_flag.name feature_flag.enabled feature_flag.params        # what changed, when
index=tuktukpay event=risk.canary_inference | timechart avg(risk.inference_ms) by risk.model_version                       # Act 2
index=tuktukpay event IN (copilot.tool_loop_suspected, guardrail.action_blocked, guardrail.prompt_injection_suspected)      # Act 3
index=tuktukpay event=mandate.blocked | stats count by mandate.block_reason, agent.id                                       # Act 4
index=tuktukpay event IN (kya.registry_unavailable, mandate.blocked) mandate.block_reason=mandate_kya_unavailable            # Act 4b
index=tuktukpay event IN (db.pool.wait, ledger.write_slow, merchant.webhook_error, webhook.dead_lettered)                    # Act 5
index=tuktukpay trace_id=<id from APM>                                                                                      # one trace, all services
```

## How each runtime emits logs

| Runtime | Mechanism | Where |
|---|---|---|
| Python (risk-engine, webhook-dispatcher, merchant-sim, merchant-copilot, shopping-agent, wallet-sim, loadgen) | `tuktuk_logging.py` (copied into each service): a JSON stdout formatter that adds trace ids from the active span, `log_event(logger, level, event, message, **fields)` which passes the fields as `extra=` so the Splunk Python distro's OTLP `LoggingHandler` exports them as attributes. The distro enables logs export by default (`OTEL_LOGS_EXPORTER=otlp`); the SDK's own diagnostics (`opentelemetry.*` loggers) are filtered out of the export. | `services/*/tuktuk_logging.py` |
| Go (checkout-api) | `logging.go`: a `slog` fan-out handler — `JSONHandler` on stdout (with `trace_id`/`span_id` copied from the context) plus the `otelslog` bridge into an OTLP `LoggerProvider` created in `otel.go`. Business fields reuse the span attributes (`kvToSlog`). | `services/checkout-api/logging.go`, `otel.go` |
| Java (payment-router) | `Log.java`: writes the JSON line and emits the record through the OpenTelemetry **Logs API** (`GlobalOpenTelemetry.get().getLogsBridge()`); the Splunk Java agent bridges it to its SDK and exports it with the span context. The only compile-time dependency is `opentelemetry-api` (fetched in the Dockerfile). `OTEL_LOGS_EXPORTER=otlp` also exports the few `java.util.logging` lines. | `services/payment-router/src/main/java/com/tuktukpay/router/Log.java` |
| Node.js (acquirer-sim, ledger) | `log.js`: pino to stdout. `@splunk/otel` injects `trace_id`/`span_id` into every pino record and, with `SPLUNK_AUTOMATIC_LOG_COLLECTION=true`, exports the records over OTLP/HTTP — hence `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://otel-collector:4318/v1/logs`. | `services/acquirer-sim/log.js`, `services/ledger/log.js` |
| .NET (ledger-dotnet) | `ILogger` message templates whose placeholders are the field names (`{payment.id}`), JSON console output with `TraceId`/`SpanId` scopes; the Splunk .NET auto-instrumentation exports every `ILogger` record over OTLP (`OTEL_DOTNET_AUTO_LOGS_ENABLED`, on by default). | `services/ledger-dotnet/Program.cs` |

`LOG_LEVEL` (per service, default `INFO`) gates both outputs; `DEBUG` adds the per-request healthy events.

## What the collector does with a log record

`otel/collector-config.yaml`, pipeline `logs`:

1. `memory_limiter`, `resourcedetection`, `resource/add_environment` — same as traces; `deployment.environment` is upserted so every record carries the team's environment.
2. `transform/logs` — copies `service.name` into `com.splunk.source` so the Splunk `source` field is the service name (while `service.name` stays an indexed field for Related Content), and derives the lowercase `severity` field Log Observer Connect understands from the OTel severity text (`WARNING`→`warn`, `SEVERE`→`error`, `Information`→`info`…) or, when an SDK sets only the severity number (Go's `otelslog` bridge), from the number (9–12 → `info`, 13–16 → `warn`, 17–20 → `error`).
3. `splunk_hec` — one HEC event per record: body → `event`, `trace_id`, `span_id`, `otel.log.severity.text` and **every resource and log attribute** → indexed `fields`; `host.name` → `host`; `index` from `SPLUNK_HEC_INDEX`. A failing endpoint gives up after two minutes and keeps the queue small, so logs can never back up into the memory limiter and cost you traces.
