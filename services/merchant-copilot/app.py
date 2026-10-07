"""TukTukPay merchant-copilot — an LLM agent that answers merchants' questions
("why did payment pay_... fail?") by calling internal tools. FastAPI, Python.

Auto-instrumented by the Splunk distro (HTTP in/out); GenAI spans and metrics
come from agent.py. Provider is selected per request or via env:
  COPILOT_PROVIDER = mock | openai | anthropic | bedrock   (default mock)
  COPILOT_MODEL    = model id for the chosen provider
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI
from opentelemetry import trace
from pydantic import BaseModel

import agent
from chaos import ChaosFlags
from tuktuk_logging import log_event, setup_logging

log = setup_logging("merchant-copilot")

chaos = ChaosFlags(watch={"copilot_tool_loop"})
DEFAULT_PROVIDER = os.getenv("COPILOT_PROVIDER", "mock")
# Compose keeps a trailing "# comment" when the value in .env is empty — strip it.
DEFAULT_MODEL = os.getenv("COPILOT_MODEL", "").split("#", 1)[0].strip()
TOOL_LOOP_WARN_AT = int(os.getenv("COPILOT_TOOL_LOOP_WARN_AT", "5"))

app = FastAPI(title="TukTukPay merchant-copilot", version="2.0.0")
log_event(log, logging.INFO, "service.started", f"merchant-copilot ready: provider={DEFAULT_PROVIDER} model={DEFAULT_MODEL or '(provider default)'} guardrail={'on' if agent.GUARDRAIL_ENABLED else 'off'}",
          **{"gen_ai.provider.name": DEFAULT_PROVIDER, "gen_ai.request.model": DEFAULT_MODEL or "(provider default)", "copilot.guardrail_enabled": agent.GUARDRAIL_ENABLED,
             "copilot.max_iterations": agent.MAX_ITERATIONS})


class AskRequest(BaseModel):
    merchant_id: str
    question: str
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None


@app.get("/healthz")
def healthz():
    return {"status": "ok", "provider": DEFAULT_PROVIDER, "model": DEFAULT_MODEL or "(provider default)", "guardrail": agent.GUARDRAIL_ENABLED}


@app.post("/v1/ask")
def ask(req: AskRequest):
    started = time.perf_counter()
    span = trace.get_current_span()
    span.set_attributes({"merchant.id": req.merchant_id, "copilot.question_length": len(req.question)})

    loop_iterations = chaos.param("copilot_tool_loop", "iterations", 10) if chaos.enabled("copilot_tool_loop") else 0
    result = agent.ask(
        merchant_id=req.merchant_id,
        question=req.question,
        session_id=req.session_id,
        provider_kind=req.provider or DEFAULT_PROVIDER,
        model=req.model or DEFAULT_MODEL,
        loop_iterations=loop_iterations,
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    tool_names = [t.get("tool", "") for t in result.tool_calls]
    fields = {
        "merchant.id": req.merchant_id, "gen_ai.provider.name": result.provider, "gen_ai.request.model": result.model,
        "gen_ai.conversation.id": req.session_id or "", "copilot.iterations": result.iterations, "copilot.tool_call_count": len(tool_names),
        "gen_ai.tool.names": tool_names, "gen_ai.usage.input_tokens": result.input_tokens, "gen_ai.usage.output_tokens": result.output_tokens,
        "copilot.cost_usd": result.cost_usd, "gen_ai.response.finish_reason": result.finish_reason, "duration_ms": latency_ms,
        "guardrail.verdict": result.guardrail.get("input", {}).get("verdict", ""),
    }
    if result.finish_reason == "max_iterations":
        log_event(log, logging.ERROR, "copilot.max_iterations",
                  f"copilot gave up after {result.iterations} iterations for {req.merchant_id}: {result.input_tokens + result.output_tokens} tokens, {latency_ms:.0f} ms, tools {tool_names}", **fields)
    elif result.iterations >= TOOL_LOOP_WARN_AT:
        repeated = max(tool_names.count(n) for n in set(tool_names)) if tool_names else 0
        log_event(log, logging.WARNING, "copilot.tool_loop_suspected",
                  f"copilot needed {result.iterations} iterations for one question from {req.merchant_id} ({repeated}x the same tool, {result.input_tokens + result.output_tokens} tokens, {latency_ms:.0f} ms)",
                  **fields, **{"copilot.repeated_tool_calls": repeated})
    log_event(log, logging.INFO, "copilot.answered",
              f"copilot answered {req.merchant_id} in {latency_ms:.0f} ms: {result.iterations} iterations, {result.input_tokens}+{result.output_tokens} tokens, USD {result.cost_usd:.5f}, guardrail {fields['guardrail.verdict']}",
              **fields)
    return {
        "answer": result.answer,
        "model": result.model,
        "provider": result.provider,
        "iterations": result.iterations,
        "finish_reason": result.finish_reason,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "cost_usd": result.cost_usd},
        "tool_calls": result.tool_calls,
        "guardrail": result.guardrail,
        "trace_id": result.trace_id,
        "latency_ms": latency_ms,
    }
