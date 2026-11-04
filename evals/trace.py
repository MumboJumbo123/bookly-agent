"""A recorded conversation, plus helpers the checks use to query it."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class Turn:
    customer: str
    events: list[dict] = field(default_factory=list)


@dataclass
class Trace:
    turns: list[Turn] = field(default_factory=list)

    # --- queries ---------------------------------------------------------

    def tool_calls(self, name: str | None = None) -> list[dict]:
        """Every tool call as {turn, name, input, result, is_error}, in order."""
        calls, pending = [], {}
        for t, turn in enumerate(self.turns):
            for ev in turn.events:
                if ev["kind"] == "tool_call":
                    pending = {"turn": t, "name": ev["name"], "input": ev["input"]}
                elif ev["kind"] == "tool_result" and pending:
                    calls.append({**pending, "result": ev["result"], "is_error": ev["is_error"]})
                    pending = {}
        return [c for c in calls if name is None or c["name"] == name]

    def successful(self, name: str) -> list[dict]:
        return [c for c in self.tool_calls(name) if not c["is_error"]]

    def agent_texts(self, turn: int | None = None) -> list[str]:
        turns = self.turns if turn is None else [self.turns[turn]]
        return [ev["text"] for tr in turns for ev in tr.events if ev["kind"] == "text"]

    def agent_text(self, turn: int | None = None) -> str:
        return "\n".join(self.agent_texts(turn))

    def texts_before_first(self, name: str) -> str:
        """Agent text emitted before the first successful call to `name`."""
        out = []
        for turn in self.turns:
            for ev in turn.events:
                if ev["kind"] == "tool_result" and ev["name"] == name and not ev["is_error"]:
                    return "\n".join(out)
                if ev["kind"] == "text":
                    out.append(ev["text"])
        return "\n".join(out)

    def tool_results_blob(self) -> str:
        return json.dumps([c["result"] for c in self.tool_calls()], ensure_ascii=False)

    def api_calls(self) -> list[dict]:
        return [ev for turn in self.turns for ev in turn.events if ev["kind"] == "api_call"]

    def mentions(self, pattern: str, turn: int | None = None) -> bool:
        return re.search(pattern, self.agent_text(turn), re.IGNORECASE) is not None

    # --- serialisation ---------------------------------------------------

    def to_json(self) -> list[dict]:
        return [{"customer": t.customer, "events": t.events} for t in self.turns]
