# Bookly Agent — Claude context

Decagon SE take-home. Single-file Python CLI agent ("Bea") for a fictional UK book retailer. Submission = design docs + working code + screen recording.

## File map
| Path | Role | Size |
|---|---|---|
| `agent.py` | Runtime. Anthropic SDK, 2 tools, 4 AOPs in system prompt, mock data inline. | ~15KB |
| `DESIGN.md` | Headline design doc — AOPs, architecture, commercial framing. | ~11KB |
| `DESIGN_APPENDIX.md` | 19 assumptions, training data, metrics, crawl/walk/run arch. | ~24KB — **do not read whole file; Grep or Read with offset** |
| `test_transcripts.md` | Four verified AOP flows for evaluator reproduction. | ~7KB |
| `README.md` | Run instructions + test flow table. | ~1KB |
| `.env` | `ANTHROPIC_API_KEY`. Not committed. | — |

## Invariants (do not change without asking)
- **Consent gate**: `submit_return_request` is only called after explicit customer confirmation in the immediately preceding turn. Hard rule, not model discretion.
- **Grounding**: agent never asserts order/tracking data it hasn't retrieved via a tool call.
- **Separation of concerns**: mock data lives in tool implementations, not in the system prompt. Tools return data; prompt defines behaviour.
- **Single-agent architecture**: no router, no classifier. Prompt-based intent routing is the deliberate choice.
- **Escalation is architectural**: payment disputes, account compromise, orders >£500, seller disputes → human approval gate, never attempted autonomously.

## Writing style for Decagon-facing artifacts
Positive-first. Balance the commercial and technical buyer in every section. Show the art-of-the-possible via the crawl/walk/run phasing. Direct commercial framing preferred over hedged language.
