#!/usr/bin/env python3
"""Bookly AI Support Agent — Bea (demo mode)."""

import json
import os
import random
import re
import sys
import traceback

from dotenv import load_dotenv
import anthropic

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
# ANSI helpers
# ---------------------------------------------------------------------------

def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if IS_TTY else text

def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if IS_TTY else text

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


def get_order_status(order_id: str, customer_email: str) -> dict:
    order, err = _verify_order(order_id, customer_email)
    if err:
        return err
    reason = _restricted_reason(order)
    if reason:
        return {
            "error": "restricted_order",
            "category": reason,
            "detail": "This order needs a human specialist. Do not discuss its "
                      "details; escalate with escalate_to_human.",
        }
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


# Consent gate — defence-in-depth for submit_return_request.
# The system prompt instructs the model never to submit without explicit
# customer confirmation. This tool-level check is the second layer: even if
# the prompt is jailbroken, the tool refuses unless (a) the customer's most
# recent message is an explicit, non-negated confirmation and (b) the
# confirmation phrase the model passes is quoted from that message.
#
# Matching is whole-word, so "ok" does not match "book" and "correct" does
# not match "incorrect". Any negation fails closed: a false "no" costs one
# extra question, a false "yes" submits a return the customer didn't want.
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
    t = text.lower().replace("\u2019", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return " ".join(t.split())


def _is_consent_phrase(text: str) -> bool:
    t = _normalise(text)
    return bool(_CONSENT_RE.search(t)) and not _NEGATION_RE.search(t)


def submit_return_request(
    order_id: str,
    customer_email: str,
    items: list[str],
    reason: str,
    customer_confirmation: str,
    _last_user_text: str = "",
) -> dict:
    # Consent gate — refuses the call if the customer did not explicitly
    # confirm in the immediately preceding turn.
    if not _is_consent_phrase(customer_confirmation):
        return {
            "error": "consent_gate_violation",
            "detail": (
                "submit_return_request was called without a valid "
                "customer_confirmation phrase. The customer must explicitly "
                "confirm in their most recent message before this tool may "
                "be called."
            ),
        }
    if not _is_consent_phrase(_last_user_text):
        return {
            "error": "consent_gate_violation",
            "detail": (
                "The customer's most recent message does not contain an "
                "explicit confirmation. Ask the customer clearly, wait for "
                "a yes, then retry."
            ),
        }
    if _normalise(customer_confirmation) not in _normalise(_last_user_text):
        return {
            "error": "consent_gate_violation",
            "detail": (
                "customer_confirmation must be quoted from the customer's "
                "most recent message. Pass their confirming words verbatim."
            ),
        }

    order, err = _verify_order(order_id, customer_email)
    if err:
        return err
    restricted = _restricted_reason(order)
    if restricted:
        return {"error": "restricted_order", "category": restricted,
                "detail": "Returns on this order need a human specialist. Escalate."}
    if not order.get("eligible_for_return"):
        return {
            "error": "Order not eligible for return",
            "reason": order.get("ineligibility_reason", "Unknown"),
        }
    titles = [i["title"].lower() for i in order["items"]]
    unknown = [it for it in items if not any(it.strip().lower() in t for t in titles)]
    if not items or unknown:
        return {"error": "Items not found on this order", "unknown_items": unknown,
                "order_items": [i["title"] for i in order["items"]]}
    ref = f"RTN-{random.Random(order_id).randint(100000, 999999)}"
    return {
        "success": True,
        "return_reference": ref,
        "label_url": f"https://bookly.com/returns/label/{ref}",
        "refund_eta": "5 business days after receipt",
    }


TOOL_DISPATCH = {
    "get_order_status": get_order_status,
    "submit_return_request": submit_return_request,
    "escalate_to_human": escalate_to_human,
}


def _latest_customer_message(history: list[dict]) -> str:
    """Most recent user message text, skipping tool_result turns."""
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
    return ""

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
        "name": "submit_return_request",
        "description": (
            "Submit a return for delivered order items. Must only be called after "
            "(1) order verified eligible via get_order_status, and "
            "(2) customer has explicitly confirmed intent to proceed in their "
            "immediately preceding message. Generates return reference and "
            "prepaid label. The tool enforces the consent check at runtime — "
            "if customer_confirmation is missing or not grounded in the "
            "customer's last message, the call is rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The Bookly order ID"},
                "customer_email": {"type": "string", "description": "Customer's email address for verification"},
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of item titles to return",
                },
                "reason": {"type": "string", "description": "Customer's reason for return"},
                "customer_confirmation": {
                    "type": "string",
                    "description": (
                        "The exact phrase the customer used to confirm, taken "
                        "from their most recent message (e.g. 'yes go ahead', "
                        "'please submit'). Required. The tool validates this "
                        "is a genuine confirmation grounded in the preceding "
                        "turn — fabricated values will be rejected."
                    ),
                },
            },
            "required": [
                "order_id", "customer_email", "items", "reason",
                "customer_confirmation",
            ],
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
Warm, efficient, confident. Not corporate. Not sycophantic. Never start with "Great!", "Absolutely!", "Of course!", "Certainly!". Use "we" for Bookly, "I" for yourself. Keep responses short — one idea per paragraph. Match customer energy.

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
4. Summarise the return request.
5. CONSENT GATE: ask for explicit confirmation ("Shall I go ahead and submit the return?"). Wait for a clear yes.
6. Only after explicit confirmation, call submit_return_request. Pass the customer's exact confirming phrase (e.g. "yes go ahead") as the customer_confirmation argument.
Rule: NEVER call submit_return_request without explicit customer confirmation in the immediately preceding turn. Model confidence is irrelevant — this is a hard rule, and the tool itself enforces it at runtime (calls without a grounded confirmation phrase are rejected).
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
Trigger: payment dispute, account compromise, high-value order dispute, customer distress, explicit request for human, repeated tool failure, or get_order_status returning restricted_order.
Behaviour: do NOT attempt to resolve. Do NOT call get_order_status on the affected order — information access is itself gated. Acknowledge the limitation clearly, explain you're escalating to a specialist, and ask for their preferred contact method. Then call escalate_to_human with a summary the specialist can act on, and tell the customer the ticket ID, which team has it, and when to expect contact — only as returned by the tool.
If customer asks for a human at any point: escalate immediately. Do not try to resolve first.

## Human approval gate
Payment disputes, account compromise flags, orders over £500, marketplace seller account data = gated data classes. Never attempt to retrieve or share. Escalate to a human who will review and approve access. Tell the customer plainly: "I need to escalate this to a specialist who can access that securely — bear with me one moment."

## Guardrails (hard — never override)
1. Never assert order/tracking data not returned by a tool call.
2. Never fabricate policy details — use only facts above.
3. Never call submit_return_request without explicit customer confirmation.
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

def run_agent_turn(client: anthropic.Anthropic, history: list[dict]) -> None:
    """Run one full agent turn (may include multiple tool-use rounds)."""
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            # The SDK already retries connection errors, 429s and 5xx with
            # backoff, so anything that reaches here has failed for good.
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=history,
            )
        except anthropic.APIConnectionError:
            print("I'm having trouble connecting right now. Please try again in a moment.")
            return
        except anthropic.RateLimitError:
            print("We're very busy right now. Please try again shortly.")
            return
        except anthropic.APIStatusError as e:
            print("Sorry, something went wrong on my end. Please try again in a moment.")
            print(_dim(f"[API error {e.status_code}: {e.message}]"), file=sys.stderr)
            return
        except Exception:
            print("Sorry, something unexpected went wrong. Please try again in a moment.")
            traceback.print_exc(file=sys.stderr)
            return

        # Process response content
        assistant_content = response.content
        tool_use_blocks = [b for b in assistant_content if b.type == "tool_use"]
        text_blocks = [b for b in assistant_content if b.type == "text"]

        # Print any text output
        for block in text_blocks:
            if block.text.strip():
                print(f"\n{_bold('Bea:')} {block.text}")

        # If end_turn, we're done
        if response.stop_reason == "end_turn":
            history.append({"role": "assistant", "content": assistant_content})
            return

        # Handle tool use
        if response.stop_reason == "tool_use" and tool_use_blocks:
            history.append({"role": "assistant", "content": assistant_content})

            tool_results = []
            for tool_block in tool_use_blocks:
                name = tool_block.name
                args = tool_block.input
                print(_dim(f"  [tool: {name}({json.dumps(args, ensure_ascii=False)})]"))

                fn = TOOL_DISPATCH.get(name)
                try:
                    if fn is None:
                        result = {"error": f"Unknown tool: {name}"}
                    elif name == "submit_return_request":
                        # Inject the customer's most recent conversational
                        # message so the consent gate can verify it.
                        result = fn(**args, _last_user_text=_latest_customer_message(history))
                    else:
                        result = fn(**args)
                except Exception as e:  # bad arguments from the model
                    result = {"error": f"Tool call failed: {type(e).__name__}: {e}"}
                is_error = "error" in result

                print(_dim(f"  [result: {json.dumps(result, ensure_ascii=False)}]"))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": json.dumps(result),
                    "is_error": is_error,
                })

            history.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason — append and return
        history.append({"role": "assistant", "content": assistant_content})
        if response.stop_reason == "refusal":
            print(f"\n{_bold('Bea:')} I can't help with that one here, but I can "
                  "pass it to a human specialist if you'd like.")
        elif not text_blocks:
            print(_dim(f"  [stopped: {response.stop_reason}]"))
        return

    # Hit max iterations
    print(f"\n{_bold('Bea:')} I've been going back and forth a bit too much here. "
          "Would you like me to pass this to a human specialist?")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set.")
        print("Copy .env.example to .env and add your key, or export it directly.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key, max_retries=3)
    history: list[dict] = []

    print(f"\n{_bold('Bookly Support — Bea')} · demo mode")
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
            history.clear()
            print(_dim("  [history cleared]"))
            continue

        history.append({"role": "user", "content": user_input})
        run_agent_turn(client, history)


if __name__ == "__main__":
    main()
