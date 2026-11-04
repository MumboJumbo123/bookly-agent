# Bookly AI Support Agent — Design

*Alastair Paterson · April 2026 · originally a Solutions Engineering take-home*

**Problem.** Bookly needs an AI support agent that handles order status, returns, and policy enquiries autonomously, and escalates payment disputes, account compromises, and high-value cases to a human every time.

## Architecture

Single-agent: four Agent Operating Procedures (AOPs, plain-language playbooks, one per job) in the system prompt, three tools, and a consent gate on the one action that changes an order. No router, no separate classifier. At this scope a router adds a classification error surface without benefit, and a single prompt stays the one point of behavioural control for a CX owner.

```mermaid
flowchart LR
    U[Customer] --> P["System prompt<br/>persona · 4 AOPs · guardrails"]
    P --> A1[AOP-01<br/>Order status]
    P --> A2[AOP-02<br/>Returns]
    P --> A3["AOP-03<br/>Policy Q&A<br/>(fixed policy facts)"]
    P --> A4[AOP-04<br/>Escalation]
    A1 --> R["get_order_status<br/>read-only · email-verified"]
    A2 --> R
    A2 --> G{{"Consent gate<br/>prompt rule + tool check"}}
    G --> W["submit_return_request<br/>creates a return"]
    A4 --> E["escalate_to_human<br/>ticket + handoff summary"]
    E --> H[Human specialist]
    style G stroke:#CC785C,stroke-width:3px
    style W stroke:#CC785C,stroke-width:3px
```

Mock data lives in tool implementations; the system prompt defines behaviour only.

## Conversation design — answer / ask / act

- **Answer** — policy Q&A from enumerated facts; order data only after `get_order_status`.
- **Ask** — one question at a time when info is missing (order ID + email; item; reason).
- **Act** — `submit_return_request` requires explicit customer confirmation in the immediately preceding turn. Hard rule, not model discretion.

## Safety controls

- **Grounding.** Agent cannot state order/tracking data not returned by a tool call.
- **Consent gate (defence-in-depth).** Protected in two layers: system prompt forbids submission without explicit confirmation, and the tool independently rejects calls whose `customer_confirmation` argument is missing, non-affirmative, or not quoted from the customer's last turn — returns a structured `consent_gate_violation`. Talking the model past the prompt rule isn't enough: the tool still needs an explicit yes in the customer's latest message.
- **Human approval gate.** Payment disputes, account compromise, orders >£500, seller disputes — information access is itself gated, not just resolution. Enforced in code: both order tools refuse restricted orders, and `escalate_to_human` opens a ticket with a handoff summary so the specialist doesn't re-ask.

## Example system prompt (excerpt — full prompt in `agent.py`)

```text
## AOP-02 — Returns
Sequence (one question at a time):
  1. Verify identity via get_order_status.  2. Ask which item if multiple.
  3. Ask reason briefly.                    4. Summarise the return.
  5. CONSENT GATE: ask for explicit confirmation. Wait for a clear yes.
  6. Only after explicit confirmation, call submit_return_request.
Rule: NEVER call submit_return_request without explicit customer confirmation
in the immediately preceding turn. Model confidence is irrelevant.

## Guardrails (hard — never override)
1. Never assert order/tracking data not returned by a tool call.
2. Never fabricate policy — use only the enumerated facts above.
3. Never call submit_return_request without explicit confirmation.
4. Never attempt to access payment or account-compromise data.
5. Never loop on an unresolvable issue — escalate once, clearly, and hold.
```

## What changes for production

Session store (Redis, keyed to authenticated user ID) · context-window management · structured logging with session IDs · output classifier as second guardrail layer · streaming responses · secrets manager · retry + circuit-breaking on real integrations · regression suite on adversarial and consent-bypass prompts · crawl → walk → run rollout (shadow → 10–50% → 80%+ with specialist sub-agents).

*[Appendix](DESIGN_APPENDIX.md): AOP-02 sequence diagram, priority discovery questions, training data strategy, crawl/walk/run architecture, success metrics.*
