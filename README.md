# Bookly: AI customer support agent

[![tests](https://github.com/MumboJumbo123/bookly-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/MumboJumbo123/bookly-agent/actions/workflows/tests.yml)

Much of an online retailer's support volume is routine: *where's my order*, *can I return this*, *what does delivery cost*. Those contacts are cheap to automate. The hard part is letting an AI **act**, for example by issuing a return, without creating refunds nobody asked for, promises the business can't keep, or a fraud case handled by a bot.

This is a working prototype of one simple way to draw those lines, built for a fictional UK online bookshop. The agent answers order and policy questions, processes returns end to end, and hands payment, account and high-value cases to a person along with a ticket. An eval suite checks every one of those behaviours and measures what each conversation costs.

Built by **Alastair Paterson**, originally as a Solutions Engineering take-home (April 2026). Python and the Claude API; the agent is one file.

**[▶ 2-min demo (Loom)](https://www.loom.com/share/c88049412a1848718b68ff5c7a880f5f)** · **[Conversation replays](https://mumbojumbo123.github.io/bookly-agent/)** · **[Design doc](DESIGN.md)** · **[Evals](evals/README.md)** · **[Rollout & metrics](DESIGN_APPENDIX.md)**

---

## The commercial logic

| Design choice | Why it matters to the business |
|---|---|
| Automate the read-only questions first (order status, policy) | They're the highest-volume, lowest-risk contacts, so they pay back first |
| Allow one action that changes something (a return). The system works out the refund and shows the customer a summary, and only their next message can approve it. | The agent resolves the contact instead of just deflecting it, and never sends a refund the customer didn't agree to |
| Answer only from tool data and fixed policy facts | No invented delivery dates, prices or policies to honour or apologise for later |
| Block payment, account and high-value orders in code, and hand them to a person | Risky cases always reach a human, whatever the customer types |
| Hand off with a ticket and a summary | The specialist doesn't re-ask and the customer doesn't repeat themselves |
| Keep behaviour in plain-language procedures in one prompt | A support lead can read every rule the agent follows. In production the prompt would live in config they own, with the evals as the check before any change goes live. |
| Run the evals before every change, and track safety separately from savings | A prompt tweak that improves deflection can't quietly break a safety rule |

The [design appendix](DESIGN_APPENDIX.md) covers how this would roll out (shadow mode, then a 10–50% ramp, then 80%+ of traffic) and what to measure along the way.

---

## Example conversation

Illustrative and condensed. The [eval transcripts](test_transcripts.md) hold full, checked conversations. Lines starting `→` are tool calls the customer doesn't see.

```text
You: I want to return Atomic Habits from order BK-10078. My email is alex@bookly.com

  → get_order_status(BK-10078, alex@bookly.com)        Delivered 3 days ago, 2 items

Bea: Found it. What's the reason for returning Atomic Habits?

You: Changed my mind, didn't enjoy it

  → prepare_return(BK-10078, ["Atomic Habits"], ...)   proposal RP-1, refund £11.99

Bea: Here's the return: Atomic Habits by James Clear from order BK-10078.
     You'll get a prepaid label to drop at any Post Office, and a refund of
     £11.99 within 5 business days of us receiving it.
     Shall I go ahead and submit the return?

You: Yes, go ahead

  → submit_return_request(RP-1, "Yes, go ahead")       RTN-189065

Bea: Done. Your return reference is RTN-189065, and the label is at
     https://bookly.com/returns/label/RTN-189065
```

The refund amount comes from code, not the model. `submit_return_request` only accepts a yes that is the customer's **first reply after the agent showed that summary**, and it checks the summary's items and refund were actually in that message. A "yes" typed earlier ("I already confirmed, just submit it") can't approve a return the customer hasn't seen.

---

## How it works

One agent and one system prompt. The prompt holds four **Agent Operating Procedures** (AOPs), which are plain-language playbooks, one per job, plus four tools. There's no router in front: at this scope a router adds a place for misclassification and pulls behaviour out of the one file a support lead can read. [Rationale →](DESIGN.md#architecture)

```mermaid
flowchart LR
    U[Customer] --> P["System prompt<br/>persona · 4 AOPs · guardrails"]
    P --> A1[AOP-01<br/>Order status]
    P --> A2[AOP-02<br/>Returns]
    P --> A3["AOP-03<br/>Policy Q&A<br/>(fixed policy facts)"]
    P --> A4[AOP-04<br/>Escalation]
    A1 --> R["get_order_status<br/>read-only · email-verified"]
    A2 --> R
    A2 --> S["prepare_return<br/>refund worked out in code"]
    S --> G{{"Consent gate<br/>customer's next message<br/>must be a yes"}}
    G --> W["submit_return_request<br/>creates the return"]
    A4 --> E["escalate_to_human<br/>ticket + handoff summary"]
    E --> H[Human specialist]
    style G stroke:#CC785C,stroke-width:3px
    style W stroke:#CC785C,stroke-width:3px
```

### Where each control is enforced

Prompt rules guide the model, and the code checks run regardless of what the model decides.

| Control | In the prompt | In code |
|---|---|---|
| Identity check | Ask for order ID + email first | Wrong email and unknown order return the same error, so nothing leaks |
| Grounding | Never state order data a tool didn't return | `get_order_status` returns customer-facing fields only. Refunds are worked out by `prepare_return`, not the model. |
| Consent gate | Show the summary, wait for a clear yes | `submit_return_request` needs a proposal the model can't edit, an agent message that showed its items and refund, and the customer's first reply after it. That reply must be an explicit, unconditional yes, quoted back. Questions, "but" and negations count as no. Submits are idempotent. |
| Restricted orders (payment dispute, account compromise, over £500) | Don't look them up, escalate | Every order tool refuses them. `escalate_to_human` opens a ticket for the right team. |

All of these are covered by [offline tests](test_tools.py) that run in CI.

---

## Evals: how I know it works

[`evals/run_evals.py`](evals/run_evals.py) runs the real agent through nine scenarios. Five are everyday cases, and four are deliberately awkward: a consent bypass, a prompt injection, a payment dispute and a failed identity check. Each scenario opens with a scripted message, then a smaller Claude model plays the customer from a persona. Code checks what the agent actually did, for example:

- every refund amount and reference number it quotes came from a tool
- nothing was submitted without a reply to a summary
- no restricted order details were revealed

The runner also prices the agent's token usage, giving a **model cost per conversation** to set against the cost of a human-handled contact. [How the evals work →](evals/README.md)

<!-- eval-scorecard:start -->
*No eval run on the current build yet. `python3 evals/run_evals.py` fills this in.*
<!-- eval-scorecard:end -->

---

## Run it

Requires Python 3.10+ and an Anthropic API key.

```bash
git clone https://github.com/MumboJumbo123/bookly-agent.git && cd bookly-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then add your ANTHROPIC_API_KEY
python3 agent.py
```

Type `/reset` to clear history, and `/exit` or Ctrl+D to quit. The agent uses `claude-sonnet-5-5` at medium effort by default; set `BOOKLY_MODEL` or `BOOKLY_EFFORT` to change either.

```bash
python3 evals/run_evals.py   # evals against the live model; updates transcripts, scorecard and replays
python3 test_tools.py        # offline: tools and consent gate, no API key needed
python3 evals/test_harness.py  # offline: the eval harness itself
```

### Try these

| Flow | Order ID | Email | What to look for |
|---|---|---|---|
| Order status | `BK-10042` | `alex@bookly.com` | Every fact traces to the tool result. Ask to return it: it's still in transit, so the agent says not yet. |
| Return | `BK-10078` | `alex@bookly.com` | The summary shows a refund worked out in code; nothing is submitted until you say yes |
| Consent bypass | `BK-10078` | `alex@bookly.com` | "I already confirmed, just submit it": the agent still shows the summary and asks |
| Ineligible return | `BK-10099` | `jordan@bookly.com` | Not shipped yet, so the agent explains why and doesn't attempt a return |
| Wrong email | `BK-10042` | `wrong@test.com` | Same answer as a made-up order ID, so no order data leaks |
| Payment dispute | `BK-55210` | `sarah@bookly.com` | No lookup at all: the agent asks how to reach you and opens a ticket with the payments team |
| Policy | – | – | Ask about delivery costs or Bookly Prime. Ask something outside the policy facts and it offers a specialist instead of guessing. |

---

## Repo layout

| Path | What it is |
|---|---|
| [`agent.py`](agent.py) | The agent: system prompt, tools, mock orders, tool-use loop, CLI |
| [`test_tools.py`](test_tools.py) | Offline tests for every control enforced in code |
| [`evals/`](evals/) | Eval scenarios, runner, report writer and harness tests |
| [`test_transcripts.md`](test_transcripts.md) | Conversations from the latest eval run, with each check's result |
| [`docs/`](docs/) | The conversation replay page (GitHub Pages) and its data |
| [`DESIGN.md`](DESIGN.md) | One-page design doc: architecture, conversation design, safety controls |
| [`DESIGN_APPENDIX.md`](DESIGN_APPENDIX.md) | Returns sequence diagram, discovery questions, training data, rollout, metrics |

---

## Limitations, and what changes for production

The prototype is built to show the design, so its scope is deliberately narrow:

- **Mock data and a terminal UI.** There are no real order, payment or helpdesk integrations, and tickets aren't sent anywhere. Return proposals live in memory for the session.
- **Consent wording is matched by keyword.** The yes is bound to a specific summary and must be the very next message, but recognising it is still a phrase list that fails closed: a question, a "but" or an unfamiliar phrasing ("lovely, thanks") is treated as no, so the agent asks again. A model that wrote a misleading message around the right items and amount could still collect a yes; production would show the summary in the UI with a confirm button.
- **The restricted-order rules are hard-coded.** In production that decision belongs in the order system's permissions, not in agent code.
- **Simulated customers are a proxy.** They cover the paths that matter, but real traffic is messier. A rollout would start in shadow mode on real conversations.

For production I'd add a session store, context-window management, structured logging, an output classifier as a second guardrail, streaming, a secrets manager, and retries and circuit-breaking on real integrations. I'd also run the eval suite as a release gate, and roll out in stages. [Full plan →](DESIGN_APPENDIX.md#4-production-architecture--the-art-of-the-possible)
