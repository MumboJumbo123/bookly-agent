#!/usr/bin/env python3
"""Bookly AI Support Agent — Bea (demo mode)."""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import json
import os
import random
import sys
import time
import traceback

from dotenv import load_dotenv
import anthropic

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 2048
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
        "eligible_for_return": True,
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
        "high_value": True,
    },
}

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _verify_order(order_id: str, customer_email: str):
    """Look up order and verify email. Returns (order_dict, error_dict)."""
    order = MOCK_ORDERS.get(order_id)
    if order is None:
        return None, {"error": "Order not found", "order_id": order_id}
    if order["customer_email"].strip().lower() != customer_email.strip().lower():
        return None, {"error": "Email does not match order records"}
    return order, None


_CUSTOMER_FACING_FIELDS = {
    "status", "items", "carrier", "tracking_number",
    "estimated_delivery", "delivered",
    "eligible_for_return", "ineligibility_reason",
}


def get_order_status(order_id: str, customer_email: str) -> dict:
    order, err = _verify_order(order_id, customer_email)
    if err:
        return err
    return {k: v for k, v in order.items() if k in _CUSTOMER_FACING_FIELDS}


def submit_return_request(
    order_id: str, customer_email: str, items: list[str], reason: str
) -> dict:
    order, err = _verify_order(order_id, customer_email)
    if err:
        return err
    if not order.get("eligible_for_return"):
        return {
            "error": "Order not eligible for return",
            "reason": order.get("ineligibility_reason", "Unknown"),
        }
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
}

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
            "(2) customer has explicitly confirmed intent to proceed. "
            "Generates return reference and prepaid label."
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
            },
            "required": ["order_id", "customer_email", "items", "reason"],
        },
    },
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
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
6. Only after explicit confirmation, call submit_return_request.
Rule: NEVER call submit_return_request without explicit customer confirmation in the immediately preceding turn. Model confidence is irrelevant — this is a hard rule.
If ineligible: explain specifically why, offer human handoff if there's an extenuating circumstance.

## AOP-03 — Policy Q&A
Trigger: question about shipping, returns policy, membership, payment methods, account.
Policy facts (ground truth — do not deviate):
- Returns: 30 days from delivery, unused, original packaging. Damaged/incorrect items: no time limit. Refunds: 5 business days to original payment method. Digital products non-returnable once downloaded.
- Shipping: standard 3–5 days, free over £20 else £2.99. Express next day if ordered before 2pm, £4.99. EU from £6.99, 5–10 days. Click & Collect free, 2,400+ UK locations.
- Bookly Prime: £7.99/month or £79/year. Free express delivery, early sale access, 10% member pricing. Cancel anytime.
- Password reset: bookly.com/reset.
- Payment: Visa, Mastercard, Amex, PayPal, Apple Pay, Google Pay, Bookly Gift Cards.
Rule: do not improvise beyond these facts. If a question falls outside, say so and offer a human specialist.

## AOP-04 — Escalation
Trigger: payment dispute, account compromise, high-value order dispute, customer distress, explicit request for human, repeated tool failure.
Behaviour: do NOT attempt to resolve. Do NOT call get_order_status or any other tool on the affected order — information access is itself gated. Acknowledge the limitation clearly, explain you're escalating to a specialist, collect best contact method.
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

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent_turn(client: anthropic.Anthropic, history: list[dict]) -> None:
    """Run one full agent turn (may include multiple tool-use rounds)."""
    for round_num in range(MAX_TOOL_ROUNDS):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=history,
            )
        except anthropic.APIConnectionError:
            print(_dim("Connection error — let me try again."))
            time.sleep(1)
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=history,
                )
            except Exception:
                print("I'm having trouble connecting right now. Please try again in a moment.")
                return
        except anthropic.RateLimitError:
            print(_dim("Bear with me, high load."))
            time.sleep(2)
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=history,
                )
            except Exception:
                print("Still experiencing high load. Please try again shortly.")
                return
        except anthropic.APIStatusError as e:
            print(f"Something went wrong on my end. I'll connect you to a human agent.")
            print(_dim(f"[API error: {e.status_code}]"), file=sys.stderr)
            return
        except Exception as e:
            print("An unexpected error occurred. Let me connect you to a human agent.")
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
                if fn:
                    result = fn(**args)
                else:
                    result = {"error": f"Unknown tool: {name}"}

                print(_dim(f"  [result: {json.dumps(result, ensure_ascii=False)}]"))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": json.dumps(result),
                })

            history.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason — append and return
        history.append({"role": "assistant", "content": assistant_content})
        return

    # Hit max iterations
    print(f"\n{_bold('Bea:')} I've been going back and forth a bit too much here. "
          "Let me connect you with a human agent who can help directly.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set.")
        print("Copy .env.example to .env and add your key, or export it directly.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
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
