"""The copilot agent loop, instrumented with the OpenTelemetry GenAI semantic
conventions (spans: invoke_agent -> chat -> execute_tool; metrics:
gen_ai.client.token.usage, gen_ai.client.operation.duration).

Why hand-rolled instrumentation? Because the workshop must run without an LLM
vendor (mock provider). With a real SDK you would normally rely on
opentelemetry-instrumentation-openai-v2 / opentelemetry-util-genai (the path
Splunk AI Agent Monitoring documents) or the Splunk Agent Observability SDK — they
emit the same attributes, so every Splunk view built on them works with this too.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

import guardrail
from providers import LLMResponse, build_provider, estimate_cost
from tools import TOOL_SCHEMAS, TOOLS

AGENT_NAME = "tuktukpay-merchant-copilot"
AGENT_ID = "copilot-v2"
CAPTURE_CONTENT = os.getenv("COPILOT_CAPTURE_CONTENT", "false").lower() == "true"
GUARDRAIL_ENABLED = os.getenv("COPILOT_GUARDRAIL", "on").lower() != "off"
MAX_ITERATIONS = int(os.getenv("COPILOT_MAX_ITERATIONS", "12"))

tracer = trace.get_tracer("tuktukpay.merchant-copilot")
meter = metrics.get_meter("tuktukpay.merchant-copilot")

# Official GenAI client metrics
token_usage = meter.create_histogram("gen_ai.client.token.usage", unit="{token}", description="Number of input and output tokens used")
op_duration = meter.create_histogram("gen_ai.client.operation.duration", unit="s", description="GenAI operation duration")
# Workshop-specific business metrics ("Tokenomics")
cost_counter = meter.create_counter("tuktukpay.copilot.cost_usd", description="Estimated LLM spend in USD")
tokens_counter = meter.create_counter("tuktukpay.copilot.tokens", unit="{token}", description="Tokens consumed (counter twin of gen_ai.client.token.usage for simple dashboards)")
tool_calls_counter = meter.create_counter("tuktukpay.copilot.tool_calls", description="Tool executions by tool and outcome")
guardrail_counter = meter.create_counter("tuktukpay.copilot.guardrail_events", description="Guardrail verdicts")
questions_counter = meter.create_counter("tuktukpay.copilot.questions", description="Copilot questions by merchant and finish reason")

SYSTEM_PROMPT = (
    "You are the TukTukPay merchant support copilot. You help merchant staff understand payment outcomes "
    "(status, decline reasons, approval rate) using the tools provided. You must never move money: refunds "
    "require a human TukTukPay operator. Answer concisely in plain English."
)


@dataclass
class AgentResult:
    answer: str
    model: str
    provider: str
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    finish_reason: str = "stop"
    tool_calls: list[dict] = field(default_factory=list)
    guardrail: dict = field(default_factory=dict)
    trace_id: str = ""


def ask(merchant_id: str, question: str, session_id: str | None, provider_kind: str, model: str, loop_iterations: int = 0) -> AgentResult:
    provider = build_provider(provider_kind, model, loop_iterations=loop_iterations)
    session_id = session_id or f"sess_{uuid.uuid4().hex[:10]}"
    result = AgentResult(answer="", model=provider.model, provider=provider.name)

    with tracer.start_as_current_span(
        f"invoke_agent {AGENT_NAME}",
        kind=SpanKind.CLIENT,
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": AGENT_NAME,
            "gen_ai.agent.id": AGENT_ID,
            "gen_ai.provider.name": provider.name,
            "gen_ai.system": provider.name,  # legacy name still used by some dashboards
            "gen_ai.request.model": provider.model,
            "gen_ai.conversation.id": session_id,
            "merchant.id": merchant_id,
        },
    ) as agent_span:
        result.trace_id = format(agent_span.get_span_context().trace_id, "032x")
        t_agent = time.perf_counter()

        # ---- input guardrail -------------------------------------------------
        with tracer.start_as_current_span("guardrail.check_input", attributes={"guardrail.stage": "input"}) as g:
            input_verdict = guardrail.check_input(question) if GUARDRAIL_ENABLED else guardrail.Verdict(True, "disabled")
            g.set_attributes({"guardrail.verdict": input_verdict.verdict, "guardrail.rules": input_verdict.rules})
            if input_verdict.verdict == "flag":
                g.add_event("guardrail.prompt_injection_suspected", {"guardrail.rules": input_verdict.rules})
                agent_span.set_attribute("security.prompt_injection.suspected", True)
            guardrail_counter.add(1, {"guardrail.stage": "input", "guardrail.verdict": input_verdict.verdict})
        result.guardrail["input"] = {"verdict": input_verdict.verdict, "rules": input_verdict.rules}

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT, "merchant_id": merchant_id},
            {"role": "user", "content": f"merchant_id: {merchant_id}\n{question}"},
        ]

        # ---- agent loop ----------------------------------------------------------
        for iteration in range(1, MAX_ITERATIONS + 1):
            result.iterations = iteration
            response = _chat(provider, messages, result)

            if not response.tool_calls:
                result.answer = response.content
                result.finish_reason = "stop"
                break

            messages.append({"role": "assistant", "content": response.content, "tool_calls": [{"id": t.id, "name": t.name, "arguments": t.arguments} for t in response.tool_calls]})
            for call in response.tool_calls:
                output = _execute_tool(call.name, call.id, call.arguments, input_verdict, result)
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(output, default=str)})
        else:
            result.finish_reason = "max_iterations"
            result.answer = "I could not complete this request (too many steps). Please contact TukTukPay support with the payment id."
            agent_span.set_status(Status(StatusCode.ERROR, "agent hit max_iterations"))

        blocked = [t for t in result.tool_calls if t.get("guardrail") == "block"]
        if blocked and not result.answer:
            result.answer = "I can look up payments and explain declines, but I cannot issue refunds. A TukTukPay operator must approve any refund."

        result.cost_usd = round(result.cost_usd, 6)
        agent_span.set_attributes({
            "gen_ai.usage.input_tokens": result.input_tokens,
            "gen_ai.usage.output_tokens": result.output_tokens,
            "gen_ai.response.finish_reasons": [result.finish_reason],
            "tuktukpay.copilot.iterations": result.iterations,
            "tuktukpay.copilot.tool_call_count": len(result.tool_calls),
            "tuktukpay.copilot.cost_usd": result.cost_usd,
            "tuktukpay.copilot.guardrail_blocked": bool(blocked),
        })
        if CAPTURE_CONTENT:
            agent_span.set_attributes({"gen_ai.input.messages": json.dumps(messages[:2]), "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": result.answer}])})
        op_duration.record(time.perf_counter() - t_agent, {"gen_ai.operation.name": "invoke_agent", "gen_ai.provider.name": provider.name, "gen_ai.request.model": provider.model})
        questions_counter.add(1, {"merchant.id": merchant_id, "gen_ai.response.finish_reason": result.finish_reason, "gen_ai.request.model": provider.model})
        cost_counter.add(result.cost_usd, {"merchant.id": merchant_id, "gen_ai.request.model": provider.model, "gen_ai.provider.name": provider.name})
    return result


def _chat(provider, messages: list[dict], result: AgentResult) -> LLMResponse:
    with tracer.start_as_current_span(
        f"chat {provider.model}",
        kind=SpanKind.CLIENT,
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider.name,
            "gen_ai.system": provider.name,
            "gen_ai.request.model": provider.model,
            "gen_ai.request.temperature": 0.0,
        },
    ) as span:
        t0 = time.perf_counter()
        try:
            response = provider.chat(messages, TOOL_SCHEMAS)
        except Exception as exc:  # noqa: BLE001
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.set_attribute("error.type", type(exc).__name__)
            raise
        duration = time.perf_counter() - t0
        dims = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": provider.name, "gen_ai.request.model": response.model or provider.model}
        span.set_attributes({
            "gen_ai.response.model": response.model or provider.model,
            "gen_ai.response.id": response.response_id,
            "gen_ai.response.finish_reasons": [response.finish_reason],
            "gen_ai.usage.input_tokens": response.input_tokens,
            "gen_ai.usage.output_tokens": response.output_tokens,
            "tuktukpay.copilot.cost_usd": estimate_cost(provider.model, response.input_tokens, response.output_tokens),
        })
        if response.tool_calls:
            span.set_attribute("gen_ai.tool.names", [t.name for t in response.tool_calls])
        if CAPTURE_CONTENT:
            span.set_attributes({"gen_ai.input.messages": json.dumps(messages, default=str)[:12000], "gen_ai.output.messages": json.dumps({"content": response.content, "tool_calls": [t.name for t in response.tool_calls]})})
        token_usage.record(response.input_tokens, {**dims, "gen_ai.token.type": "input"})
        token_usage.record(response.output_tokens, {**dims, "gen_ai.token.type": "output"})
        tokens_counter.add(response.input_tokens, {**dims, "gen_ai.token.type": "input"})
        tokens_counter.add(response.output_tokens, {**dims, "gen_ai.token.type": "output"})
        op_duration.record(duration, dims)
        result.input_tokens += response.input_tokens
        result.output_tokens += response.output_tokens
        result.cost_usd += estimate_cost(provider.model, response.input_tokens, response.output_tokens)
        return response


def _execute_tool(name: str, call_id: str, arguments: dict, input_verdict: guardrail.Verdict, result: AgentResult):
    with tracer.start_as_current_span(
        f"execute_tool {name}",
        kind=SpanKind.INTERNAL,
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": call_id,
            "gen_ai.tool.type": "function",
        },
    ) as span:
        record = {"tool": name, "arguments": arguments}
        verdict = guardrail.check_action(name, arguments, input_verdict) if GUARDRAIL_ENABLED else guardrail.Verdict(True, "disabled")
        span.set_attributes({"guardrail.verdict": verdict.verdict, "guardrail.rules": verdict.rules})
        guardrail_counter.add(1, {"guardrail.stage": "action", "guardrail.verdict": verdict.verdict, "gen_ai.tool.name": name})
        if not verdict.allowed:
            span.add_event("guardrail.blocked", {"gen_ai.tool.name": name, "guardrail.rules": verdict.rules})
            span.set_status(Status(StatusCode.ERROR, "blocked by guardrail"))
            tool_calls_counter.add(1, {"gen_ai.tool.name": name, "outcome": "blocked"})
            record["guardrail"] = "block"
            result.tool_calls.append(record)
            return {"error": "blocked_by_guardrail", "rules": verdict.rules, "message": "This action requires approval by a TukTukPay operator."}
        fn = TOOLS.get(name)
        if fn is None:
            tool_calls_counter.add(1, {"gen_ai.tool.name": name, "outcome": "unknown"})
            record["guardrail"] = "unknown_tool"
            result.tool_calls.append(record)
            return {"error": f"unknown tool {name}"}
        try:
            output = fn(**arguments)
            tool_calls_counter.add(1, {"gen_ai.tool.name": name, "outcome": "ok"})
            record["guardrail"] = "pass"
        except Exception as exc:  # noqa: BLE001
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            tool_calls_counter.add(1, {"gen_ai.tool.name": name, "outcome": "error"})
            record["error"] = str(exc)
            output = {"error": str(exc)}
        if CAPTURE_CONTENT:
            span.set_attribute("gen_ai.tool.call.arguments", json.dumps(arguments)[:2000])
            span.set_attribute("gen_ai.tool.call.result", json.dumps(output, default=str)[:4000])
        result.tool_calls.append(record)
        return output
