"""LLM providers behind one tiny interface.

    provider.chat(messages, tools) -> LLMResponse

`mock` needs no network or API key and is fully deterministic, so the workshop
never depends on an LLM vendor being up. `openai`, `anthropic` and `bedrock`
call the real APIs when the matching credentials are present.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field

# USD per 1M tokens (input, output) — used for the "Tokenomics" cost attribute.
PRICES = {
    "tuktuk-1-mini": (0.15, 0.60),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "amazon.nova-pro-v1:0": (0.80, 3.20),
    "amazon.nova-lite-v1:0": (0.06, 0.24),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = next((v for k, v in PRICES.items() if k in model), (1.0, 4.0))
    return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 6)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"
    model: str = ""
    response_id: str = ""


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- mock
class MockProvider:
    """Scripted 'model' that behaves like a helpful-but-gullible tool-calling LLM."""

    name = "tuktuk-mock"

    def __init__(self, model: str = "tuktuk-1-mini", loop_iterations: int = 0):
        self.model = model
        self.loop_iterations = loop_iterations  # >0 => re-call get_payment this many times (chaos)
        self.simulate_latency = os.getenv("COPILOT_MOCK_LATENCY", "true").lower() != "false"

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        response = self._decide(messages)
        if self.simulate_latency:
            # Real models take time proportional to prompt + completion size.
            time.sleep(0.2 + response.input_tokens / 6000 + response.output_tokens / 250)
        return response

    def _decide(self, messages: list[dict]) -> LLMResponse:
        question = next((m["content"] for m in messages if m["role"] == "user"), "")
        tool_results = [m for m in messages if m["role"] == "tool"]
        prompt_text = json.dumps(messages)
        input_tokens = _approx_tokens(prompt_text)
        merchant_id = _extract(question, r"merchant[_ ]id[:= ]+([a-z0-9-]+)") or (messages[0].get("merchant_id") if messages else None) or "unknown"
        payment_id = _extract(question, r"(pay_[0-9a-f]{6,})")
        injected = bool(re.search(r"ignore (all )?(previous|prior) instructions|refund .* to (my|this|the following)|developer mode|you are now", question.lower()))
        n_calls = len(tool_results)

        def call(name, args):
            return LLMResponse("", [ToolCall(f"call_{uuid.uuid4().hex[:8]}", name, args)], input_tokens, 24, "tool_calls", self.model, f"mock-{uuid.uuid4().hex[:8]}")

        # After the guardrail blocked a refund attempt, the model gives up gracefully.
        if any(t["name"] == "refund_payment" for t in tool_results):
            text = "I can look up payments and explain declines, but I am not able to issue refunds or move funds. A TukTukPay operator must approve any refund — please raise a ticket with the payment id."
            return LLMResponse(text, [], input_tokens, _approx_tokens(text), "stop", self.model, f"mock-{uuid.uuid4().hex[:8]}")

        # A gullible model: the injected instruction wins over the system prompt.
        if injected and payment_id:
            if n_calls == 0:
                return call("get_payment", {"payment_id": payment_id})
            return call("refund_payment", {"payment_id": payment_id, "amount": _extract(question, r"(\d+(?:\.\d+)?)") or None})

        # Chaos: the model keeps re-fetching the same payment ("I should double-check…").
        if self.loop_iterations and payment_id and n_calls < self.loop_iterations:
            return call("get_payment", {"payment_id": payment_id})

        if payment_id:
            if n_calls == 0 or not any(t["name"] == "get_payment" for t in tool_results):
                return call("get_payment", {"payment_id": payment_id})
            payment = _last_result(tool_results, "get_payment")
            reason = (payment or {}).get("decline_reason")
            if reason and not any(t["name"] == "explain_decline_code" for t in tool_results):
                return call("explain_decline_code", {"decline_reason": reason})
            return self._answer_payment(payment, _last_result(tool_results, "explain_decline_code"), input_tokens)

        if re.search(r"approval rate|how (am|are) (i|we) doing|summary|performance", question.lower()):
            if not any(t["name"] == "get_merchant_summary" for t in tool_results):
                return call("get_merchant_summary", {"merchant_id": merchant_id, "hours": 1})
            return self._answer_summary(_last_result(tool_results, "get_merchant_summary"), input_tokens)

        if re.search(r"declin|fail", question.lower()):
            if not any(t["name"] == "list_recent_declines" for t in tool_results):
                return call("list_recent_declines", {"merchant_id": merchant_id, "limit": 10})
            return self._answer_declines(_last_result(tool_results, "list_recent_declines"), input_tokens)

        text = "I can help with payment status, decline reasons and your approval rate. Share a payment id (pay_...) or ask for a summary."
        return LLMResponse(text, [], input_tokens, _approx_tokens(text), "stop", self.model, f"mock-{uuid.uuid4().hex[:8]}")

    def _answer_payment(self, payment, explanation, input_tokens):
        if not payment or payment.get("error"):
            text = "I could not find that payment. Please check the id and try again."
        elif payment.get("status") == "approved":
            text = f"Payment {payment['payment_id']} for {payment['amount']} {payment['currency']} was approved via {payment.get('acquirer')} (auth code {payment.get('auth_code')})."
        else:
            reason = payment.get("decline_reason") or "unknown"
            why = (explanation or {}).get("explanation", "")
            text = f"Payment {payment['payment_id']} ({payment['amount']} {payment['currency']}, {payment.get('payment_method')}) was {payment.get('status')} with reason '{reason}'. {why}"
            if payment.get("risk_model_version"):
                text += f" Risk score {payment.get('risk_score')} from model {payment.get('risk_model_version')}."
        return LLMResponse(text, [], input_tokens, _approx_tokens(text), "stop", self.model, f"mock-{uuid.uuid4().hex[:8]}")

    def _answer_summary(self, summary, input_tokens):
        if not summary or summary.get("total") in (None, 0):
            text = "No payments in the last hour."
        else:
            rate = summary.get("approval_rate") or 0
            top = [b for b in summary.get("breakdown", []) if b.get("status") != "approved"][:3]
            reasons = ", ".join(f"{b.get('decline_reason') or b.get('status')} ({b.get('count')})" for b in top) or "none"
            text = f"In the last hour you processed {summary['total']} payments with a {rate:.1%} approval rate. Top decline reasons: {reasons}."
        return LLMResponse(text, [], input_tokens, _approx_tokens(text), "stop", self.model, f"mock-{uuid.uuid4().hex[:8]}")

    def _answer_declines(self, declines, input_tokens):
        items = (declines or {}).get("payments", [])
        if not items:
            text = "No recent declines found."
        else:
            lines = "; ".join(f"{p['payment_id']} {p['amount']} {p['currency']} — {p.get('decline_reason')}" for p in items[:5])
            text = f"Your {len(items)} most recent declines: {lines}."
        return LLMResponse(text, [], input_tokens, _approx_tokens(text), "stop", self.model, f"mock-{uuid.uuid4().hex[:8]}")


def _extract(text: str, pattern: str):
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1) if m else None


def _last_result(tool_results: list[dict], name: str):
    for m in reversed(tool_results):
        if m["name"] == name:
            try:
                return json.loads(m["content"])
            except json.JSONDecodeError:
                return None
    return None


# --------------------------------------------------------------------------- openai
class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str):
        from openai import OpenAI  # lazy import: optional dependency

        self.client = OpenAI()
        self.model = model

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        oa_msgs = []
        for m in messages:
            if m["role"] == "tool":
                oa_msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                oa_msgs.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                    {"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])}} for t in m["tool_calls"]]})
            else:
                oa_msgs.append({"role": m["role"], "content": m["content"]})
        res = self.client.chat.completions.create(
            model=self.model, messages=oa_msgs,
            tools=[{"type": "function", "function": t} for t in tools], tool_choice="auto", temperature=0,
        )
        choice = res.choices[0]
        calls = [ToolCall(t.id, t.function.name, json.loads(t.function.arguments or "{}")) for t in (choice.message.tool_calls or [])]
        return LLMResponse(choice.message.content or "", calls, res.usage.prompt_tokens, res.usage.completion_tokens, choice.finish_reason or "stop", res.model, res.id)


# --------------------------------------------------------------------------- anthropic
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str):
        import anthropic  # lazy import: optional dependency

        self.client = anthropic.Anthropic()
        self.model = model

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        an_msgs = []
        for m in messages:
            if m["role"] == "system":
                continue
            if m["role"] == "tool":
                an_msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                blocks = ([{"type": "text", "text": m["content"]}] if m.get("content") else []) + [
                    {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["arguments"]} for t in m["tool_calls"]]
                an_msgs.append({"role": "assistant", "content": blocks})
            else:
                an_msgs.append({"role": m["role"], "content": m["content"]})
        res = self.client.messages.create(
            model=self.model, max_tokens=600, system=system, messages=an_msgs,
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools],
        )
        text = "".join(b.text for b in res.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in res.content if b.type == "tool_use"]
        finish = "tool_calls" if calls else (res.stop_reason or "stop")
        return LLMResponse(text, calls, res.usage.input_tokens, res.usage.output_tokens, finish, res.model, res.id)


# --------------------------------------------------------------------------- bedrock
class BedrockProvider:
    name = "aws.bedrock"

    def __init__(self, model: str):
        import boto3  # lazy import: optional dependency

        self.client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "ap-southeast-1"))
        self.model = model

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        system = [{"text": m["content"]} for m in messages if m["role"] == "system"]
        br_msgs = []
        for m in messages:
            if m["role"] == "system":
                continue
            if m["role"] == "tool":
                br_msgs.append({"role": "user", "content": [{"toolResult": {"toolUseId": m["tool_call_id"], "content": [{"json": _safe_json(m["content"])}]}}]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                blocks = ([{"text": m["content"]}] if m.get("content") else []) + [
                    {"toolUse": {"toolUseId": t["id"], "name": t["name"], "input": t["arguments"]}} for t in m["tool_calls"]]
                br_msgs.append({"role": "assistant", "content": blocks})
            else:
                br_msgs.append({"role": m["role"], "content": [{"text": m["content"]}]})
        res = self.client.converse(
            modelId=self.model, system=system, messages=br_msgs,
            toolConfig={"tools": [{"toolSpec": {"name": t["name"], "description": t["description"], "inputSchema": {"json": t["parameters"]}}} for t in tools]},
            inferenceConfig={"maxTokens": 600, "temperature": 0},
        )
        content = res["output"]["message"]["content"]
        text = "".join(b.get("text", "") for b in content if "text" in b)
        calls = [ToolCall(b["toolUse"]["toolUseId"], b["toolUse"]["name"], b["toolUse"].get("input", {})) for b in content if "toolUse" in b]
        finish = "tool_calls" if calls else res.get("stopReason", "stop")
        usage = res.get("usage", {})
        return LLMResponse(text, calls, usage.get("inputTokens", 0), usage.get("outputTokens", 0), finish, self.model, res.get("ResponseMetadata", {}).get("RequestId", ""))


def _safe_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def build_provider(kind: str, model: str, loop_iterations: int = 0):
    kind = (kind or "mock").lower()
    if kind == "openai":
        return OpenAIProvider(model or "gpt-4o-mini")
    if kind == "anthropic":
        return AnthropicProvider(model or "claude-haiku-4-5")
    if kind == "bedrock":
        return BedrockProvider(model or "amazon.nova-lite-v1:0")
    return MockProvider(model or "tuktuk-1-mini", loop_iterations=loop_iterations)
