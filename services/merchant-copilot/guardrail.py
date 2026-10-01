"""A deliberately simple guardrail for the workshop.

Two checks:
  * input  — does the merchant's question look like a prompt-injection attempt?
  * action — is the agent allowed to execute this tool with these arguments?

In production this is where Cisco AI Defense (Splunk "AI Security Monitoring")
or Splunk Agent Observability's Luna guardrails would sit; both attach their
verdicts to the same OpenTelemetry spans. The regexes below stand in for that so
the storyline works without extra licences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

INJECTION_PATTERNS = [
    (r"ignore (all )?(previous|prior|above) (instructions|rules)", "instruction_override"),
    (r"disregard (the )?(system|previous|your) (prompt|instructions)", "instruction_override"),
    (r"you are now (an?|the) ", "role_hijack"),
    (r"developer mode|jailbreak|do anything now", "role_hijack"),
    (r"(reveal|print|show|repeat) (your|the) (system prompt|instructions)", "prompt_leak"),
    (r"\b(refund|transfer|move|send|pay out|payout)\b.*\b(all|every|entire|full|maximum)\b", "unauthorised_money_movement"),
    (r"\brefund\b.{0,60}\b(to|into) (my|this|the following|account|wallet)\b", "unauthorised_money_movement"),
    (r"as (the )?(admin|administrator|tuktukpay staff|support agent),? (i|we) (authorise|authorize|approve)", "authority_spoof"),
]

# Tools the copilot may execute on behalf of a merchant user. Anything else is blocked.
ALLOWED_TOOLS = {"get_payment", "list_recent_declines", "explain_decline_code", "get_merchant_summary"}
SENSITIVE_TOOLS = {"refund_payment": "money_movement_requires_human_approval"}


@dataclass
class Verdict:
    allowed: bool
    verdict: str  # pass | flag | block
    rules: list[str] = field(default_factory=list)


def check_input(question: str) -> Verdict:
    q = question.lower()
    hits = [name for pattern, name in INJECTION_PATTERNS if re.search(pattern, q)]
    if hits:
        # Flag, don't refuse: we still want to see what the model does with it.
        return Verdict(allowed=True, verdict="flag", rules=sorted(set(hits)))
    return Verdict(allowed=True, verdict="pass")


def check_action(tool_name: str, arguments: dict, input_verdict: Verdict) -> Verdict:
    if tool_name in ALLOWED_TOOLS:
        return Verdict(allowed=True, verdict="pass")
    if tool_name in SENSITIVE_TOOLS:
        rules = [SENSITIVE_TOOLS[tool_name]]
        if input_verdict.verdict == "flag":
            rules.append("prompt_injection_suspected")
        return Verdict(allowed=False, verdict="block", rules=rules)
    return Verdict(allowed=False, verdict="block", rules=["unknown_tool"])
