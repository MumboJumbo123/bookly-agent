# Bookly Take-Home — Submission Guide
**Alastair Paterson** · Decagon Solutions Engineering

## Best viewing experience (recommended)
- **Demo (2 min, Loom):** https://www.loom.com/share/c88049412a1848718b68ff5c7a880f5f
- **GitHub repo:** https://github.com/MumboJumbo123/bookly-agent

The design doc renders best on GitHub — Mermaid diagrams (architecture, sequence, crawl/walk/run) don't render in PDF. The Loom + GitHub combination is the intended experience.

## What's where
- `DESIGN.md` — one-page design doc (primary deliverable)
- `DESIGN_APPENDIX.md` — optional depth: AOP-02 sequence, crawl/walk/run, extended rationale
- `agent.py` — single-agent prototype (Anthropic API, tool use, consent-gated return submission)
- `test_transcripts.md` — six end-to-end flows including adversarial consent-bypass
- `README.md` — run instructions

## Fallback files (bundled in this upload)
- `DESIGN.pdf`, `DESIGN_APPENDIX.pdf` — PDF fallbacks (diagrams missing; see GitHub)
- `bookly_demo.mp4` — Loom download
- `bookly-agent.zip` — full source

## Notes
- Demo covers Flow 2 (returns + consent gate) — highest-signal flow within the 2-min cap.
- Single-agent architecture is deliberate, not an oversight — rationale in DESIGN.md.
- Consent gate enforced in two layers: system prompt + tool-level grounding check.
