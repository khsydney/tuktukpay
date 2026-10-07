"""wallet-sim — stands in for a consumer wallet (TrueMoney / DANA / GCash / TNG / Alipay)
that supports AMP-style agentic payments.

Two jobs:
  * Know-Your-Agent registry:  GET /v1/kya/{agent_id}  -> rating A-D, operator, registered
  * Task mandate issuance:     POST /v1/mandates        -> a scoped, time-boxed task token for a
                                                         registered agent, tied to a customer + wallet

Auto-instrumented with the Splunk distro. The `kya_registry_down` chaos flag makes the
KYA lookup fail (503) so TukTukPay's checkout has to fail closed: an "AI governance
dependency" incident for the troubleshooting acts.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

from fastapi import FastAPI, HTTPException
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field

import amp
from chaos import ChaosFlags
from tuktuk_logging import log_event, setup_logging

log = setup_logging("wallet-sim")

chaos = ChaosFlags(watch={"kya_registry_down", "wallet_crash_loop"})


def _crash_loop_watch():
    """Act 7 (Kubernetes): with the `wallet_crash_loop` flag on, the process dies shortly after
    start as if a schema migration in a new release had failed. Kubernetes restarts the pod
    into CrashLoopBackOff (the AutoDetect "container restart count > 0" alert); Know-Your-Agent
    lookups fail meanwhile and checkout-api fails closed for every AMP agent payment."""
    time.sleep(4)  # let the pod come up and poll the flags once, like a service that fails on its first real request
    while True:
        if chaos.enabled("wallet_crash_loop"):
            release = chaos.param("wallet_crash_loop", "release", "wallet-sim 1.7.0")
            code = chaos.param("wallet_crash_loop", "exit_code", 3)
            log_event(log, logging.CRITICAL, "service.crashed",
                      f"FATAL: startup check failed after rollout {release}: KYA registry schema migration kya_agents_v2 did not apply "
                      f"(relation \"kya_agents_v2\" does not exist); exiting with code {code}",
                      **{"error.type": "SchemaMigrationError", "deployment.release": release, "process.exit_code": code, "peer.service": "kya-registry-db"})
            time.sleep(2)  # the batch log processor exports every second; never block on a flush here
            os._exit(int(code))
        time.sleep(2)


threading.Thread(target=_crash_loop_watch, name="crash-loop-watch", daemon=True).start()
meter = metrics.get_meter("tuktukpay.wallet-sim")
mandates_issued = meter.create_counter("tuktukpay.wallet.mandates_issued", description="Task mandates issued by the wallet")
kya_lookups = meter.create_counter("tuktukpay.wallet.kya_lookups", description="Know-Your-Agent lookups by outcome")

app = FastAPI(title="wallet-sim (AMP task mandates + KYA registry)", version="1.0.0")

# Know-Your-Agent registry: who is allowed to act for our customers, and how trusted they are.
KYA_REGISTRY = {
    "travelgenie.ai":   {"operator": "TravelGenie Pte Ltd",     "rating": "A", "registered": True,  "capabilities": ["flights", "hotels"]},
    "shopbot.lazada":    {"operator": "Lazada Thailand",              "rating": "A", "registered": True,  "capabilities": ["electronics", "fashion", "groceries"]},
    "gemini-shopper":   {"operator": "Google",                  "rating": "B", "registered": True,  "capabilities": ["electronics", "fashion", "games"]},
    "siam-concierge":   {"operator": "Siam Concierge Co.",      "rating": "B", "registered": True,  "capabilities": ["flights", "hotels", "food"]},
    "grab-assistant":   {"operator": "Grab",                 "rating": "B", "registered": True,  "capabilities": ["food", "rides"]},
    "dealhunter.app":   {"operator": "DealHunter Labs",         "rating": "C", "registered": True,  "capabilities": ["electronics", "games"]},
    "promo-hunter-bot": {"operator": "unknown",                 "rating": "D", "registered": False, "capabilities": []},
}

WALLETS = {"truemoney": "THB", "rabbitlinepay": "THB", "shopeepay": "THB", "dana": "IDR", "gcash": "PHP", "tng": "MYR", "alipayhk": "HKD", "alipay": "CNY", "grabpay": "SGD"}

ISSUED: deque = deque(maxlen=500)


class MandateRequest(BaseModel):
    agent_id: str
    customer_id: str = "cus_00001"
    wallet: str = "truemoney"
    intent: str
    category: str = "flights"
    max_amount: float = Field(gt=0)
    currency: str = "THB"
    merchants: list[str] = Field(default_factory=list)
    ttl_s: int = 900
    max_uses: int = 1


@app.get("/healthz")
def healthz():
    return {"status": "ok", "agents": len(KYA_REGISTRY), "kya_registry_down": chaos.enabled("kya_registry_down")}


@app.get("/v1/kya/{agent_id}")
def kya(agent_id: str):
    span = trace.get_current_span()
    span.set_attribute("agent.id", agent_id)
    if chaos.enabled("kya_registry_down"):
        kya_lookups.add(1, {"outcome": "unavailable"})
        span.set_attribute("kya.outcome", "unavailable")
        # The Act 4b smoking gun: the registry's backend is down, so every verification fails closed.
        log_event(log, logging.ERROR, "kya.registry_unavailable",
                  f"Know-Your-Agent lookup for {agent_id} failed: registry database unreachable (connection refused); returning 503",
                  **{"agent.id": agent_id, "kya.outcome": "unavailable", "http.response.status_code": 503,
                     "peer.service": "kya-registry-db", "error.type": "RegistryUnavailable"})
        raise HTTPException(503, "KYA registry unavailable")
    entry = KYA_REGISTRY.get(agent_id)
    if not entry:
        kya_lookups.add(1, {"outcome": "unknown"})
        span.set_attributes({"kya.outcome": "unknown", "kya.rating": "none"})
        log_event(log, logging.WARNING, "kya.agent_unknown", f"Know-Your-Agent lookup: {agent_id} is not registered with this wallet",
                  **{"agent.id": agent_id, "kya.outcome": "unknown", "kya.rating": "none"})
        return {"agent_id": agent_id, "registered": False, "rating": "none", "operator": "unknown"}
    kya_lookups.add(1, {"outcome": "found", "kya.rating": entry["rating"]})
    span.set_attributes({"kya.outcome": "found", "kya.rating": entry["rating"]})
    log_event(log, logging.DEBUG, "kya.lookup", f"Know-Your-Agent lookup: {agent_id} rated {entry['rating']} ({entry['operator']})",
              **{"agent.id": agent_id, "kya.outcome": "found", "kya.rating": entry["rating"], "kya.operator": entry["operator"]})
    return {"agent_id": agent_id, **entry}


@app.get("/v1/agents")
def agents():
    return KYA_REGISTRY


@app.post("/v1/mandates")
def issue_mandate(req: MandateRequest):
    span = trace.get_current_span()
    entry = KYA_REGISTRY.get(req.agent_id)
    span.set_attributes({"agent.id": req.agent_id, "amp.wallet": req.wallet, "amp.mandate.max_amount": req.max_amount, "amp.mandate.currency": req.currency})
    if not entry or not entry["registered"]:
        mandates_issued.add(1, {"outcome": "refused", "reason": "agent_not_registered"})
        span.set_attribute("amp.issue.outcome", "refused_unregistered_agent")
        log_event(log, logging.WARNING, "mandate.refused",
                  f"refused task mandate for unregistered agent {req.agent_id} (customer {req.customer_id}, wallet {req.wallet})",
                  **{"agent.id": req.agent_id, "customer.id": req.customer_id, "amp.wallet": req.wallet, "amp.issue.outcome": "refused_unregistered_agent",
                     "amp.mandate.max_amount": req.max_amount, "amp.mandate.currency": req.currency})
        raise HTTPException(403, f"agent {req.agent_id} is not registered with this wallet (Know-Your-Agent)")
    if req.wallet not in WALLETS:
        raise HTTPException(400, f"unknown wallet {req.wallet}")
    token, payload = amp.issue(
        agent_id=req.agent_id, customer_id=req.customer_id, wallet=req.wallet, intent=req.intent, category=req.category,
        max_amount=req.max_amount, currency=req.currency, merchants=req.merchants, ttl_s=req.ttl_s, max_uses=req.max_uses,
        kya_rating=entry["rating"],
    )
    span.set_attributes({"amp.task_id": payload["task_id"], "amp.kya.rating": entry["rating"], "amp.issue.outcome": "issued"})
    mandates_issued.add(1, {"outcome": "issued", "amp.wallet": req.wallet, "kya.rating": entry["rating"]})
    ISSUED.append({**payload, "issued_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    log_event(log, logging.INFO, "mandate.issued",
              f"issued task mandate {payload['task_id']} to {req.agent_id} (KYA {entry['rating']}) for {req.customer_id}: max {req.max_amount:.2f} {req.currency}, merchants {req.merchants or 'any'}, {req.max_uses} use, ttl {req.ttl_s}s",
              **{"amp.task_id": payload["task_id"], "agent.id": req.agent_id, "amp.kya.rating": entry["rating"], "kya.operator": entry["operator"],
                 "customer.id": req.customer_id, "amp.wallet": req.wallet, "amp.mandate.max_amount": req.max_amount, "amp.mandate.currency": req.currency,
                 "amp.mandate.merchants": req.merchants, "amp.mandate.category": req.category, "amp.mandate.ttl_s": req.ttl_s, "amp.mandate.max_uses": req.max_uses,
                 "amp.issue.outcome": "issued"})
    return {"token": token, "mandate": payload}


@app.get("/v1/mandates")
def list_mandates(limit: int = 20):
    return list(ISSUED)[-limit:][::-1]
