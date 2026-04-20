# Bookly AI Support Agent

Solutions Engineering take-home — Decagon.

## Run
1. Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`
2. `pip install -r requirements.txt`
3. `python3 agent.py`

Commands: `/reset` clears history, `/exit` or Ctrl+D quits.

## Architecture
- Single-agent architecture (no router) — chosen for simplicity at this scope
- Four AOPs encoded in the system prompt
- Two tools: `get_order_status`, `submit_return_request` (mocked)
- In-memory conversation history with max-iteration safety bound

## Test flows
| Flow | Order ID | Email | Demonstrates |
|---|---|---|---|
| Order status | BK-10042 | alex@bookly.com | AOP-01 happy path |
| Return | BK-10078 | alex@bookly.com | AOP-02 consent gate |
| Ineligible return | BK-10099 | jordan@bookly.com | AOP-02 rejection |
| Auth guardrail | BK-10042 | wrong@test.com | Email verification |
| Payment dispute | BK-55210 | sarah@bookly.com | AOP-04 + approval gate |

## Production follow-ups
- Context window management (history truncation + summarisation)
- Structured logging with session IDs
- Output classifier as second guardrail layer
- Session store (Redis) keyed to authenticated user ID
- Streaming responses for perceived latency
- Input normalisation (Unicode, subaddress handling)
- Regression test suite with adversarial inputs
- Secrets management (don't rely on .env in production)
