"""A scripted stand-in for the Anthropic client, for offline tests.

It answers `messages.create` from per-model queues of canned responses, so
the agent loop, tools, eval checks and reports can all be exercised without
an API key. It is a test double only: nothing it produces is ever written
into the published transcripts.
"""

from __future__ import annotations

from types import SimpleNamespace


def text(t: str):
    return SimpleNamespace(type="text", text=t)


_ids = iter(range(1, 10_000))


def tool(name: str, **input):
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=input)


def response(*blocks, stop_reason: str | None = None, usage: dict | None = None):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    u = {"input_tokens": 50, "output_tokens": 80,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3000, **(usage or {})}
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=SimpleNamespace(**u))


class ScriptedClient:
    """Answers each call from the queue for its role: agent, simulator or judge.

    The role is worked out from the request, because the agent and the judge
    can run on the same model: agent calls carry tools, simulator calls carry
    the role-play system prompt, and anything else is the judge.
    """

    def __init__(self, agent=(), simulator=(), judge=()):
        self.queues = {"agent": list(agent), "simulator": list(simulator), "judge": list(judge)}
        self.requests: list[tuple[str, dict]] = []
        self.messages = self

    @staticmethod
    def role(kwargs: dict) -> str:
        if "tools" in kwargs:
            return "agent"
        if str(kwargs.get("system", "")).startswith("You are role-playing"):
            return "simulator"
        return "judge"

    def create(self, **kwargs):
        role = self.role(kwargs)
        self.requests.append((role, kwargs))
        if not self.queues[role]:
            raise AssertionError(f"no scripted {role} response left")
        return self.queues[role].pop(0)
