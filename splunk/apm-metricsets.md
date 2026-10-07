# Splunk APM MetricSets for TukTukPay (what to index, and why)

Splunk APM keeps **every span attribute on every trace** (Trace Analyzer can filter on any of them). To make a tag
usable in **Tag Spotlight**, breakdowns and dashboards, index it as a MetricSet:

*Settings → APM MetricSets → Troubleshooting MetricSets → Add new* (per environment). Indexing starts within minutes.

## Recommended Troubleshooting MetricSets (Tag Spotlight)

| Span tag | Set by | Used in |
|---|---|---|
| `merchant.id` | all services | every act |
| `payment.acquirer` | checkout-api, payment-router (via collector transform) | Act 1 |
| `payment.card.bin` | checkout-api, acquirer-sim | Act 1 (needle in the haystack) |
| `payment.method`, `payment.currency` | checkout-api, risk-engine | Acts 1, 2 |
| `payment.initiator`, `agent.protocol`, `agent.id`, `agent.verified` | checkout-api, risk-engine | Act 4 |
| `mandate.verdict`, `mandate.block_reason`, `amp.kya.rating`, `amp.wallet` | checkout-api (amp.verify_mandate), wallet-sim | Act 4 governance |
| `gen_ai.agent.name`, `amp.outcome`, `agent.attack_simulation`, `security.prompt_injection.suspected` | shopping-agent | Act 4 console |
| `payment.decline_reason`, `payment.outcome` | checkout-api, payment-router | Acts 1, 4 |
| `risk.model_version`, `risk.decision` | risk-engine, checkout-api | Act 2 |
| `route.failover`, `route.attempts` | payment-router (collector transform) | Act 1 |
| `gen_ai.request.model`, `gen_ai.operation.name`, `gen_ai.tool.name`, `guardrail.verdict` | merchant-copilot | Act 3 |
| `webhook.attempt`, `merchant.webhook_outcome` | webhook-dispatcher, merchant-sim | Act 5 |
| `db.pool.max` | ledger | Act 5 |

Cardinality guidance: `payment.card.bin` has ~25 values in the simulator; in production index the **BIN prefix**
(6–8 digits) rather than full PANs (never put PANs on spans). `payment.id` and `order.id` are *not* indexed — they are
searchable in Trace Analyzer, which is the right tool for "find this one payment".

## Monitoring MetricSets (dashboards, detectors, SLOs)

Create Monitoring MetricSets for `merchant.id` and `payment.acquirer` on `checkout-api` if you want detectors per merchant /
acquirer built on APM RED metrics (`service.request.count` etc. with the extra dimension). Otherwise use the
`tuktukpay.*` business metrics, which already carry those dimensions.

## Business Workflows

*Settings → APM Business Workflows*: initiating span `checkout-api: POST /v1/payments` → workflow name `payment_authorization`.
This gives an end-to-end workflow latency/error metric that includes the async webhook delivery, and it is one of the alert types
the AI Troubleshooting Agent can work from.
