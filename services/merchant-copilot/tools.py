"""Tools the copilot agent can call. Each is a plain function; the agent wraps
the call in an `execute_tool` span (GenAI semantic conventions)."""

from __future__ import annotations

import os
import uuid

import httpx

LEDGER_URL = os.getenv("LEDGER_URL", "http://localhost:8084").rstrip("/")
http_client = httpx.Client(timeout=httpx.Timeout(5.0))

DECLINE_KB = {
    "insufficient_funds": "Issuer response 51: the customer's account had insufficient funds or credit. Ask the customer to use another card or wallet; do not retry automatically.",
    "do_not_honor": "Issuer response 05: the issuing bank declined without a specific reason — often risk rules or cross-border restrictions. A retry via a domestic acquirer or a different method usually helps.",
    "invalid_card_number": "Issuer response 14: the card number failed validation. Ask the customer to re-enter the details.",
    "expired_card": "Issuer response 54: the card has expired. Prompt for a new card; account-updater services can refresh stored credentials.",
    "suspected_fraud": "Issuer response 59: the issuer suspects fraud. Do not retry; the customer should contact their bank.",
    "exceeds_withdrawal_frequency": "Issuer response 65: velocity limit reached at the issuer. Retry later.",
    "issuer_unavailable": "Issuer response 91: the issuer or acquirer host was unreachable; TukTukPay retries on a backup acquirer automatically.",
    "acquirer_timeout": "The acquirer did not answer within the timeout; TukTukPay failed over to a backup acquirer where possible.",
    "acquirer_unavailable": "All configured acquirers were unavailable for this route. TukTukPay operations are alerted automatically.",
    "risk_declined": "TukTukPay's fraud model scored this payment above the merchant's risk threshold before it reached the bank. Review the risk reasons on the payment.",
    "risk_engine_unavailable": "Risk scoring was unavailable, so the payment was not authorised (fail closed). This is an internal TukTukPay incident.",
    "router_unavailable": "Payment routing was unavailable. This is an internal TukTukPay incident.",
}

TOOL_SCHEMAS = [
    {"name": "get_payment", "description": "Look up one payment by its TukTukPay payment id (pay_...).", "parameters": {"type": "object", "properties": {"payment_id": {"type": "string"}}, "required": ["payment_id"]}},
    {"name": "list_recent_declines", "description": "List the merchant's most recent declined payments.", "parameters": {"type": "object", "properties": {"merchant_id": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["merchant_id"]}},
    {"name": "explain_decline_code", "description": "Explain a decline reason code in merchant-friendly terms.", "parameters": {"type": "object", "properties": {"decline_reason": {"type": "string"}}, "required": ["decline_reason"]}},
    {"name": "get_merchant_summary", "description": "Approval rate and decline breakdown for the merchant over the last N hours.", "parameters": {"type": "object", "properties": {"merchant_id": {"type": "string"}, "hours": {"type": "integer", "default": 1}}, "required": ["merchant_id"]}},
    {"name": "refund_payment", "description": "Refund a payment to the customer. Requires human approval.", "parameters": {"type": "object", "properties": {"payment_id": {"type": "string"}, "amount": {"type": "number"}}, "required": ["payment_id"]}},
]


def get_payment(payment_id: str, **_) -> dict:
    res = http_client.get(f"{LEDGER_URL}/v1/payments/{payment_id}")
    if res.status_code == 404:
        return {"error": "payment_not_found", "payment_id": payment_id}
    res.raise_for_status()
    return res.json()


def list_recent_declines(merchant_id: str, limit: int = 10, **_) -> dict:
    res = http_client.get(f"{LEDGER_URL}/v1/payments", params={"merchant_id": merchant_id, "status": "declined", "limit": limit})
    res.raise_for_status()
    return res.json()


def explain_decline_code(decline_reason: str, **_) -> dict:
    return {"decline_reason": decline_reason, "explanation": DECLINE_KB.get(decline_reason, "Unknown reason code. Ask TukTukPay support with the payment id.")}


def get_merchant_summary(merchant_id: str, hours: int = 1, **_) -> dict:
    res = http_client.get(f"{LEDGER_URL}/v1/merchants/{merchant_id}/summary", params={"hours": hours})
    res.raise_for_status()
    return res.json()


def refund_payment(payment_id: str, amount: float | None = None, **_) -> dict:
    # Only reachable when the guardrail is disabled (COPILOT_GUARDRAIL=off): simulated.
    return {"refund_id": f"rf_{uuid.uuid4().hex[:12]}", "payment_id": payment_id, "amount": amount, "status": "simulated"}


TOOLS = {
    "get_payment": get_payment,
    "list_recent_declines": list_recent_declines,
    "explain_decline_code": explain_decline_code,
    "get_merchant_summary": get_merchant_summary,
    "refund_payment": refund_payment,
}
