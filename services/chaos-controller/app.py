"""TukTukPay chaos-controller — the facilitator's control panel.

Holds the failure-injection flags every service polls (GET /flags) and exposes
one-click "Act" presets for the workshop storyline. Deliberately NOT
instrumented: it is workshop tooling, not part of the payment platform.
"""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="TukTukPay chaos-controller", version="1.0.0")

# name -> definition. `params` are the defaults a service reads when enabled.
FLAG_DEFINITIONS: dict[str, dict] = {
    "festival_spike": {
        "title": "11.11 festival traffic spike",
        "consumer": "loadgen (merchant-storefront)",
        "description": "Multiplies the base request rate. 2C2P sees ~30x on shopping festivals.",
        "params": {"multiplier": 10},
    },
    "acquirer_kbank_timeout": {
        "title": "Acquirer KBank hangs for one card BIN",
        "consumer": "acquirer-sim",
        "description": "acq-kbank (Kasikornbank acquiring) hangs for cards whose BIN starts with bin_prefix in THB. "
        "payment-router times out at 2.5s and fails over to acq-uob (UOB regional, booked in Singapore), whose approval rate on domestic Thai cards is poor.",
        "params": {"bin_prefix": "457173", "currency": "THB", "hang_ms": 3200},
    },
    "risk_model_drift": {
        "title": "Risk model v3 canary (slow + biased)",
        "consumer": "risk-engine",
        "description": "Routes canary_pct% of scoring to model v3: CPU-heavy (cpu_work loop) and over-declines wallet payments in IDR/PHP.",
        "params": {"canary_pct": 20, "model_version": "v3", "cpu_work": 1200000},
    },
    "copilot_tool_loop": {
        "title": "Copilot tool-call loop",
        "consumer": "merchant-copilot",
        "description": "The LLM agent keeps re-calling get_payment until max_iterations; tokens and latency balloon.",
        "params": {"iterations": 10},
    },
    "copilot_prompt_injection": {
        "title": "Prompt-injection attempts against the copilot",
        "consumer": "loadgen (merchant-storefront)",
        "description": "Every Nth copilot question carries an injected instruction that tries to trigger refund_payment. The guardrail must block it.",
        "params": {"every_n": 4},
    },
    "agent_traffic_surge": {
        "title": "AI-agent initiated checkout surge",
        "consumer": "loadgen (merchant-storefront)",
        "description": "Raises the share of payments carrying agent headers (Visa TAP / Mastercard Agent Pay / Ant AMP / ACP). "
        "AMP agents present a wallet-issued task mandate that checkout-api verifies; the v2 risk model treats unverified agents as bots.",
        "params": {"agent_share": 0.25, "amp_share": 0.5},
    },
    "agent_mandate_violations": {
        "title": "Agents violating their task mandates",
        "consumer": "loadgen (merchant-storefront)",
        "description": "Share of AMP agent payments that break the pre-approval (over the limit, wrong merchant, expired, replayed, forged token, unregistered agent). checkout-api must block every one.",
        "params": {"rate": 0.15},
    },
    "catalog_prompt_injection": {
        "title": "Poisoned product listing (indirect prompt injection)",
        "consumer": "merchant-sim",
        "description": "One merchant's catalog contains a listing whose description instructs AI shopping agents to buy 3 units and add a gift card. Gullible agents obey; the mandate ceiling stops them.",
        "params": {"merchant_id": "lazada-th"},
    },
    "kya_registry_down": {
        "title": "Know-Your-Agent registry unavailable",
        "consumer": "wallet-sim",
        "description": "The wallet's KYA lookup returns 503. checkout-api fails closed: every AMP agent payment is blocked with mandate_kya_unavailable — an AI-governance dependency incident.",
        "params": {},
    },
    "router_failover_disabled": {
        "title": "Disable acquirer failover",
        "consumer": "payment-router",
        "description": "payment-router stops retrying on the backup acquirer. Combine with the KBank hang to turn a latency problem into hard declines.",
        "params": {},
    },
    "ledger_db_slow": {
        "title": "Ledger database contention",
        "consumer": "ledger",
        "description": "Shrinks the connection pool and adds pg_sleep to inserts: pool wait + slow INSERT visible in Database Query Performance.",
        "params": {"sleep_ms": 250, "pool_size": 2},
    },
    "webhook_merchant_flaky": {
        "title": "Merchant webhook endpoint flaky",
        "consumer": "merchant-sim",
        "description": "The merchant's webhook endpoint returns 500 for a share of deliveries; webhook-dispatcher retries with backoff.",
        "params": {"merchant_id": "lazada-th", "fail_rate": 0.4, "slow_ms": 2500},
    },
    # ---- Kubernetes acts: incidents the cluster itself notices (restarts, OOMKilled, CrashLoopBackOff),
    #      so the AI Troubleshooting Agent can produce a Remediation Plan (Kubernetes alerts only).
    "ledger_memory_leak": {
        "title": "Ledger memory leak (OOMKilled on Kubernetes)",
        "consumer": "ledger",
        "description": "The ledger retains mb_per_second of buffers per second, like a cache without eviction after a bad release. "
        "With the Kubernetes memory limit (384Mi) the kernel OOM-kills the container every minute or so and the pod restarts; "
        "on Docker Compose the leak stops at max_mb.",
        "params": {"mb_per_second": 6, "max_mb": 1536},
    },
    "wallet_crash_loop": {
        "title": "Bad rollout: wallet-sim crash-loops (CrashLoopBackOff)",
        "consumer": "wallet-sim",
        "description": "wallet-sim exits a few seconds after start as if a schema migration in a new release failed. Kubernetes restarts "
        "it into CrashLoopBackOff; Know-Your-Agent lookups fail and every AMP agent payment fails closed.",
        "params": {"exit_code": 3, "release": "wallet-sim 1.7.0"},
    },
}

# One-click presets for the storyline. Everything not listed is turned off.
SCENARIOS: dict[str, dict] = {
    "baseline": {"title": "Baseline — everything healthy", "flags": {}},
    "act1": {
        "title": "Act 1 — The one-in-ten-thousand decline (festival spike + acquirer BIN hang)",
        "flags": {"festival_spike": {"multiplier": 10}, "acquirer_kbank_timeout": {}},
    },
    "act2": {
        "title": "Act 2 — The model that drifted (risk v3 canary)",
        "flags": {"festival_spike": {"multiplier": 3}, "risk_model_drift": {}},
    },
    "act3": {
        "title": "Act 3 — The copilot bill (tool loop + prompt injection)",
        "flags": {"copilot_tool_loop": {}, "copilot_prompt_injection": {}},
    },
    "act4": {
        "title": "Act 4 — Agents are buying (agent surge + mandate violations + poisoned listing)",
        "flags": {"agent_traffic_surge": {"agent_share": 0.25}, "agent_mandate_violations": {"rate": 0.15}, "catalog_prompt_injection": {}, "festival_spike": {"multiplier": 3}},
    },
    "act4b": {
        "title": "Act 4b — The KYA registry goes down (governance dependency)",
        "flags": {"agent_traffic_surge": {"agent_share": 0.25}, "kya_registry_down": {}},
    },
    "act5": {
        "title": "Act 5 — Ask, don't dig (ledger DB contention + flaky webhooks)",
        "flags": {"ledger_db_slow": {}, "webhook_merchant_flaky": {}},
    },
    "act6": {
        "title": "Act 6 — The leaky ledger (memory leak → OOMKilled → pod restarts; Kubernetes)",
        "flags": {"ledger_memory_leak": {}, "festival_spike": {"multiplier": 2}},
    },
    "act7": {
        "title": "Act 7 — The bad rollout (wallet-sim CrashLoopBackOff → agents fail closed; Kubernetes)",
        "flags": {"agent_traffic_surge": {"agent_share": 0.25}, "wallet_crash_loop": {}},
    },
}

STATE_FILE = Path(os.getenv("CHAOS_STATE_FILE", "/tmp/chaos-state.json"))


def _fresh_state() -> dict:
    return {name: {"enabled": False, "params": copy.deepcopy(d["params"])} for name, d in FLAG_DEFINITIONS.items()}


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text())
            state = _fresh_state()
            for k, v in saved.items():
                if k in state:
                    state[k]["enabled"] = bool(v.get("enabled", False))
                    state[k]["params"].update(v.get("params", {}))
            return state
        except Exception:  # noqa: BLE001
            pass
    return _fresh_state()


STATE = _load()
HISTORY: list[dict] = []
# Last preset started from the panel, so the act pages can show "running now". Cleared by
# /reset and by any manual flag change (the state no longer matches the preset).
CURRENT_SCENARIO: dict = {"name": None}

# Traffic on/off for the load generator. Kept outside STATE on purpose: /reset and individual
# flag changes never touch it. Starting a scenario DOES resume paused traffic — an act with the
# traffic switched off shows nothing in Splunk, which every facilitator so far has read as "the
# button does not work" — and says so in the history and in the response (`traffic_resumed`).
# Published to services as a pseudo-flag `traffic_paused` in GET /flags.
TRAFFIC_FILE = STATE_FILE.with_name("traffic-state.json")


def _load_traffic() -> dict:
    try:
        return {"paused": bool(json.loads(TRAFFIC_FILE.read_text()).get("paused", False))}
    except Exception:  # noqa: BLE001
        return {"paused": False}


TRAFFIC = _load_traffic()


def _save():
    try:
        STATE_FILE.write_text(json.dumps(STATE))
    except Exception:  # noqa: BLE001
        pass


def _published_flags() -> dict:
    return {**STATE, "traffic_paused": {"enabled": TRAFFIC["paused"], "params": {}}}


def _log(action: str, detail: dict):
    HISTORY.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, **detail})
    del HISTORY[:-200]


class FlagUpdate(BaseModel):
    enabled: bool | None = None
    params: dict | None = None


class TrafficUpdate(BaseModel):
    paused: bool


@app.get("/healthz")
def healthz():
    return {"status": "ok", "traffic_paused": TRAFFIC["paused"], "active": [k for k, v in STATE.items() if v["enabled"]]}


@app.get("/flags")
def get_flags():
    return _published_flags()


@app.get("/traffic")
def get_traffic():
    return TRAFFIC


def _set_traffic(paused: bool, by: str) -> None:
    TRAFFIC["paused"] = paused
    try:
        TRAFFIC_FILE.write_text(json.dumps(TRAFFIC))
    except Exception:  # noqa: BLE001
        pass
    _log("traffic", {"flag": "paused" if paused else "resumed", "by": by})


@app.post("/traffic")
def set_traffic(update: TrafficUpdate):
    _set_traffic(update.paused, by="traffic button")
    return TRAFFIC


@app.get("/flags/definitions")
def get_definitions():
    return FLAG_DEFINITIONS


@app.post("/flags/{name}")
def set_flag(name: str, update: FlagUpdate):
    if name not in STATE:
        raise HTTPException(404, f"unknown flag {name}")
    if update.enabled is not None:
        STATE[name]["enabled"] = update.enabled
    if update.params:
        STATE[name]["params"].update(update.params)
    CURRENT_SCENARIO["name"] = None
    _save()
    _log("flag", {"flag": name, "state": STATE[name]})
    return STATE[name]


@app.post("/reset")
def reset():
    global STATE  # noqa: PLW0603
    STATE = _fresh_state()
    CURRENT_SCENARIO["name"] = None
    _save()
    _log("reset", {})
    return STATE


@app.get("/scenarios")
def scenarios():
    return {k: v["title"] for k, v in SCENARIOS.items()}


@app.get("/scenarios/current")
def current_scenario():
    return CURRENT_SCENARIO


@app.post("/scenarios/{name}")
def run_scenario(name: str):
    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario {name}")
    for flag in STATE.values():
        flag["enabled"] = False
    for flag, params in SCENARIOS[name]["flags"].items():
        STATE[flag]["enabled"] = True
        STATE[flag]["params"] = {**copy.deepcopy(FLAG_DEFINITIONS[flag]["params"]), **params}
    CURRENT_SCENARIO["name"] = name
    _save()
    _log("scenario", {"scenario": name})
    resumed = TRAFFIC["paused"]
    if resumed:
        _set_traffic(False, by=f"start of {name}")
    return {"scenario": name, "title": SCENARIOS[name]["title"], "flags": _published_flags(),
            "traffic": dict(TRAFFIC), "traffic_resumed": resumed}


@app.get("/history")
def history():
    return HISTORY[-50:]


@app.get("/", response_class=HTMLResponse)
def ui():
    return (Path(__file__).parent / "ui.html").read_text()


@app.get("/act/{name}", response_class=HTMLResponse)
def act_page(name: str):
    """Picture-first explainer for one storyline preset (for audiences reading in a second language)."""
    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario {name}")
    return (Path(__file__).parent / "act.html").read_text()
