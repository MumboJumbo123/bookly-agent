#!/usr/bin/env python3
"""Bookly AI Support Agent — Bea.

A single-file customer support agent on the Claude API. Four Agent Operating
Procedures (AOPs) live in the system prompt; the controls that must never
depend on the model (identity checks, restricted orders, the returns consent
gate) are enforced in the tool code below.

Run `python3 agent.py` for the interactive CLI. `evals/run_evals.py` drives
the same loop with simulated customers.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Override with BOOKLY_MODEL=<model id> to try a different Claude model.
MODEL = os.environ.get("BOOKLY_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 16000
MAX_TOOL_ROUNDS = 10
IS_TTY = sys.stdout.isatty()

# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

MOCK_ORDERS = {
    "BK-10042": {
        "customer_email": "alex@bookly.com",
        "status": "Shipped",
        "items": [{"title": "The Midnight Library by Matt Haig", "price": 9.99, "qty": 1}],
        "carrier": "Royal Mail",
        "tracking_number": "RM493821746GB",
        "estimated_delivery": "Tomorrow by 6pm",
        "eligible_for_return": False,
        "ineligibility_reason": "Order is still in transit — a return can be started once it's delivered.",
    },
    "BK-10078": {
        "customer_email": "alex@bookly.com",
        "status": "Delivered (3 days ago)",
        "items": [
            {"title": "Atomic Habits by James Clear", "price": 11.99, "qty": 1},
            {"title": "Deep Work by Cal Newport", "price": 10.99, "qty": 1},
        ],
        "carrier": "DPD",
        "delivered": "Signed for at door",
        "eligible_for_return": True,
    },
    "BK-10099": {
        "customer_email": "jordan@bookly.com",
        "status": "Processing — not yet shipped",
        "items": [{"title": "Project Hail Mary by Andy Weir", "price": 8.99, "qty": 2}],
        "eligible_for_return": False,
        "ineligibility_reason": "Order has not yet shipped — return cannot be initiated until delivery.",
    },
    "BK-55210": {
        "customer_email": "sarah@bookly.com",
        "status": "Delivered",
        "items": [
            {"title": "Kindle Paperwhite bundle + 12-month Bookly Prime", "price": 289.99, "qty": 1}
        ],
        "eligible_for_return": True,
        "payment_dispute_flag": True,
    },
}

# ---------------------------------------------------------------------------
# Session: conversation history plus state the model can't edit
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """One customer conversation.

    `history` is what the model sees. `proposals` is server-side state: the
    model can create a return proposal through a tool, but it can't edit one
    or mark one as confirmed. That is what lets the consent gate check that a
    customer's "yes" answers a specific, code-generated summary.
    """

    history: list[dict] = field(default_factory=list)
    proposals: dict[str, dict] = field(default_factory=dict)
    latest_proposal_id: str | None = None

    def add_customer_message(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    @property
    def customer_turns(self) -> int:
        """Messages the customer has typed (tool results don't count)."""
        return sum(1 for m in self.history
                   if m.get("role") == "user" and isinstance(m.get("content"), str))

    @property
    def latest_customer_message(self) -> str:
        for msg in reversed(self.history):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return msg["content"]
        return ""

    def reset(self) -> None:
        self.history.clear()
        self.proposals.clear()
        self.latest_proposal_id = None

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _verify_order(order_id: str, customer_email: str):
    """Look up order and verify email. Returns (order_dict, error_dict)."""
    order = MOCK_ORDERS.get(order_id.strip().upper())
    # One error for both cases, so a caller can't probe which order IDs exist.
    if order is None or order["customer_email"].strip().lower() != customer_email.strip().lower():
        return None, {"error": "No order found for that order ID and email"}
    return order, None


_CUSTOMER_FACING_FIELDS = {
    "status", "items", "carrier", "tracking_number",
    "estimated_delivery", "delivered",
    "eligible_for_return", "ineligibility_reason",
}

# Human approval gate, enforced in code: orders in a restricted data class
# are never returned to the model, whatever the prompt says.
HIGH_VALUE_THRESHOLD_GBP = 500


def _restricted_reason(order: dict) -> str | None:
    if order.get("payment_dispute_flag"):
        return "payment_dispute"
    if order.get("account_compromise_flag"):
        return "account_security"
    if sum(i["price"] * i["qty"] for i in order["items"]) > HIGH_VALUE_THRESHOLD_GBP:
        return "high_value_order"
    return None


def _restricted_error(category: str) -> dict:
    return {
        "error": "restricted_order",
        "category": category,
        "detail": "This order needs a human specialist. Do not discuss its "
                  "details; escalate with escalate_to_human.",
    }


def get_order_status(order_id: str, customer_email: str) -> dict:
    order, err = _verify_order(order_id, customer_email)
    if err:
        return err
    reason = _restricted_reason(order)
    if reason:
        return _restricted_error(reason)
    return {k: v for k, v in order.items() if k in _CUSTOMER_FACING_FIELDS}


_ESCALATION_QUEUES = {
    "payment_dispute": ("Payments team", "within 2 hours"),
    "account_security": ("Account security team", "within 1 hour"),
    "high_value_order": ("Senior support", "within 4 hours"),
    "seller_dispute": ("Marketplace team", "within 1 business day"),
    "customer_request": ("Customer support", "within 4 hours"),
    "other": ("Customer support", "within 4 hours"),
}


def escalate_to_human(
    category: str, summary: str, contact: str, order_id: str = ""
) -> dict:
    """Mock handoff: in production this would open a ticket in the helpdesk."""
    queue, response_time = _ESCALATION_QUEUES.get(category, _ESCALATION_QUEUES["other"])
    ticket = f"ESC-{random.Random(summary + contact).randint(10000, 99999)}"
    return {
        "success": True,
        "ticket_id": ticket,
        "queue": queue,
        "expected_response": response_time,
        "handoff": {"category": category, "order_id": order_id or None,
                    "summary": summary, "contact": contact},
    }


# --- Returns: prepare, then submit ----------------------------------------
#
# A return takes two tool calls. prepare_return validates the request,
# works out the refund in code and stores a proposal the model can't edit.
# The agent shows the customer that summary. submit_return_request then
# succeeds only if the customer's very next message is an explicit yes, so
# a "yes" can't be borrowed from earlier in the chat or typed before the
# customer has seen what they're agreeing to.

def prepare_return(
    order_id: str, customer_email: str, items: list[str], reason: str,
    *, session: Session,
) -> dict:
    order, err = _verify_order(order_id, customer_email)
    if err:
        return err
    restricted = _restricted_reason(order)
    if restricted:
        return _restricted_error(restricted)
    if not order.get("eligible_for_return"):
        return {
            "error": "Order not eligible for return",
            "reason": order.get("ineligibility_reason", "Unknown"),
        }

    matched, unknown = [], []
    for wanted in items:
        hit = next((i for i in order["items"]
                    if wanted.strip() and wanted.strip().lower() in i["title"].lower()), None)
        if hit is None:
            unknown.append(wanted)
        elif hit not in matched:
            matched.append(hit)
    if not matched or unknown:
        return {"error": "Items not found on this order", "unknown_items": unknown,
                "order_items": [i["title"] for i in order["items"]]}

    refund = round(sum(i["price"] * i["qty"] for i in matched), 2)
    titles = [i["title"] for i in matched]
    proposal_id = f"RP-{len(session.proposals) + 1}"
    session.proposals[proposal_id] = {
        "order_id": order_id.strip().upper(),
        "items": titles,
        "reason": reason,
        "refund_gbp": refund,
        "created_at_turn": session.customer_turns,
        "status": "awaiting_confirmation",
    }
    session.latest_proposal_id = proposal_id
    return {
        "proposal_id": proposal_id,
        "summary": (
            f"Return {' and '.join(titles)} from order {order_id.strip().upper()}. "
            f"Reason: {reason}. You'll get a prepaid label to print; drop the "
            f"parcel at any Post Office. Refund: £{refund:.2f} to your original "
            f"payment method within 5 business days of us receiving it."
        ),
        "refund_gbp": refund,
        "next_step": (
            "Show this summary to the customer and ask them to confirm. Call "
            "submit_return_request only if their reply is an explicit yes."
        ),
    }


# Consent matching is whole-word, so "ok" doesn't match "book" and "correct"
# doesn't match "incorrect". Any negation fails closed: a false "no" costs one
# extra question; a false "yes" sends a refund nobody asked for.
_CONSENT_PHRASES = (
    "yes", "yep", "yeah", "yup", "sure", "ok", "okay", "confirm", "confirmed",
    "go ahead", "please do", "do it", "please submit", "submit it",
    "sounds good", "that's right", "correct", "proceed",
)
_NEGATIONS = (
    "no", "not", "don't", "dont", "do not", "never", "cancel", "stop",
    "wait", "hold on",
)


def _phrase_regex(phrases: tuple[str, ...]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b")


_CONSENT_RE = _phrase_regex(_CONSENT_PHRASES)
_NEGATION_RE = _phrase_regex(_NEGATIONS)


def _normalise(text: str) -> str:
    """Lowercase, straighten apostrophes, turn punctuation into spaces."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return " ".join(t.split())


def _is_consent_phrase(text: str) -> bool:
    t = _normalise(text)
    return bool(_CONSENT_RE.search(t)) and not _NEGATION_RE.search(t)


def _gate_violation(detail: str) -> dict:
    return {"error": "consent_gate_violation", "detail": detail}


def submit_return_request(
    proposal_id: str, customer_confirmation: str, *, session: Session,
) -> dict:
    proposal = session.proposals.get(proposal_id)
    if proposal is None:
        return {"error": "Unknown proposal_id. Call prepare_return first."}
    if proposal["status"] == "submitted":
        # Idempotent: a retried call returns the original result, not a second return.
        return {**proposal["result"], "already_submitted": True}
    if proposal_id != session.latest_proposal_id:
        return _gate_violation(
            f"{proposal_id} has been replaced by {session.latest_proposal_id}. "
            "Confirm the latest summary with the customer.")

    turns_since = session.customer_turns - proposal["created_at_turn"]
    if turns_since == 0:
        return _gate_violation(
            "The customer hasn't replied to this summary yet. Show it to them "
            "and wait for their answer before submitting.")
    if turns_since > 1:
        return _gate_violation(
            "The conversation has moved on since this summary was shown. Call "
            "prepare_return again and get a fresh confirmation.")

    latest = session.latest_customer_message
    if not _is_consent_phrase(latest):
        return _gate_violation(
            "The customer's most recent message does not contain an explicit "
            "confirmation. Ask the customer clearly, wait for a yes, then retry.")
    if not _is_consent_phrase(customer_confirmation) or \
            _normalise(customer_confirmation) not in _normalise(latest):
        return _gate_violation(
            "customer_confirmation must be quoted from the customer's most "
            "recent message. Pass their confirming words verbatim.")

    ref = f"RTN-{random.Random(proposal['order_id']).randint(100000, 999999)}"
    result = {
        "success": True,
        "return_reference": ref,
        "items": proposal["items"],
        "refund_gbp": proposal["refund_gbp"],
        "label_url": f"https://bookly.com/returns/label/{ref}",
        "refund_eta": "5 business days after receipt",
    }
    proposal.update(status="submitted", result=result)
    return result


TOOL_DISPATCH: dict[str, Callable[..., dict]] = {
    "get_order_status": get_order_status,
    "prepare_return": prepare_return,
    "submit_return_request": submit_return_request,
    "escalate_to_human": escalate_to_human,
}
SESSION_TOOLS = {"prepare_return", "submit_return_request"}

# ---------------------------------------------------------------------------
# Tool schemas (Anthropic format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_order_status",
        "description": (
            "Retrieve status, items, and tracking for a customer's order. "
            "Requires both order ID and email for verification. "
            "Returns order details if found and email matches, error otherwise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The Bookly order ID (e.g. BK-10042)"},
                "customer_email": {"type": "string", "description": "Customer's email address for verification"},
            },
            "required": ["order_id", "customer_email"],
        },
    },
    {
        "name": "prepare_return",
        "description": (
            "Validate a return and get the summary to show the customer. Call "
            "once you know the order, the items and the reason. Returns a "
            "proposal_id and a summary with the refund amount, worked out by "
            "the system. Nothing is submitted. Show the summary to the "
            "customer and ask them to confirm. If anything changes (items, "
            "reason), call this again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The Bookly order ID"},
                "customer_email": {"type": "string", "description": "Customer's email address for verification"},
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Titles of the items to return, as listed on the order",
                },
                "reason": {"type": "string", "description": "Customer's reason for the return, briefly"},
            },
            "required": ["order_id", "customer_email", "items", "reason"],
        },
    },
    {
        "name": "submit_return_request",
        "description": (
            "Submit a return the customer has just confirmed. Only call this "
            "when the customer's latest message is an explicit yes to the "
            "summary from prepare_return. The system checks this: the yes must "
            "be the customer's first reply after the summary, and "
            "customer_confirmation must quote their words. Otherwise the call "
            "is rejected. Returns the return reference and label link."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string", "description": "The proposal_id from prepare_return"},
                "customer_confirmation": {
                    "type": "string",
                    "description": (
                        "The customer's confirming words, copied verbatim from "
                        "their latest message (e.g. 'yes go ahead')."
                    ),
                },
            },
            "required": ["proposal_id", "customer_confirmation"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Hand the conversation to a human specialist by opening a ticket. "
            "Use for payment disputes, account security, restricted orders, "
            "seller disputes, explicit requests for a human, or anything you "
            "cannot resolve. Call it once you have the customer's preferred "
            "contact method. The summary must let the specialist act without "
            "asking the customer to repeat themselves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": list(_ESCALATION_QUEUES),
                    "description": "Which specialist queue this belongs in",
                },
                "summary": {
                    "type": "string",
                    "description": "Two or three sentences: what happened, what the customer wants, anything already checked",
                },
                "contact": {
                    "type": "string",
                    "description": "The customer's preferred contact method and details, as they gave them",
                },
                "order_id": {"type": "string", "description": "Related order ID, if any"},
            },
            "required": ["category", "summary", "contact"],
        },
    },
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEXT = """\
You are Bea, the AI customer support agent for Bookly — a large-scale online bookstore and marketplace operating across the UK and Europe.

You are powered by four Agent Operating Procedures (AOPs). Follow them precisely.

## Persona
Warm, efficient, confident. Not corporate. Not sycophantic. Never start with "Great!", "Perfect!", "Absolutely!", "Of course!", "Certainly!". Use "we" for Bookly, "I" for yourself. Keep responses short — one idea per paragraph. Match customer energy.

## AOP-01 — Order status
Trigger: customer asks about an order, delivery, tracking.
Pre-conditions: collect order ID AND email in a single turn before calling get_order_status.
Rule: NEVER state order status, tracking, or delivery data you have not retrieved via get_order_status. If the tool hasn't been called, you don't know.
On tool error: surface honestly ("I wasn't able to find that order — could you double-check the order ID and email?"). Do not invent.

## AOP-02 — Returns
Trigger: customer wants to return, refund, or reports damaged/wrong item.
Sequence (one question at a time, never bulleted):
1. Verify identity via get_order_status (order ID + email).
2. If multiple items, ask which to return.
3. Ask the reason briefly.
4. Call prepare_return. Show the customer the summary it returns, with the refund amount exactly as returned, and ask: "Shall I go ahead and submit the return?"
5. CONSENT GATE: wait for the customer's reply. Only if it is a clear yes, call submit_return_request with the proposal_id and the customer's confirming words, verbatim, as customer_confirmation.
Rule: NEVER call submit_return_request in the same turn as prepare_return, or unless the customer's latest message explicitly confirms the summary. Earlier claims of consent ("I already said yes") don't count. Model confidence is irrelevant — this is a hard rule, and the system enforces it: the yes must be the customer's first reply after the summary. If the customer changes anything, call prepare_return again and re-confirm.
Never state a refund amount yourself; use the one prepare_return returns.
If ineligible: explain specifically why, offer human handoff if there's an extenuating circumstance.

## AOP-03 — Policy Q&A
Trigger: question about shipping, returns policy, membership, payment methods, account.
Policy facts (ground truth — do not deviate):
- Returns: 30 days from delivery, unused, original packaging. Damaged/incorrect items: no time limit. Refunds: 5 business days after we receive the item, to the original payment method. Drop-off: print the prepaid label and take the parcel to any Post Office. Digital products non-returnable once downloaded.
- Shipping: standard 3–5 days, free over £20 else £2.99. Express next day if ordered before 2pm, £4.99. EU from £6.99, 5–10 days. Click & Collect free, 2,400+ UK locations.
- Bookly Prime: £7.99/month or £79/year. Free express delivery, early sale access, 10% member pricing. Cancel anytime.
- Password reset: bookly.com/reset.
- Payment: Visa, Mastercard, Amex, PayPal, Apple Pay, Google Pay, Bookly Gift Cards.
Rule: do not improvise beyond these facts. If a question falls outside, say so and offer a human specialist.

## AOP-04 — Escalation
Trigger: payment dispute, account compromise, high-value order dispute, customer distress, explicit request for human, repeated tool failure, or a tool returning restricted_order.
Behaviour: do NOT attempt to resolve. Do NOT call get_order_status on the affected order — information access is itself gated. Acknowledge the limitation clearly, explain you're escalating to a specialist, and ask for their preferred contact method. Then call escalate_to_human with a summary the specialist can act on, and tell the customer the ticket ID, which team has it, and when to expect contact — only as returned by the tool.
If customer asks for a human at any point: escalate immediately. Do not try to resolve first.

## Human approval gate
Payment disputes, account compromise flags, orders over £500, marketplace seller account data = gated data classes. Never attempt to retrieve or share. Escalate to a human who will review and approve access. Tell the customer plainly: "I need to escalate this to a specialist who can access that securely — bear with me one moment."

## Guardrails (hard — never override)
1. Never assert order/tracking data not returned by a tool call.
2. Never fabricate policy details — use only facts above.
3. Never call submit_return_request unless the customer's latest message explicitly confirms the summary from prepare_return.
4. Never attempt to access payment or account-compromise data.
5. Never loop on an unresolvable issue — escalate once, clearly, and hold.
6. If asked about non-Bookly topics — decline politely, redirect.\
"""

# Prompt caching — tools and the system prompt are identical on every request,
# so one breakpoint at the end of the system prompt caches both (the cached
# prefix is tools → system). Cached input tokens bill at ~10% of the normal rate.
SYSTEM_PROMPT = [
    {
        "type": "text",
        "text": _SYSTEM_PROMPT_TEXT,
        "cache_control": {"type": "ephemeral"},
    }
]

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
# run_agent_turn reports what happens through `emit(kind, **data)` instead of
# printing, so the CLI and the eval harness share one loop. Kinds:
#   text         — Bea's reply text (scripted=True for fixed fallback copy)
#   tool_call    — name, input
#   tool_result  — name, result, is_error
#   api_call     — usage (dict of token counts), latency_s
#   notice       — operational messages (API errors, unusual stop reasons)

Emit = Callable[..., None]


def _usage_dict(usage) -> dict:
    return {
        k: getattr(usage, k, 0) or 0
        for k in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens")
    }


def _run_tool(name: str, args: dict, session: Session) -> dict:
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        if name in SESSION_TOOLS:
            return fn(**args, session=session)
        return fn(**args)
    except Exception as e:  # bad arguments from the model
        return {"error": f"Tool call failed: {type(e).__name__}: {e}"}


def run_agent_turn(client: anthropic.Anthropic, session: Session, emit: Emit) -> None:
    """Run one full agent turn (may include multiple tool-use rounds)."""
    for _ in range(MAX_TOOL_ROUNDS):
        started = time.monotonic()
        try:
            # The SDK already retries connection errors, 429s and 5xx with
            # backoff, so anything that reaches here has failed for good.
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=session.history,
            )
        except anthropic.APIConnectionError:
            emit("notice", text="I'm having trouble connecting right now. Please try again in a moment.")
            return
        except anthropic.RateLimitError:
            emit("notice", text="We're very busy right now. Please try again shortly.")
            return
        except anthropic.APIStatusError as e:
            emit("notice", text="Sorry, something went wrong on my end. Please try again in a moment.",
                 detail=f"API error {e.status_code}: {e.message}")
            return
        except Exception:
            emit("notice", text="Sorry, something unexpected went wrong. Please try again in a moment.",
                 detail=traceback.format_exc())
            return
        emit("api_call", usage=_usage_dict(response.usage),
             latency_s=round(time.monotonic() - started, 3))

        content = response.content
        session.history.append({"role": "assistant", "content": content})
        for block in content:
            if block.type == "text" and block.text.strip():
                emit("text", text=block.text)

        tool_uses = [b for b in content if b.type == "tool_use"]
        if response.stop_reason == "tool_use" and tool_uses:
            results = []
            for block in tool_uses:
                emit("tool_call", name=block.name, input=block.input)
                result = _run_tool(block.name, block.input, session)
                is_error = "error" in result
                emit("tool_result", name=block.name, result=result, is_error=is_error)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                    "is_error": is_error,
                })
            session.history.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "refusal":
            emit("text", scripted=True,
                 text="I can't help with that one here, but I can pass it to a "
                      "human specialist if you'd like.")
        elif response.stop_reason != "end_turn":
            emit("notice", text=f"stopped: {response.stop_reason}")
        return

    emit("text", scripted=True,
         text="I've been going back and forth a bit too much here. Would you "
              "like me to pass this to a human specialist?")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if IS_TTY else text


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if IS_TTY else text


def print_event(kind: str, **data) -> None:
    if kind == "text":
        print(f"\n{_bold('Bea:')} {data['text']}")
    elif kind == "tool_call":
        print(_dim(f"  [tool: {data['name']}({json.dumps(data['input'], ensure_ascii=False)})]"))
    elif kind == "tool_result":
        print(_dim(f"  [result: {json.dumps(data['result'], ensure_ascii=False)}]"))
    elif kind == "notice":
        print(data["text"])
        if data.get("detail"):
            print(_dim(f"[{data['detail'].strip()}]"), file=sys.stderr)


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set.")
        print("Copy .env.example to .env and add your key, or export it directly.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key, max_retries=3)
    session = Session()

    print(f"\n{_bold('Bookly Support — Bea')} · demo mode · {MODEL}")
    print("Type /reset to clear history, /exit or Ctrl+D to quit.\n")

    while True:
        try:
            user_input = input(_bold("You: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() == "/exit":
            print("Goodbye!")
            break
        if user_input.lower() == "/reset":
            session.reset()
            print(_dim("  [history cleared]"))
            continue

        session.add_customer_message(user_input)
        run_agent_turn(client, session, print_event)
        print()


if __name__ == "__main__":
    main()
