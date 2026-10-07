# TukTukPay "11.11 War Room" — dashboard + detectors as code for Splunk Observability Cloud.
#
#   cd splunk/terraform
#   export SFX_AUTH_TOKEN=<API token (Settings > Access Tokens, API scope)>
#   terraform init && terraform apply -var realm=us1 -var environment=tuktukpay-facilitator
#
# The metrics referenced here are emitted by the services (tuktukpay.* counters) and by
# Splunk APM Monitoring MetricSets (service.request.*). Nothing here depends on histograms
# so it works regardless of how your org ingests OTLP histograms.

terraform {
  required_version = ">= 1.5"
  required_providers {
    signalfx = {
      source  = "splunk-terraform/signalfx"
      version = ">= 9.0"
    }
  }
}

variable "realm" {
  description = "Splunk Observability Cloud realm (us1, eu0, au0, jp0, sg0 ...)"
  type        = string
}

variable "environment" {
  description = "deployment.environment of the stack to chart (one per team)"
  type        = string
  default     = "tuktukpay-team01"
}

variable "k8s_namespace" {
  description = "Kubernetes namespace of the stack (k8s/ variant); the Kubernetes detectors filter on it. Empty on Docker Compose."
  type        = string
  default     = "tuktukpay"
}

variable "notifications" {
  description = "Detector notifications, e.g. [\"Email,you@example.com\"] or [\"Slack,<credential id>,#channel\"]"
  type        = list(string)
  default     = []
}

provider "signalfx" {
  auth_token = var.sfx_auth_token # null -> the provider reads SFX_AUTH_TOKEN from the environment
  api_url    = "https://api.${var.realm}.observability.splunkcloud.com"
}

variable "sfx_auth_token" {
  description = "API token; or set SFX_AUTH_TOKEN in the environment"
  type        = string
  default     = null
  sensitive   = true
}

locals {
  env_filter = "filter('deployment.environment', '${var.environment}')"
  apm_filter = "filter('sf_environment', '${var.environment}')"
}

# ------------------------------------------------------------------ charts

resource "signalfx_time_chart" "throughput" {
  name         = "Payments per second by outcome"
  description  = "tuktukpay.payments.count from checkout-api"
  plot_type    = "AreaChart"
  stacked      = true
  unit_prefix  = "Metric"
  program_text = <<-EOF
    data('tuktukpay.payments.count', filter=${local.env_filter}, rollup='rate').sum(by=['payment.outcome']).publish(label='per second')
  EOF
  viz_options {
    label = "per second"
  }
}

resource "signalfx_time_chart" "approval_rate_merchant" {
  name         = "Approval rate by merchant (%)"
  plot_type    = "LineChart"
  program_text = <<-EOF
    A = data('tuktukpay.payments.count', filter=${local.env_filter} and filter('payment.outcome', 'approved'), rollup='delta').sum(by=['merchant.id'])
    B = data('tuktukpay.payments.count', filter=${local.env_filter}, rollup='delta').sum(by=['merchant.id'])
    (A / B * 100).publish(label='approval %')
  EOF
  axis_left {
    min_value = 0
    max_value = 100
  }
}

resource "signalfx_time_chart" "declines_by_reason" {
  name         = "Declines by reason"
  plot_type    = "ColumnChart"
  stacked      = true
  program_text = <<-EOF
    data('tuktukpay.payments.declines', filter=${local.env_filter}, rollup='delta').sum(by=['payment.decline_reason']).publish(label='declines')
  EOF
}

resource "signalfx_time_chart" "approval_rate_model" {
  name         = "Approval rate by risk model version (%) — Act 2"
  plot_type    = "LineChart"
  program_text = <<-EOF
    A = data('tuktukpay.risk.decisions', filter=${local.env_filter} and not filter('risk.decision', 'deny'), rollup='delta').sum(by=['risk.model_version'])
    B = data('tuktukpay.risk.decisions', filter=${local.env_filter}, rollup='delta').sum(by=['risk.model_version'])
    (A / B * 100).publish(label='allow+review %')
  EOF
  axis_left {
    min_value = 0
    max_value = 100
  }
}

resource "signalfx_time_chart" "decline_rate_initiator" {
  name         = "Decline rate: agent vs human (%) — Act 4"
  plot_type    = "LineChart"
  program_text = <<-EOF
    D = data('tuktukpay.payments.declines', filter=${local.env_filter}, rollup='delta').sum(by=['payment.initiator'])
    T = data('tuktukpay.payments.count', filter=${local.env_filter}, rollup='delta').sum(by=['payment.initiator'])
    (D / T * 100).publish(label='decline %')
  EOF
}

resource "signalfx_time_chart" "acquirer_share" {
  name         = "Payments by acquirer — Act 1 (watch failover)"
  plot_type    = "AreaChart"
  stacked      = true
  program_text = <<-EOF
    data('tuktukpay.payments.count', filter=${local.env_filter}, rollup='rate').sum(by=['payment.acquirer']).publish(label='per second')
  EOF
}

resource "signalfx_time_chart" "checkout_latency" {
  name         = "checkout-api latency p90 (ms) — APM"
  plot_type    = "LineChart"
  program_text = <<-EOF
    (data('service.request.duration.ns.p90', filter=${local.apm_filter} and filter('sf_service', 'checkout-api') and filter('sf_error', 'false')).mean() / 1000000).publish(label='p90 ms')
    (data('service.request.duration.ns.median', filter=${local.apm_filter} and filter('sf_service', 'checkout-api') and filter('sf_error', 'false')).mean() / 1000000).publish(label='p50 ms')
  EOF
}

resource "signalfx_time_chart" "service_errors" {
  name         = "Error rate by service (%) — APM"
  plot_type    = "LineChart"
  program_text = <<-EOF
    E = data('service.request.count', filter=${local.apm_filter} and filter('sf_error', 'true'), rollup='delta').sum(by=['sf_service'])
    T = data('service.request.count', filter=${local.apm_filter}, rollup='delta').sum(by=['sf_service'])
    (E / T * 100).publish(label='error %')
  EOF
}

resource "signalfx_time_chart" "agent_mandates" {
  name         = "Agent task-mandate verdicts — Act 4 governance"
  plot_type    = "ColumnChart"
  stacked      = true
  program_text = <<-EOF
    data('tuktukpay.agent_mandates', filter=${local.env_filter}, rollup='delta').sum(by=['mandate.verdict', 'mandate.block_reason']).publish(label='mandates')
  EOF
}

resource "signalfx_time_chart" "agent_tasks" {
  name         = "Shopping-agent tasks by outcome — Act 4 console"
  plot_type    = "ColumnChart"
  stacked      = true
  program_text = <<-EOF
    data('tuktukpay.agent_tasks', filter=${local.env_filter}, rollup='delta').sum(by=['outcome']).publish(label='tasks')
  EOF
}

resource "signalfx_time_chart" "copilot_tokens" {
  name         = "Copilot tokens per minute by type — Act 3"
  plot_type    = "AreaChart"
  stacked      = true
  program_text = <<-EOF
    data('tuktukpay.copilot.tokens', filter=${local.env_filter}, rollup='rate').sum(by=['gen_ai.token.type']).scale(60).publish(label='tokens/min')
  EOF
}

resource "signalfx_time_chart" "copilot_cost" {
  name         = "Copilot estimated cost per hour by merchant (USD) — Tokenomics"
  plot_type    = "ColumnChart"
  stacked      = true
  program_text = <<-EOF
    data('tuktukpay.copilot.cost_usd', filter=${local.env_filter}, rollup='rate').sum(by=['merchant.id']).scale(3600).publish(label='USD/hour')
  EOF
}

resource "signalfx_time_chart" "copilot_guardrail" {
  name         = "Copilot guardrail verdicts and tool calls"
  plot_type    = "ColumnChart"
  stacked      = true
  program_text = <<-EOF
    data('tuktukpay.copilot.guardrail_events', filter=${local.env_filter} and filter('guardrail.stage', 'action'), rollup='delta').sum(by=['guardrail.verdict']).publish(label='guardrail')
    data('tuktukpay.copilot.tool_calls', filter=${local.env_filter}, rollup='delta').sum(by=['gen_ai.tool.name']).publish(label='tool calls')
  EOF
}

resource "signalfx_time_chart" "webhooks" {
  name         = "Webhook deliveries by outcome — Act 5"
  plot_type    = "ColumnChart"
  stacked      = true
  program_text = <<-EOF
    data('tuktukpay.webhooks.deliveries', filter=${local.env_filter}, rollup='delta').sum(by=['outcome']).publish(label='deliveries')
  EOF
}

resource "signalfx_single_value_chart" "approval_now" {
  name         = "Approval rate (last 5 min)"
  program_text = <<-EOF
    A = data('tuktukpay.payments.count', filter=${local.env_filter} and filter('payment.outcome', 'approved'), rollup='delta').sum().sum(over='5m')
    B = data('tuktukpay.payments.count', filter=${local.env_filter}, rollup='delta').sum().sum(over='5m')
    (A / B * 100).publish(label='approval %')
  EOF
  unit_prefix  = "Metric"
  color_by     = "Dimension"
}

resource "signalfx_single_value_chart" "agent_share" {
  name         = "Agent-initiated share (last 5 min)"
  program_text = <<-EOF
    A = data('tuktukpay.payments.count', filter=${local.env_filter} and filter('payment.initiator', 'agent'), rollup='delta').sum().sum(over='5m')
    B = data('tuktukpay.payments.count', filter=${local.env_filter}, rollup='delta').sum().sum(over='5m')
    (A / B * 100).publish(label='agent %')
  EOF
}

# ------------------------------------------------------------------ dashboard

resource "signalfx_dashboard_group" "tuktukpay" {
  name        = "TukTukPay 11.11 War Room"
  description = "Workshop dashboards for the TukTukPay payment platform (environment ${var.environment})"
}

resource "signalfx_dashboard" "warroom" {
  name            = "War Room — ${var.environment}"
  dashboard_group = signalfx_dashboard_group.tuktukpay.id
  time_range      = "-1h"

  chart {
    chart_id = signalfx_single_value_chart.approval_now.id
    row      = 0
    column   = 0
    width    = 3
    height   = 1
  }
  chart {
    chart_id = signalfx_single_value_chart.agent_share.id
    row      = 0
    column   = 3
    width    = 3
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.throughput.id
    row      = 0
    column   = 6
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.approval_rate_merchant.id
    row      = 1
    column   = 0
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.declines_by_reason.id
    row      = 1
    column   = 6
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.acquirer_share.id
    row      = 2
    column   = 0
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.checkout_latency.id
    row      = 2
    column   = 6
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.approval_rate_model.id
    row      = 3
    column   = 0
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.decline_rate_initiator.id
    row      = 3
    column   = 6
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.copilot_tokens.id
    row      = 4
    column   = 0
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.copilot_cost.id
    row      = 4
    column   = 4
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.copilot_guardrail.id
    row      = 4
    column   = 8
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.agent_mandates.id
    row      = 5
    column   = 0
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.agent_tasks.id
    row      = 5
    column   = 6
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.service_errors.id
    row      = 6
    column   = 0
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.webhooks.id
    row      = 6
    column   = 6
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.k8s_restarts.id
    row      = 7
    column   = 0
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.k8s_memory_pct.id
    row      = 7
    column   = 4
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.k8s_cpu_pct.id
    row      = 7
    column   = 8
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_text_chart.logs_howto.id
    row      = 8
    column   = 0
    width    = 12
    height   = 1
  }
}

# ---- Kubernetes row (k8s/ variant): what Acts 6 and 7 look like from the cluster ----

resource "signalfx_time_chart" "k8s_restarts" {
  name         = "Container restarts — Acts 6 & 7 (Kubernetes)"
  description  = "k8s.container.restarts delta per container in the stack's namespace"
  program_text = <<-EOF
    data('k8s.container.restarts', filter=filter('k8s.namespace.name', '${var.k8s_namespace}')).delta().sum(by=['k8s.container.name']).publish(label='restarts')
  EOF
  plot_type         = "ColumnChart"
  axis_left { min_value = 0 }
}

resource "signalfx_time_chart" "k8s_memory_pct" {
  name         = "Container memory vs limit (%) — Act 6 (Kubernetes)"
  description  = "container.memory.usage / k8s.container.memory_limit; the ledger climbs to 100% and is OOMKilled"
  program_text = <<-EOF
    usage = data('container.memory.usage', filter=filter('k8s.namespace.name', '${var.k8s_namespace}')).sum(by=['k8s.container.name'])
    limit = data('k8s.container.memory_limit', filter=filter('k8s.namespace.name', '${var.k8s_namespace}')).sum(by=['k8s.container.name'])
    (usage / limit * 100).publish(label='% of limit')
  EOF
  plot_type         = "LineChart"
  axis_left { min_value = 0 max_value = 110 }
}

resource "signalfx_time_chart" "k8s_cpu_pct" {
  name         = "Container CPU vs limit (%) — Act 2 on Kubernetes"
  description  = "container.cpu.utilization / k8s.container.cpu_limit; risk-engine pins its 500m limit under model v3"
  program_text = <<-EOF
    cpu   = data('container.cpu.utilization', filter=filter('k8s.namespace.name', '${var.k8s_namespace}')).sum(by=['k8s.container.name'])
    limit = data('k8s.container.cpu_limit', filter=filter('k8s.namespace.name', '${var.k8s_namespace}')).sum(by=['k8s.container.name'])
    (cpu / limit * 100).publish(label='% of limit')
  EOF
  plot_type         = "LineChart"
  axis_left { min_value = 0 }
}

# Logs live in Splunk Cloud Platform (Log Observer Connect); this panel carries the
# searches the war room uses, so nobody has to remember the field names (docs/log-schema.md).
resource "signalfx_text_chart" "logs_howto" {
  name     = "Logs — where to look (Log Observer Connect)"
  markdown = <<-EOF
    **Logs for this environment** (Log Observer → Splunk platform index `tuktukpay`): every service logs one JSON event per payment outcome
    with `trace_id`, `span_id`, `service.name`, `deployment.environment=${var.environment}` and the business keys used in Tag Spotlight.

    * Act 1 — `event=acquirer.timeout` (payment-router) and `event=acquirer.host_unresponsive` (acquirer-sim): which acquirer, which BIN, failover target.
    * Act 2 — `event=risk.canary_inference` / `risk.inference_slow` and `config.changed feature_flag.name=risk_model_drift` (risk-engine).
    * Act 3 — `event=copilot.tool_loop_suspected`, `guardrail.prompt_injection_suspected`, `guardrail.action_blocked` (merchant-copilot).
    * Act 4 — `event=mandate.blocked mandate.block_reason=*` (checkout-api), `mandate.issued` (wallet-sim), `catalog.untrusted_listing_served` (merchant-sim).
    * Act 4b — `event=kya.registry_unavailable` (wallet-sim) → `mandate.block_reason=mandate_kya_unavailable` (checkout-api).
    * Act 5 — `event=db.pool.wait` / `ledger.write_slow` (ledger), `merchant.webhook_error` (merchant-sim), `webhook.dead_lettered` (webhook-dispatcher).

    From any span: **Related Content → Logs for this trace**. From an alert: **AI Troubleshooting Agent → Evidence** shows the matching log lines.
  EOF
}

# ------------------------------------------------------------------ detectors
#
# Two families. Detectors on Splunk APM Monitoring MetricSets (service.request.*) are
# "APM service alerts": the AI Troubleshooting Agent runs on them automatically and the
# AI Assistant can explain them. Detectors on the tuktukpay.* business metrics are the
# war-room KPIs; the agent does not run on custom metrics, so each act below has an APM
# detector as well.

resource "signalfx_detector" "approval_rate" {
  name         = "TukTukPay [${var.environment}] approval rate below 85%"
  description  = "Business KPI: share of approved payments over 5 minutes"
  program_text = <<-EOF
    A = data('tuktukpay.payments.count', filter=${local.env_filter} and filter('payment.outcome', 'approved'), rollup='delta').sum().sum(over='5m')
    B = data('tuktukpay.payments.count', filter=${local.env_filter}, rollup='delta').sum().sum(over='5m')
    rate = (A / B * 100)
    detect(when(rate < 85, lasting='3m')).publish('approval_rate_low')
  EOF
  rule {
    detect_label  = "approval_rate_low"
    severity      = "Critical"
    description   = "Approval rate under 85% — check Tag Spotlight on checkout-api (acquirer, BIN, model version, initiator)"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "checkout_latency" {
  name         = "TukTukPay [${var.environment}] checkout-api p90 latency above 1.5s"
  description  = "APM standard metric — eligible for the AI Troubleshooting Agent"
  program_text = <<-EOF
    p90 = data('service.request.duration.ns.p90', filter=${local.apm_filter} and filter('sf_service', 'checkout-api') and filter('sf_error', 'false')).mean() / 1000000
    detect(when(p90 > 1500, lasting='2m')).publish('checkout_p90_high')
  EOF
  rule {
    detect_label  = "checkout_p90_high"
    severity      = "Major"
    description   = "checkout-api p90 over 1.5 s for 2 minutes"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "risk_latency" {
  name         = "TukTukPay [${var.environment}] risk-engine p90 latency above 150ms"
  description  = "Model inference regression (Act 2)"
  program_text = <<-EOF
    p90 = data('service.request.duration.ns.p90', filter=${local.apm_filter} and filter('sf_service', 'risk-engine')).mean() / 1000000
    detect(when(p90 > 150, lasting='2m')).publish('risk_p90_high')
  EOF
  rule {
    detect_label  = "risk_p90_high"
    severity      = "Major"
    description   = "risk-engine p90 over 150 ms — compare risk.model_version in Tag Spotlight, open AlwaysOn Profiling"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "agent_declines" {
  name         = "TukTukPay [${var.environment}] agent-initiated decline rate above 20%"
  description  = "Agentic commerce segment (Act 4)"
  program_text = <<-EOF
    D = data('tuktukpay.payments.declines', filter=${local.env_filter} and filter('payment.initiator', 'agent'), rollup='delta').sum().sum(over='5m')
    T = data('tuktukpay.payments.count', filter=${local.env_filter} and filter('payment.initiator', 'agent'), rollup='delta').sum().sum(over='5m')
    rate = (D / T * 100)
    detect(when(rate > 20 and T > 20, lasting='3m')).publish('agent_declines_high')
  EOF
  rule {
    detect_label  = "agent_declines_high"
    severity      = "Major"
    description   = "More than 20% of agent-initiated payments declined — check risk.reasons for bot_like_pattern"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "mandate_blocks" {
  name         = "TukTukPay [${var.environment}] agent mandate blocks above 10% of AMP payments"
  description  = "AI governance: agents exceeding, replaying or forging their pre-approvals (Act 4)"
  program_text = <<-EOF
    B = data('tuktukpay.agent_mandates', filter=${local.env_filter} and filter('mandate.verdict', 'blocked'), rollup='delta').sum().sum(over='5m')
    T = data('tuktukpay.agent_mandates', filter=${local.env_filter}, rollup='delta').sum().sum(over='5m')
    rate = (B / T * 100)
    detect(when(rate > 10 and T > 10, lasting='2m')).publish('mandate_blocks_high')
  EOF
  rule {
    detect_label  = "mandate_blocks_high"
    severity      = "Major"
    description   = "More than 10% of agent payments blocked at mandate verification — check mandate.block_reason and agent.id in checkout-api Tag Spotlight"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "kya_unavailable" {
  name         = "TukTukPay [${var.environment}] Know-Your-Agent registry unavailable"
  description  = "Governance dependency: checkout fails closed for every AMP agent when the KYA lookup fails (Act 4b)"
  program_text = <<-EOF
    u = data('tuktukpay.agent_mandates', filter=${local.env_filter} and filter('mandate.block_reason', 'mandate_kya_unavailable'), rollup='delta').sum()
    detect(when(u > 0)).publish('kya_unavailable')
  EOF
  rule {
    detect_label  = "kya_unavailable"
    severity      = "Critical"
    description   = "Agent payments blocked with mandate_kya_unavailable — wallet-sim KYA registry is down; every AMP checkout is failing closed"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "copilot_tokens" {
  name         = "TukTukPay [${var.environment}] copilot token burn above 20k tokens/min"
  description  = "Tokenomics guard (Act 3)"
  program_text = <<-EOF
    tokens = data('tuktukpay.copilot.tokens', filter=${local.env_filter}, rollup='rate').sum().scale(60)
    detect(when(tokens > 20000, lasting='2m')).publish('copilot_tokens_high')
  EOF
  rule {
    detect_label  = "copilot_tokens_high"
    severity      = "Warning"
    description   = "Copilot consuming more than 20k tokens per minute — look for tool-call loops (tuktukpay.copilot.iterations)"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "guardrail_blocks" {
  name         = "TukTukPay [${var.environment}] copilot guardrail blocked an action"
  description  = "Security signal: an LLM tool call was blocked (prompt injection?)"
  program_text = <<-EOF
    blocks = data('tuktukpay.copilot.guardrail_events', filter=${local.env_filter} and filter('guardrail.verdict', 'block'), rollup='delta').sum()
    detect(when(blocks > 0)).publish('guardrail_block')
  EOF
  rule {
    detect_label  = "guardrail_block"
    severity      = "Warning"
    description   = "The copilot tried to execute a blocked tool — open the trace (guardrail.verdict=block)"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "webhook_dlq" {
  name         = "TukTukPay [${var.environment}] webhook dead-letters"
  description  = "Merchant webhooks exhausted retries (Act 5)"
  program_text = <<-EOF
    dlq = data('tuktukpay.webhooks.deliveries', filter=${local.env_filter} and filter('outcome', 'dead_letter'), rollup='delta').sum()
    detect(when(dlq > 0)).publish('webhook_dlq')
  EOF
  rule {
    detect_label  = "webhook_dlq"
    severity      = "Minor"
    description   = "Webhook deliveries dead-lettered — check merchant.id in webhook-dispatcher Tag Spotlight"
    notifications = var.notifications
  }
}

# ---- APM service alerts per act (eligible for the AI Troubleshooting Agent) ----

resource "signalfx_detector" "router_latency" {
  name         = "TukTukPay [${var.environment}] payment-router p90 latency above 1s"
  description  = "Act 1: an acquirer hangs, the router waits 2.5 s before failing over — APM service alert, the AI Troubleshooting Agent runs on it"
  program_text = <<-EOF
    p90 = data('service.request.duration.ns.p90', filter=${local.apm_filter} and filter('sf_service', 'payment-router')).mean() / 1000000
    detect(when(p90 > 1000, lasting='2m')).publish('router_p90_high')
  EOF
  rule {
    detect_label  = "router_p90_high"
    severity      = "Major"
    description   = "payment-router p90 over 1 s for 2 minutes — Tag Spotlight on payment.acquirer / payment.card.bin; logs: event=acquirer.timeout"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "checkout_errors" {
  name         = "TukTukPay [${var.environment}] checkout-api error rate above 5%"
  description  = "Acts 1 (no failover), 4b (KYA registry down) and any dependency outage — APM service alert"
  program_text = <<-EOF
    E = data('service.request.count', filter=${local.apm_filter} and filter('sf_service', 'checkout-api') and filter('sf_error', 'true'), rollup='delta').sum().sum(over='3m')
    T = data('service.request.count', filter=${local.apm_filter} and filter('sf_service', 'checkout-api'), rollup='delta').sum().sum(over='3m')
    rate = (E / T * 100)
    detect(when(rate > 5 and T > 30, lasting='1m')).publish('checkout_errors_high')
  EOF
  rule {
    detect_label  = "checkout_errors_high"
    severity      = "Critical"
    description   = "More than 5% of checkout-api requests fail — logs: event=dependency.unavailable peer.service=* or event=mandate.blocked"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "copilot_latency" {
  name         = "TukTukPay [${var.environment}] merchant-copilot p90 latency above 8s"
  description  = "Act 3: a tool-call loop multiplies LLM round trips — APM service alert"
  program_text = <<-EOF
    p90 = data('service.request.duration.ns.p90', filter=${local.apm_filter} and filter('sf_service', 'merchant-copilot')).mean() / 1000000
    detect(when(p90 > 8000, lasting='2m')).publish('copilot_p90_high')
  EOF
  rule {
    detect_label  = "copilot_p90_high"
    severity      = "Major"
    description   = "merchant-copilot p90 over 8 s — logs: event=copilot.tool_loop_suspected (copilot.iterations, gen_ai.tool.names)"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "wallet_errors" {
  name         = "TukTukPay [${var.environment}] wallet-sim error rate above 20%"
  description  = "Act 4b: the Know-Your-Agent registry answers 503 — APM service alert on the dependency itself"
  program_text = <<-EOF
    E = data('service.request.count', filter=${local.apm_filter} and filter('sf_service', 'wallet-sim') and filter('sf_error', 'true'), rollup='delta').sum().sum(over='3m')
    T = data('service.request.count', filter=${local.apm_filter} and filter('sf_service', 'wallet-sim'), rollup='delta').sum().sum(over='3m')
    rate = (E / T * 100)
    detect(when(rate > 20 and T > 10, lasting='1m')).publish('wallet_errors_high')
  EOF
  rule {
    detect_label  = "wallet_errors_high"
    severity      = "Critical"
    description   = "wallet-sim failing — logs: event=kya.registry_unavailable; downstream checkout-api logs mandate.block_reason=mandate_kya_unavailable"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "ledger_latency" {
  name         = "TukTukPay [${var.environment}] ledger p90 latency above 400ms"
  description  = "Act 5: connection-pool starvation + slow INSERTs — APM service alert; Database Query Performance shows the statement"
  program_text = <<-EOF
    p90 = data('service.request.duration.ns.p90', filter=${local.apm_filter} and filter('sf_service', 'ledger')).mean() / 1000000
    detect(when(p90 > 400, lasting='2m')).publish('ledger_p90_high')
  EOF
  rule {
    detect_label  = "ledger_p90_high"
    severity      = "Major"
    description   = "ledger p90 over 400 ms — logs: event=db.pool.wait (db.pool.max, db.pool.waiting) and event=ledger.write_slow"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "merchant_sim_latency" {
  name         = "TukTukPay [${var.environment}] merchant-sim p90 latency above 1.5s"
  description  = "Act 5: one merchant's webhook endpoint is slow / failing — APM service alert"
  program_text = <<-EOF
    p90 = data('service.request.duration.ns.p90', filter=${local.apm_filter} and filter('sf_service', 'merchant-sim')).mean() / 1000000
    detect(when(p90 > 1500, lasting='2m')).publish('merchant_sim_p90_high')
  EOF
  rule {
    detect_label  = "merchant_sim_p90_high"
    severity      = "Minor"
    description   = "merchant-sim p90 over 1.5 s — logs: event=merchant.webhook_slow / merchant.webhook_error (merchant.id)"
    notifications = var.notifications
  }
}

# ---- Kubernetes alerts (k8s/ variant): the alert family that also gets a Remediation Plan ----
#
# Splunk's AutoDetect detectors ("K8s container restart count is > 0", "K8s pod phase
# pending/failed", "K8s cluster deployment is not at spec" ...) fire for the same
# conditions org-wide; these are the environment-scoped versions so every team sees its own
# alert with the pod and namespace dimensions the AI Troubleshooting Agent keys on.

locals {
  k8s_filter = "filter('k8s.namespace.name', '${var.k8s_namespace}')"
}

resource "signalfx_detector" "k8s_container_restarts" {
  name         = "TukTukPay [${var.environment}] Kubernetes container restarted"
  description  = "Acts 6 (ledger OOMKilled) and 7 (wallet-sim CrashLoopBackOff): a container restarted — Kubernetes alert, AI Troubleshooting Agent + Remediation Plan"
  program_text = <<-EOF
    restarts = data('k8s.container.restarts', filter=${local.k8s_filter}).delta().sum(by=['k8s.cluster.name', 'k8s.namespace.name', 'k8s.pod.name', 'k8s.container.name'])
    detect(when(restarts > 0)).publish('k8s_container_restarted')
  EOF
  rule {
    detect_label  = "k8s_container_restarted"
    severity      = "Critical"
    description   = "A container restarted (OOMKilled / crash) — logs: event=memory.pressure (ledger) or event=service.crashed (wallet-sim); check the last termination reason on the pod"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "k8s_pods_pending" {
  name         = "TukTukPay [${var.environment}] Kubernetes pod pending or failed"
  description  = "Unschedulable or failed pods (k8s/acts/oversize-rollout.sh, ImagePullBackOff) — Kubernetes alert"
  program_text = <<-EOF
    pending = data('k8s.pod.phase', filter=${local.k8s_filter}).sum(by=['k8s.cluster.name', 'k8s.namespace.name', 'k8s.pod.name'])
    detect(when(pending == 1 or pending == 4, lasting='2m')).publish('k8s_pod_pending')
  EOF
  rule {
    detect_label  = "k8s_pod_pending"
    severity      = "Major"
    description   = "A pod has been Pending (1) or Failed (4) for 2 minutes — resource requests, image tag or node capacity"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "k8s_deployment_unavailable" {
  name         = "TukTukPay [${var.environment}] Kubernetes deployment below desired replicas"
  description  = "A rollout that cannot become ready (crash loop, bad image, unschedulable) — Kubernetes alert"
  program_text = <<-EOF
    desired   = data('k8s.deployment.desired', filter=${local.k8s_filter}).sum(by=['k8s.cluster.name', 'k8s.namespace.name', 'k8s.deployment.name'])
    available = data('k8s.deployment.available', filter=${local.k8s_filter}).sum(by=['k8s.cluster.name', 'k8s.namespace.name', 'k8s.deployment.name'])
    detect(when(available < desired, lasting='3m')).publish('k8s_deployment_unavailable')
  EOF
  rule {
    detect_label  = "k8s_deployment_unavailable"
    severity      = "Major"
    description   = "Fewer available than desired replicas for 3 minutes — kubectl rollout status / rollout undo"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "k8s_memory_near_limit" {
  name         = "TukTukPay [${var.environment}] container memory above 85% of its limit"
  description  = "Act 6: the ledger's settlement cache grows until OOMKilled — Kubernetes alert that fires before the kill"
  program_text = <<-EOF
    usage = data('container.memory.usage', filter=${local.k8s_filter}).sum(by=['k8s.cluster.name', 'k8s.namespace.name', 'k8s.pod.name', 'k8s.container.name'])
    limit = data('k8s.container.memory_limit', filter=${local.k8s_filter}).sum(by=['k8s.cluster.name', 'k8s.namespace.name', 'k8s.pod.name', 'k8s.container.name'])
    pct = (usage / limit * 100)
    detect(when(pct > 85, lasting='30s')).publish('k8s_memory_near_limit')
  EOF
  rule {
    detect_label  = "k8s_memory_near_limit"
    severity      = "Major"
    description   = "Container memory within 15% of its limit — logs: event=memory.pressure with memory.leaked_mb"
    notifications = var.notifications
  }
}

resource "signalfx_detector" "k8s_cpu_at_limit" {
  name         = "TukTukPay [${var.environment}] container CPU at its limit (throttling)"
  description  = "Act 2 on Kubernetes: risk-engine (500m limit) saturates on the v3 canary — Kubernetes alert, Remediation Plan proposes the limit change"
  program_text = <<-EOF
    cpu   = data('container.cpu.utilization', filter=${local.k8s_filter}).sum(by=['k8s.cluster.name', 'k8s.namespace.name', 'k8s.pod.name', 'k8s.container.name'])
    limit = data('k8s.container.cpu_limit', filter=${local.k8s_filter}).sum(by=['k8s.cluster.name', 'k8s.namespace.name', 'k8s.pod.name', 'k8s.container.name'])
    pct = (cpu / limit * 100)
    detect(when(pct > 90, lasting='2m')).publish('k8s_cpu_at_limit')
  EOF
  rule {
    detect_label  = "k8s_cpu_at_limit"
    severity      = "Major"
    description   = "Container using more than 90% of its CPU limit for 2 minutes — risk-engine model v3 (AlwaysOn Profiling: _infer_v3; logs: event=risk.inference_slow)"
    notifications = var.notifications
  }
}

output "dashboard_url" {
  value = "https://app.${var.realm}.signalfx.com/#/dashboard/${signalfx_dashboard.warroom.id}"
}
