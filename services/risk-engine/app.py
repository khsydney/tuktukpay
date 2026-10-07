"""TukTukPay risk-engine — real-time fraud / risk scoring (Python, FastAPI).

Zero-code instrumented with the Splunk Distribution of OpenTelemetry Python
(`opentelemetry-instrument uvicorn app:app`). The only OpenTelemetry code in
this file adds *business* context (model version, score, decision) to the spans
the auto-instrumentation already creates, plus a few custom metrics.

Two "models" live here:
  v2 — the production model: fast, well calibrated.
  v3 — a canary model that is slower (bigger feature set) and biased against
       wallet payments in IDR/PHP. Enabling the `risk_model_drift` chaos flag
       routes a percentage of traffic to v3. This is the Act 2 storyline:
       approval rate drops and p95 latency climbs, but only for one slice.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field

from chaos import ChaosFlags
from tuktuk_logging import log_event, setup_logging

log = setup_logging("risk-engine")

tracer = trace.get_tracer("tuktukpay.risk-engine")
meter = metrics.get_meter("tuktukpay.risk-engine")
score_hist = meter.create_histogram("tuktukpay.risk.score", description="Risk score distribution (0-1)")
inference_hist = meter.create_histogram("tuktukpay.risk.inference_ms", unit="ms", description="Model inference time")
decision_counter = meter.create_counter("tuktukpay.risk.decisions", description="Risk decisions by model/decision")

chaos = ChaosFlags(watch={"risk_model_drift"})
app = FastAPI(title="TukTukPay risk-engine", version="2.3.0")
INFERENCE_SLO_MS = float(os.getenv("RISK_INFERENCE_SLO_MS", "50"))
log_event(log, logging.INFO, "service.started", "risk-engine ready: model v2 in production, v3 canary available",
          **{"risk.model_version": "v2", "risk.inference_slo_ms": INFERENCE_SLO_MS})

# --- reference data ---------------------------------------------------------
CURRENCY_COUNTRY = {"THB": "TH", "SGD": "SG", "MYR": "MY", "IDR": "ID", "PHP": "PH", "VND": "VN", "USD": "US"}
HIGH_RISK_BINS = {"601100", "222300", "356789"}
MERCHANT_TYPICAL_SGD = {  # typical ticket in SGD-equivalent, converted per currency for an amount ratio
    "thai-airways": 170.0,
    "lazada-th": 85.0,
    "grab": 12.0,
    "cafe-amazon": 3.0,
    "centara-hotels": 150.0,
    "garena": 25.0,
}
FX_PER_SGD = {"SGD": 1.0, "MYR": 3.3, "THB": 26.0, "IDR": 12000.0, "PHP": 43.0, "VND": 19500.0, "USD": 0.75}
DENY_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.60

# --- velocity tracking (in-memory, per process) -----------------------------
_velocity: dict[str, deque] = defaultdict(deque)


def _velocity_count(customer_id: str, window_s: int = 60) -> int:
    now = time.time()
    q = _velocity[customer_id]
    q.append(now)
    while q and now - q[0] > window_s:
        q.popleft()
    if len(_velocity) > 50000:  # crude memory guard
        _velocity.clear()
    return len(q)


class Customer(BaseModel):
    id: str = "anonymous"
    country: str = ""


class ScoreRequest(BaseModel):
    payment_id: str
    merchant_id: str
    amount: float
    currency: str
    payment_method: str = "card"
    card_bin: str | None = None
    card_network: str | None = None
    wallet_provider: str | None = None
    customer: Customer = Field(default_factory=Customer)
    initiator: str = "human"
    agent_verified: bool = False   # AMP mandate verified + KYA-registered agent (set by checkout-api)
    kya_rating: str = ""
    channel: str = "web"


class ScoreResponse(BaseModel):
    score: float
    decision: str
    model_version: str
    reasons: list[str]
    latency_ms: float


# --- feature extraction -----------------------------------------------------
def _features(req: ScoreRequest) -> dict:
    typical = MERCHANT_TYPICAL_SGD.get(req.merchant_id, 100.0) * FX_PER_SGD.get(req.currency, 1.0)
    amount_ratio = req.amount / typical if typical else 1.0
    country_mismatch = bool(req.customer.country) and CURRENCY_COUNTRY.get(req.currency, "") not in ("", req.customer.country, "US")
    return {
        "amount_ratio": amount_ratio,
        "amount_log": math.log1p(max(req.amount, 0.0)),
        "country_mismatch": 1.0 if country_mismatch else 0.0,
        "high_risk_bin": 1.0 if (req.card_bin or "") in HIGH_RISK_BINS else 0.0,
        "velocity_60s": float(_velocity_count(req.customer.id)),
        "is_agent": 1.0 if req.initiator == "agent" else 0.0,
        "agent_verified": 1.0 if (req.initiator == "agent" and req.agent_verified) else 0.0,
        "is_wallet": 1.0 if req.payment_method == "wallet" else 0.0,
        "is_card": 1.0 if req.payment_method == "card" else 0.0,
        "digital_goods": 1.0 if req.merchant_id == "garena" else 0.0,
        "channel_api": 1.0 if req.channel == "api" else 0.0,
    }


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _noise(payment_id: str, salt: str) -> float:
    """Deterministic pseudo-random in [0,1) so a payment always scores the same."""
    h = hashlib.sha256(f"{salt}:{payment_id}".encode()).digest()
    return int.from_bytes(h[:4], "big") / 2**32


# --- models -----------------------------------------------------------------
def _infer_v2(req: ScoreRequest, f: dict) -> tuple[float, list[str]]:
    reasons = []
    z = -3.2
    z += 0.9 * min(f["amount_ratio"], 6.0) * 0.35
    if f["amount_ratio"] > 4:
        reasons.append("amount_unusual_for_merchant")
    if f["country_mismatch"]:
        z += 1.1
        reasons.append("issuer_country_mismatch")
    if f["high_risk_bin"]:
        z += 3.4
        reasons.append("high_risk_bin")
    if f["velocity_60s"] > 5:
        z += 0.6 * min(f["velocity_60s"] - 5, 10)
        reasons.append("velocity_anomaly")
    if f["digital_goods"]:
        z += 0.6
    # The production model was trained before agentic commerce existed: machine
    # initiated traffic looks like a bot to it. (Act 4 storyline.) A verified agent
    # (AMP task mandate + Know-Your-Agent rating, enforced by checkout-api) is the one
    # signal it has been taught to trust — "KYA as a feature".
    if f["is_agent"] and f["agent_verified"]:
        z += 0.6
        reasons.append("verified_agent")
    elif f["is_agent"]:
        z += 4.0
        reasons.append("bot_like_pattern")
    z += (_noise(req.payment_id, "v2") - 0.5) * 2.4
    return _sigmoid(z), reasons


def _infer_v3(req: ScoreRequest, f: dict) -> tuple[float, list[str]]:
    # "Bigger model": burn CPU proportional to a feature-count knob so
    # AlwaysOn Profiling shows this function as the hot spot.
    work = chaos.param("risk_model_drift", "cpu_work", 1200000)
    acc = 0.0
    for i in range(int(work)):
        acc += math.sin(i * 0.001) * math.cos(i * 0.0007)
    score, reasons = _infer_v2(req, f)
    z = math.log(score / (1 - score)) + acc * 1e-9
    # Drift: v3 over-penalises wallets in IDR/PHP -> false declines.
    if f["is_wallet"] and req.currency in ("IDR", "PHP"):
        z += 2.6
        reasons.append("wallet_geo_risk_v3")
    return _sigmoid(z), reasons


def _pick_model(payment_id: str) -> str:
    if chaos.enabled("risk_model_drift"):
        pct = chaos.param("risk_model_drift", "canary_pct", 20)
        if _noise(payment_id, "canary") * 100 < pct:
            return chaos.param("risk_model_drift", "model_version", "v3")
    return "v2"


# --- API --------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "flags": chaos.snapshot()}


@app.post("/v1/score", response_model=ScoreResponse)
def score(req: ScoreRequest):
    started = time.perf_counter()
    span = trace.get_current_span()
    span.set_attributes(
        {
            "merchant.id": req.merchant_id,
            "payment.id": req.payment_id,
            "payment.method": req.payment_method,
            "payment.currency": req.currency,
            "payment.initiator": req.initiator,
            "agent.verified": bool(req.agent_verified),
        }
    )

    model_version = _pick_model(req.payment_id)
    features = _features(req)

    with tracer.start_as_current_span("risk.model.infer") as infer_span:
        infer_span.set_attributes({"risk.model_version": model_version, "risk.feature_count": len(features)})
        t0 = time.perf_counter()
        if model_version == "v3":
            s, reasons = _infer_v3(req, features)
        else:
            s, reasons = _infer_v2(req, features)
        infer_ms = (time.perf_counter() - t0) * 1000
        infer_span.set_attribute("risk.inference_ms", round(infer_ms, 2))

    decision = "deny" if s >= DENY_THRESHOLD else "review" if s >= REVIEW_THRESHOLD else "allow"
    span.set_attributes(
        {
            "risk.model_version": model_version,
            "risk.score": round(s, 4),
            "risk.decision": decision,
            "risk.reasons": reasons,
        }
    )
    dims = {"risk.model_version": model_version, "payment.initiator": req.initiator, "payment.method": req.payment_method}
    score_hist.record(s, dims)
    inference_hist.record(infer_ms, {"risk.model_version": model_version})
    decision_counter.add(1, {**dims, "risk.decision": decision})

    fields = {
        "payment.id": req.payment_id, "merchant.id": req.merchant_id, "payment.method": req.payment_method,
        "payment.currency": req.currency, "payment.initiator": req.initiator, "customer.country": req.customer.country,
        "risk.model_version": model_version, "risk.score": round(s, 4), "risk.decision": decision, "risk.reasons": reasons,
        "risk.inference_ms": round(infer_ms, 1), "risk.feature_count": len(features),
    }
    if model_version != "v2":
        fields["risk.canary_pct"] = chaos.param("risk_model_drift", "canary_pct", 20)
    # One line per inference: the SLO breach is the Act 2 story (it names the canary model
    # version), a canary that is fast enough is an INFO line, v2 within SLO stays quiet.
    if infer_ms > INFERENCE_SLO_MS:
        log_event(log, logging.WARNING, "risk.inference_slow",
                  f"model {model_version} inference took {infer_ms:.0f} ms (SLO {INFERENCE_SLO_MS:.0f} ms) for payment {req.payment_id}: {decision} ({', '.join(reasons) or 'no flags'})",
                  **fields, **{"risk.inference_slo_ms": INFERENCE_SLO_MS})
    elif model_version != "v2":
        log_event(log, logging.INFO, "risk.canary_inference",
                  f"canary model {model_version} scored payment {req.payment_id} in {infer_ms:.0f} ms: {decision} ({', '.join(reasons) or 'no flags'})", **fields)
    if decision == "deny":
        log_event(log, logging.WARNING, "risk.declined",
                  f"model {model_version} denied payment {req.payment_id} for {req.merchant_id}: score {s:.3f} ({', '.join(reasons)})", **fields)
    elif decision == "review":
        log_event(log, logging.INFO, "risk.review",
                  f"model {model_version} sent payment {req.payment_id} for {req.merchant_id} to manual review: score {s:.3f}", **fields)
    else:
        log_event(log, logging.DEBUG, "risk.scored", f"model {model_version} allowed payment {req.payment_id}: score {s:.3f}", **fields)
    return ScoreResponse(
        score=round(s, 4),
        decision=decision,
        model_version=model_version,
        reasons=reasons,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )
