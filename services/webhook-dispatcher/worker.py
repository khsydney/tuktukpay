"""TukTukPay webhook-dispatcher — async worker that delivers payment events to
merchants' webhook endpoints (Python).

Consumes the `payments.events` Redis stream written by checkout-api. The
producer put the W3C `traceparent` inside the message, so the worker continues
the SAME trace: in Splunk APM one payment trace spans the synchronous API call
AND the asynchronous webhook delivery (with retries) seconds later.

Run with `opentelemetry-instrument python worker.py` (Splunk distro). Redis and
httpx calls are auto-instrumented; the consumer span is created by hand because
stream consumption has no framework hook.
"""

from __future__ import annotations

import json
import logging
import os
import random
import socket
import time

import httpx
import redis
from opentelemetry import context, metrics, trace
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, Status, StatusCode

from chaos import ChaosFlags

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("webhook-dispatcher")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is too chatty for Log Observer

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STREAM = os.getenv("EVENT_STREAM", "payments.events")
GROUP = os.getenv("CONSUMER_GROUP", "webhook-dispatchers")
CONSUMER = os.getenv("CONSUMER_NAME", socket.gethostname())
MERCHANT_SIM_URL = os.getenv("MERCHANT_SIM_URL", "http://localhost:8085").rstrip("/")
MAX_ATTEMPTS = int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "3"))
BACKOFF_S = [0.5, 1.0, 2.0]

tracer = trace.get_tracer("tuktukpay.webhook-dispatcher")
meter = metrics.get_meter("tuktukpay.webhook-dispatcher")
deliveries = meter.create_counter("tuktukpay.webhooks.deliveries", description="Webhook delivery attempts by outcome")
delivery_latency = meter.create_histogram("tuktukpay.webhooks.latency_ms", unit="ms")
stream_lag = meter.create_histogram("tuktukpay.webhooks.stream_lag_ms", unit="ms", description="Time from payment event to delivery start")

chaos = ChaosFlags()  # not used for injection here (merchant-sim does that) but handy for /debug
http_client = httpx.Client(timeout=httpx.Timeout(4.0))


def ensure_group(r: redis.Redis):
    try:
        r.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
        log.info("created consumer group %s on %s", GROUP, STREAM)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def deliver(event: dict) -> tuple[bool, int]:
    """POST the event to the merchant. Returns (delivered, attempts)."""
    merchant_id = event.get("merchant_id", "unknown")
    url = f"{MERCHANT_SIM_URL}/webhooks/{merchant_id}"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with tracer.start_as_current_span("webhook.deliver", kind=SpanKind.INTERNAL) as span:
            span.set_attributes({"webhook.attempt": attempt, "merchant.id": merchant_id, "webhook.url": url, "payment.id": event.get("payment_id", "")})
            t0 = time.perf_counter()
            try:
                res = http_client.post(url, json=event, headers={"X-TukTukPay-Event": event.get("type", "payment.unknown"), "X-TukTukPay-Attempt": str(attempt)})
                ms = (time.perf_counter() - t0) * 1000
                span.set_attribute("http.response.status_code", res.status_code)
                delivery_latency.record(ms, {"merchant.id": merchant_id})
                if res.status_code < 300:
                    deliveries.add(1, {"merchant.id": merchant_id, "outcome": "delivered", "webhook.attempt": attempt})
                    return True, attempt
                span.set_status(Status(StatusCode.ERROR, f"merchant returned {res.status_code}"))
                deliveries.add(1, {"merchant.id": merchant_id, "outcome": "failed", "webhook.attempt": attempt})
                log.warning("webhook to %s failed attempt=%d status=%d payment_id=%s", merchant_id, attempt, res.status_code, event.get("payment_id"))
            except httpx.HTTPError as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                deliveries.add(1, {"merchant.id": merchant_id, "outcome": "error", "webhook.attempt": attempt})
                log.warning("webhook to %s errored attempt=%d payment_id=%s error=%s", merchant_id, attempt, event.get("payment_id"), exc)
        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)] * (0.8 + random.random() * 0.4))
    return False, MAX_ATTEMPTS


def handle(r: redis.Redis, msg_id: str, fields: dict):
    event = json.loads(fields.get("event", "{}"))
    # Continue the producer's trace: traceparent/tracestate travelled in the message.
    carrier = {k: v for k, v in fields.items() if k in ("traceparent", "tracestate", "baggage")}
    parent_ctx = extract(carrier)
    token = context.attach(parent_ctx)
    try:
        with tracer.start_as_current_span(
            f"process {STREAM}",
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "redis",
                "messaging.destination.name": STREAM,
                "messaging.operation.type": "process",
                "messaging.message.id": msg_id,
                "messaging.consumer.group.name": GROUP,
                "merchant.id": event.get("merchant_id", "unknown"),
                "payment.id": event.get("payment_id", ""),
                "payment.outcome": event.get("status", ""),
            },
        ) as span:
            try:
                occurred = event.get("occurred_at")
                if occurred:
                    from datetime import datetime, timezone

                    lag_ms = (datetime.now(timezone.utc) - datetime.fromisoformat(occurred.replace("Z", "+00:00"))).total_seconds() * 1000
                    stream_lag.record(max(lag_ms, 0), {"merchant.id": event.get("merchant_id", "unknown")})
                    span.set_attribute("messaging.lag_ms", round(lag_ms, 1))
            except Exception:  # noqa: BLE001
                pass

            delivered, attempts = deliver(event)
            span.set_attributes({"webhook.delivered": delivered, "webhook.attempts": attempts})
            if not delivered:
                span.set_status(Status(StatusCode.ERROR, "webhook dead-lettered"))
                r.xadd(f"{STREAM}.dlq", {"event": fields.get("event", "{}"), "attempts": attempts, "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, maxlen=10000, approximate=True)
                deliveries.add(1, {"merchant.id": event.get("merchant_id", "unknown"), "outcome": "dead_letter", "webhook.attempt": attempts})
                log.error("webhook dead-lettered payment_id=%s merchant=%s after %d attempts", event.get("payment_id"), event.get("merchant_id"), attempts)
            r.xack(STREAM, GROUP, msg_id)
    finally:
        context.detach(token)


def main():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    while True:
        try:
            ensure_group(r)
            break
        except redis.RedisError as exc:
            log.warning("redis not ready (%s); retrying", exc)
            time.sleep(2)
    log.info("webhook-dispatcher consuming %s as %s/%s -> %s", STREAM, GROUP, CONSUMER, MERCHANT_SIM_URL)
    while True:
        try:
            batches = r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=16, block=2000)
        except redis.RedisError as exc:
            log.warning("redis read failed (%s); retrying", exc)
            time.sleep(1)
            continue
        for _stream, messages in batches or []:
            for msg_id, fields in messages:
                try:
                    handle(r, msg_id, fields)
                except Exception:  # noqa: BLE001
                    log.exception("failed to process %s", msg_id)
                    r.xack(STREAM, GROUP, msg_id)


if __name__ == "__main__":
    main()
