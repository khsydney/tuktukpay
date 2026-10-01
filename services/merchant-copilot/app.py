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

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("merchant-copilot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is too chatty for Log Observer

chaos = ChaosFlags()
DEFAULT_PROVIDER = os.getenv("COPILOT_PROVIDER", "mock")
DEFAULT_MODEL = os.getenv("COPILOT_MODEL", "")

app = FastAPI(title="TukTukPay merchant-copilot", version="2.0.0")


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
    log.info(
        "copilot answered merchant=%s model=%s iterations=%d tokens_in=%d tokens_out=%d cost_usd=%.5f finish=%s latency_ms=%.0f guardrail=%s",
        req.merchant_id, result.model, result.iterations, result.input_tokens, result.output_tokens, result.cost_usd,
        result.finish_reason, latency_ms, result.guardrail.get("input", {}).get("verdict"),
    )
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
