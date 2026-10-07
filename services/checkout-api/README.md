# checkout-api

TukTukPay's public payments API, written in Go. Every payment, whether it comes from the load generator or from an AI
shopping agent, enters the platform here. The service uses the **upstream OpenTelemetry Go SDK with manual
instrumentation**, so you can see exactly which lines create spans, attributes and metrics. All the other services use
zero-code agents.

| | |
|---|---|
| Language | Go 1.24 (stdlib `net/http`, no framework) |
| Port | `8080` |
| Instrumentation | OTel SDK, manual: `otelhttp` server/client spans, `redisotel`, custom spans + metrics |
| Calls | `risk-engine` → `payment-router` → `ledger`, `wallet-sim` (KYA), Redis stream `payments.events` |
| Image | distroless static, non-root |

## Files

| File | What it does |
|---|---|
| [`main.go`](main.go) | HTTP routes, the payment flow, business span attributes, metrics, one log line per payment outcome |
| [`amp.go`](amp.go) | Checks the AMP task mandate for agent-initiated payments (signature, expiry, limits, KYA, replay) |
| [`otel.go`](otel.go) | OTel SDK setup: OTLP/gRPC exporters for traces, metrics and logs, resource, propagators, and a fix for Trace Context Level 2 headers |
| [`logging.go`](logging.go) | `slog` fan-out: JSON on stdout with `trace_id`/`span_id` + the `otelslog` bridge that exports every record over OTLP |

## API

### `POST /v1/payments`

```bash
curl -s -X POST localhost:8080/v1/payments \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: ord_123' \
  -d '{
    "merchant_id": "lazada-th",
    "amount": 1290,
    "currency": "THB",
    "payment_method": "card",
    "card": {"bin": "411111", "last4": "1111", "network": "visa"},
    "customer": {"id": "cus_00001", "country": "TH"},
    "order_id": "ord_123",
    "channel": "web"
  }'
```

```json
{
  "payment_id": "pay_e9d7b8fd4c6b",
  "status": "approved",
  "acquirer": "acq-kbank",
  "auth_code": "49E42B",
  "risk": {"score": 0.0795, "decision": "allow", "model_version": "v2", "reasons": [], "latency_ms": 0.24},
  "initiator": "human",
  "latency_ms": 141
}
```

Request fields: `merchant_id`, `amount` and `currency` are required. `payment_method` is `card`, `wallet`,
`bank_transfer` or `qr`, and comes with an optional `card` or `wallet` object. The remaining fields are `customer`,
`order_id`, `channel` (`web`, `app` or `api`) and `metadata`.

| HTTP | `status` | When |
|---|---|---|
| 200 | `approved` / `declined` | Normal outcome (`decline_reason` is set on declines, e.g. `risk_declined`) |
| 400 | — | Invalid body or missing required field |
| 403 | `declined` | Agent payment blocked by its mandate (`decline_reason: mandate_*`, plus `mandate_detail`) |
| 502 | `error` | `risk_engine_unavailable` or `router_unavailable` |

### `GET /v1/payments/{id}`

Proxies to the ledger and returns the stored payment record.

### `GET /healthz`

Liveness probe. Returns `{"status":"ok"}`. This route is not traced.

## How a payment is processed

1. **Validate and classify.** If the request has an `X-Agent-Protocol` header, or a User-Agent containing "agent",
   the payment is `payment.initiator=agent`. Otherwise it is `human`.
2. **Mandate check (AMP agents only).** This runs in the `amp.verify_mandate` span. The checks run in this order:
   token present → HMAC signature → agent id matches → not expired → merchant allowed → currency matches → amount
   ≤ ceiling → KYA registry lookup on `wallet-sim` (fails closed) → single-use replay guard (Redis `INCR`). The first
   check that fails sets the reason and returns 403.
3. **Risk scoring.** `POST risk-engine/v1/score`. A `deny` decision gives `declined` / `risk_declined`.
4. **Routing and authorisation.** `POST payment-router/v1/route`. The router picks the acquirer and fails over to a
   backup when the primary is slow.
5. **Persist.** `POST ledger/v1/entries`. A ledger failure is recorded as a span event and logged, and the payment
   result is still returned. The ledger is off the critical path.
6. **Publish.** `XADD payments.events` in a `PRODUCER` span. The `traceparent` is copied into the stream message, so
   the `webhook-dispatcher` worker joins the same trace.

### Agent mandate tokens

Agents send `X-Agent-Protocol: amp/1.0`, `X-Agent-Id: <agent>` and `X-AMP-Task-Token: amp1.<payload>.<sig>`. The
token is a base64url JSON payload signed with HMAC-SHA256 using `AMP_SHARED_SECRET`. The wallet issues it; see
[`../agent-console/amp.py`](../agent-console/amp.py). Possible block reasons:

`mandate_missing`, `mandate_malformed`, `mandate_invalid_signature`, `mandate_agent_mismatch`, `mandate_expired`,
`mandate_merchant_not_allowed`, `mandate_currency_mismatch`, `mandate_over_limit`, `mandate_kya_unavailable`,
`mandate_kya_untrusted`, `mandate_replay_check_failed`, `mandate_replay`.

## Telemetry

**Spans**

- `POST /v1/payments` and `GET /v1/payments/{id}`: SERVER spans from `otelhttp`, with low-cardinality route names.
- `POST <host>/v1/...`: CLIENT spans for each downstream call. `/v1/payments/<id>` paths are collapsed to `{id}`.
- `amp.verify_mandate`: the mandate decision. A blocked mandate adds a `mandate.blocked` event.
- `publish payments.events`: PRODUCER span with `messaging.system=redis`.
- Redis commands, via `redisotel`.

**Span attributes**, which you can slice on in Tag Spotlight: `payment.id`, `merchant.id`, `payment.amount`,
`payment.currency`, `payment.method`, `payment.channel`, `payment.initiator`, `payment.card.bin`,
`payment.card.network`, `payment.wallet.provider`, `customer.country`, `order.id`, `payment.idempotency_key`,
`risk.score`, `risk.decision`, `risk.model_version`, `risk.reasons`, `payment.acquirer`, `route.attempts`,
`route.failover`, `payment.outcome`, `payment.decline_reason`, `agent.protocol`, `agent.id`, `agent.verified`,
`mandate.verdict`, `mandate.block_reason`, `amp.task_id`, `amp.mandate.*`, `amp.kya.rating`, `kya.*`.

**Logs** (one structured record per payment outcome, [schema](../../docs/log-schema.md)): `event=payment.approved |
payment.declined | payment.failed | mandate.blocked | mandate.verified | dependency.unavailable | ledger.write_failed |
ledger.write_slow | event.publish_failed`, each with the span's business attributes, `payment.decline_reason`,
`route.attempt_summary` (e.g. `acq-kbank:timeout:timeout:2504ms,acq-uob:declined:05:98ms`), `peer.service`,
`error.type`, `duration_ms` and the trace context. Written to stdout as JSON and exported over OTLP by the
`otelslog` bridge (`LOG_LEVEL` gates both; `debug` adds `mandate.check_failed` details).

**Metrics** (exported every 10 s)

| Metric | Type | Dimensions |
|---|---|---|
| `tuktukpay.payments.count` | counter | merchant, currency, method, initiator, acquirer, outcome |
| `tuktukpay.payments.amount` | histogram | merchant, currency, outcome |
| `tuktukpay.payments.declines` | counter | the `count` dimensions + `payment.decline_reason` |
| `tuktukpay.agent_mandates` | counter | `mandate.verdict`, `mandate.block_reason`, `amp.kya.rating` |

**SDK setup** ([`otel.go`](otel.go)): OTLP/gRPC exporters configured from the standard `OTEL_EXPORTER_OTLP_*`
variables, `ParentBased(AlwaysSample)`, and W3C Trace Context + Baggage propagation. The service has no
Splunk-specific code, so the same binary can export to any OTLP backend. `tolerantTraceContext` reduces the trace
flags of an incoming `traceparent` (for example `-03` from newer Python SDKs) to just the sampled bit. Without it,
OTel Go older than v1.42 rejects the header and starts a new trace, which breaks the agent → checkout trace link.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | Listen port |
| `RISK_URL` | `http://localhost:8081` | risk-engine |
| `ROUTER_URL` | `http://localhost:8082` | payment-router |
| `LEDGER_URL` | `http://localhost:8084` | ledger |
| `WALLET_URL` | `http://localhost:8088` | wallet-sim (KYA registry) |
| `REDIS_ADDR` | `localhost:6379` | Redis for the event stream and replay guard |
| `EVENT_STREAM` | `payments.events` | Redis stream name |
| `AMP_SHARED_SECRET` | `tuktukpay-amp-demo-secret` | HMAC key for mandate tokens (must match the wallet) |
| `OTEL_SERVICE_NAME` | `checkout-api` | Service name |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Collector endpoint (set by Compose/K8s) |
| `OTEL_RESOURCE_ATTRIBUTES` | — | e.g. `deployment.environment=...` |

## Running

The service normally runs as part of the full stack. See the [top-level README](../../README.md).

```bash
docker compose up -d --build checkout-api      # rebuild just this service
docker compose logs -f checkout-api            # JSON logs (slog)
```

To run it standalone, the other services must be reachable at the URLs above:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 OTEL_EXPORTER_OTLP_INSECURE=true go run .
```
