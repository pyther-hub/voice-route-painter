"""app/llm.py: the Sarvam (Chat Completions) path and the model switch. Fake clients only, no paid calls."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import agent, llm, main, state

GEMMA = "sarvam/gemma4"


class FakeMessage(SimpleNamespace):
    def model_dump(self, exclude_none=False):
        d = {"role": "assistant", "content": self.content,
             "tool_calls": [{"id": c.id, "type": "function",
                             "function": {"name": c.function.name, "arguments": c.function.arguments}}
                            for c in self.tool_calls] if self.tool_calls else None}
        return {k: v for k, v in d.items() if v is not None} if exclude_none else d


def _reply(calls=(), text=None, cached=None):
    tool_calls = [SimpleNamespace(id=f"call-{n}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
                  for n, (name, args) in enumerate(calls)]
    details = None if cached is None else SimpleNamespace(cached_tokens=cached)    # null on a cache miss
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120, prompt_tokens_details=details)
    return SimpleNamespace(choices=[SimpleNamespace(message=FakeMessage(content=text, tool_calls=tool_calls or None))],
                           usage=usage)


@pytest.fixture
def sarvam(monkeypatch):
    """A fake Sarvam client: give it replies, read back what each call sent."""
    sent, replies = [], []

    def create(**kw):
        sent.append(json.loads(json.dumps(kw)))      # a copy: the loop keeps appending to the messages
        return replies.pop(0)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setitem(llm._clients, "sarvam", fake)
    return sent, replies


def test_step_parses_tool_calls_and_usage(sarvam):
    sent, replies = sarvam
    replies.append(_reply([("resolve_place", {"queries": ["Agra"]})]))
    tools = [{"type": "function", "name": "resolve_place", "description": "d",
              "parameters": {"type": "object"}, "strict": True}]
    r = llm.step(GEMMA, "SYS", [{"role": "user", "content": "hi"}], tools, None, 4000)
    assert [(c.name, json.loads(c.arguments), c.call_id) for c in r.calls] == [("resolve_place", {"queries": ["Agra"]}, "call-0")]
    assert r.usage == {"input": 100, "cached": 0, "output": 20, "total": 120}
    kw = sent[0]
    assert kw["model"] == "gemma4" and kw["max_tokens"] == 4000 and "reasoning_effort" not in kw
    assert kw.get("extra_body") is None
    assert kw["messages"][0] == {"role": "system", "content": "SYS"}
    assert kw["tools"] == [{"type": "function", "function": {"name": "resolve_place", "description": "d",
                                                              "parameters": {"type": "object"}}}]   # nested, no strict
    assert llm.tool_output(GEMMA, "call-0", "{}") == {"role": "tool", "tool_call_id": "call-0", "content": "{}"}


def test_cost_converts_rupees():
    usage = {"input": 1_000_000, "cached": 0, "output": 1_000_000}
    assert llm.cost_usd(GEMMA, usage) == pytest.approx((35 + 90) / llm.INR_PER_USD)
    assert llm.cost_usd("groq/openai/gpt-oss-120b", usage) == pytest.approx(0.15 + 0.60)


def test_turn_on_sarvam_edits_the_trip(sarvam):
    sent, replies = sarvam
    sid = "sarvam-turn"
    state.drop_session(sid)
    state.update_state(sid, ["delhi", "agra"])
    state.session(sid).model = GEMMA
    replies += [_reply([("add_stop", {"place_id": "jaipur", "after": "agra", "before": None, "position": None})],
                       cached=60),
                _reply(text="")]
    assert agent.run_turn(sid, "Agra ke baad Jaipur") == "Added Jaipur after Agra."
    assert state.stop_ids(sid) == ["delhi", "agra", "jaipur"]
    second = sent[1]["messages"]                      # the 2nd call carries the tool call and its result
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "call-0"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call-0"
    assert json.loads(second[-1]["content"])["ok"]
    assert sent[0]["reasoning_effort"] == "low"


def test_model_click_switches_without_an_llm_call(sarvam, monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "x")
    sid = "switch1"
    state.drop_session(sid)
    assert asyncio.run(main.read_input(sid, json.dumps({"type": "model", "id": GEMMA}))) == (None, None, "text")
    assert state.session(sid).model == GEMMA and state.ui(sid)["model"] == GEMMA
    text, error, _ = asyncio.run(main.read_input(sid, json.dumps({"type": "model", "id": "sarvam/nope"})))
    assert text is None and error and state.session(sid).model == GEMMA


def test_models_without_a_key_are_not_offered(monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert {m["provider"] for m in llm.available()} == {"groq"}
    assert not llm.is_available(GEMMA)


def test_groq_uses_its_own_output_cap_name_and_extra_fields(monkeypatch):
    sent = []
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: sent.append(kw) or _reply(text="ok"))))
    monkeypatch.setitem(llm._clients, "groq", fake)
    r = llm.step("groq/openai/gpt-oss-20b", "SYS", [{"role": "user", "content": "hi"}], None, "low", 4000)
    assert r.text == "ok"
    kw = sent[0]
    assert kw["model"] == "openai/gpt-oss-20b" and kw["max_completion_tokens"] == 4000 and "max_tokens" not in kw
    assert kw["reasoning_effort"] == "low" and kw["extra_body"] == {"include_reasoning": False}


def test_openai_is_not_a_provider():
    assert "openai" not in llm.PROVIDERS
    assert not any(m["provider"] == "openai" for m in llm.MODELS.values())
    assert llm.DEFAULT in llm.MODELS and llm.FRONTIER in llm.MODELS
