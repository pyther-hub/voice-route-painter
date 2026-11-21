"""Which LLM answers, and how to call it. The only file that knows the providers.

    r = step("sarvam/gemma4", SYSTEM_PROMPT, items, tools.openai_tools(), effort="low", max_out=4000)
    r.text, r.calls (name, arguments, call_id), r.items (append to the history), r.usage {input, cached, output, total}

- OpenAI (gpt-5.4-nano): Responses API with strict function calling, as before.
- Sarvam Token Factory: Sarvam's own OpenAI-compatible API (https://api.sarvam.ai/v2, Chat Completions,
  SARVAM_API_KEY, the same key as speech-to-text).
- Groq: its OpenAI-compatible API (https://api.groq.com/openai/v1, Chat Completions, GROQ_API_KEY).
  Sarvam and Groq don't document `strict` function tools, so the gateway's validator (tools._validate) is what
  catches off-schema arguments there.

A turn's history only ever goes to one provider (the model is fixed when the turn starts). The past turns
(`{role, content}` messages) are valid for both APIs; within a turn each provider appends its own item shapes.
"""
import json
import os
from types import SimpleNamespace
from typing import NamedTuple

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()   # OPENAI_API_KEY / SARVAM_API_KEY from .env (gitignored)

TIMEOUT_S = 30
INR_PER_USD = 88          # rough; Sarvam prices are in ₹ (excl. GST), the spend cap is in USD


def _inr(inp: float, cached: float, out: float) -> dict:
    return {"input": inp / INR_PER_USD, "cached": cached / INR_PER_USD, "output": out / INR_PER_USD}


# Provider -> (label, API key variable, base URL (None = OpenAI), how the output cap is named). Chat Completions
# for all but OpenAI.
PROVIDERS = {
    "openai": dict(label="OpenAI", key="OPENAI_API_KEY", url=None, max_param=None),
    "sarvam": dict(label="Sarvam", key="SARVAM_API_KEY", url="https://api.sarvam.ai/v2", max_param="max_tokens"),
    "groq": dict(label="Groq", key="GROQ_API_KEY", url="https://api.groq.com/openai/v1",
                 max_param="max_completion_tokens"),
}

# id -> settings. price = USD per million tokens. effort = reasoning for the agent loop, chips_effort = for the
# follow-up chips (None = don't send the parameter). max_out = output tokens per call (reasoning shares it).
# extra = more request fields for that model.
MODELS = {
    "openai/gpt-5.4-nano": dict(provider="openai", model="gpt-5.4-nano", label="GPT-5.4 nano",
                                price={"input": 0.20, "cached": 0.02, "output": 1.25},
                                note="$0.20 / $1.25 per M tokens", effort="low", chips_effort="none", max_out=1500),
    # The frontier planner (tools.plan_with_frontier), not offered in the UI picker (hidden).
    # Price checked 2026-11-21 on OpenAI's model page: $2.50 input / $0.25 cached / $15.00 output per M tokens
    # (prompts over 272k tokens cost more; ours are ~1k).
    "openai/gpt-5.4": dict(provider="openai", model="gpt-5.4", label="GPT-5.4",
                           price={"input": 2.50, "cached": 0.25, "output": 15.00},
                           note="frontier planner", effort="low", chips_effort="none", max_out=3000, hidden=True),
    "sarvam/gemma4": dict(provider="sarvam", model="gemma4", label="Gemma 4 31B",
                          price=_inr(35, 10, 90), note="₹35 / ₹90 per M tokens",
                          effort="low", chips_effort=None, max_out=4000),
    "sarvam/deepseekv4.1-flash-1017": dict(provider="sarvam", model="deepseekv4.1-flash-1017",
                                           label="DeepSeek V4.1 Flash", price=_inr(14, 0.25, 55),
                                           note="₹14 / ₹55 per M tokens", effort="low", chips_effort="none",
                                           max_out=4000),
    # gpt-oss can't switch reasoning off (low is the least); its reasoning isn't needed back, so it isn't returned.
    "groq/openai/gpt-oss-120b": dict(provider="groq", model="openai/gpt-oss-120b", label="gpt-oss 120B",
                                     price={"input": 0.15, "cached": 0.075, "output": 0.60},
                                     note="$0.15 / $0.60 per M tokens", effort="low", chips_effort="low",
                                     max_out=4000, extra={"include_reasoning": False}),
    "groq/openai/gpt-oss-20b": dict(provider="groq", model="openai/gpt-oss-20b", label="gpt-oss 20B",
                                    price={"input": 0.075, "cached": 0.037, "output": 0.30},
                                    note="$0.075 / $0.30 per M tokens", effort="low", chips_effort="low",
                                    max_out=4000, extra={"include_reasoning": False}),
    # Qwen on Groq: no prompt caching; "none" = no thinking (Groq's default for it).
    "groq/qwen/qwen3.8-27b": dict(provider="groq", model="qwen/qwen3.8-27b", label="Qwen 3.8 27B",
                                  price={"input": 0.80, "cached": 0.80, "output": 4.00},
                                  note="$0.80 / $4.00 per M tokens", effort="none", chips_effort="none",
                                  max_out=4000),
}
DEFAULT = os.getenv("LLM_MODEL") or "openai/gpt-5.4-nano"
if DEFAULT not in MODELS:
    raise ValueError(f"LLM_MODEL={DEFAULT!r} is not one of {sorted(MODELS)}")
FRONTIER = os.getenv("FRONTIER_MODEL") or "openai/gpt-5.4"     # the planner the cheap agent escalates to
if FRONTIER not in MODELS:
    raise ValueError(f"FRONTIER_MODEL={FRONTIER!r} is not one of {sorted(MODELS)}")

_openai = OpenAI(timeout=TIMEOUT_S, max_retries=1)
_clients: dict[str, OpenAI] = {}   # other providers, made on first use (tests and CLI runs need no key for them)


def _client(provider: str) -> OpenAI:
    if provider not in _clients:
        p = PROVIDERS[provider]
        _clients[provider] = OpenAI(base_url=p["url"], api_key=os.environ.get(p["key"]),
                                    timeout=TIMEOUT_S, max_retries=1)
    return _clients[provider]


def available() -> list[dict]:
    """The models whose provider key is set, for the UI's picker."""
    return [{"id": k, "provider": m["provider"], "provider_label": PROVIDERS[m["provider"]]["label"],
             "label": m["label"], "note": m["note"]}
            for k, m in MODELS.items() if os.environ.get(PROVIDERS[m["provider"]]["key"]) and not m.get("hidden")]


def is_available(model_id: str) -> bool:
    return any(m["id"] == model_id for m in available())


def cost_usd(model_id: str, usage: dict) -> float:
    p = MODELS[model_id]["price"]
    fresh = usage["input"] - usage["cached"]
    return (fresh * p["input"] + usage["cached"] * p["cached"] + usage["output"] * p["output"]) / 1e6


class Step(NamedTuple):
    text: str               # the model's words ("" when it only called tools)
    calls: list             # [SimpleNamespace(name, arguments (JSON string), call_id)]
    items: list[dict]       # what to append to the history before the tool outputs
    usage: dict             # {input, cached, output, total}


def step(model_id: str, instructions: str, items: list[dict], tools: list[dict] | None,
         effort: str | None, max_out: int) -> Step:
    """One LLM call. `tools` is the registry in Responses format (tools.openai_tools()); None = no tools."""
    m = MODELS[model_id]
    if m["provider"] == "openai":
        kw = {"tools": tools} if tools else {}
        if effort:
            kw["reasoning"] = {"effort": effort}
        resp = _openai.responses.create(model=m["model"], instructions=instructions, input=items,
                                        max_output_tokens=max_out, **kw)
        u = resp.usage
        details = getattr(u, "input_tokens_details", None)
        usage = {"input": getattr(u, "input_tokens", 0) or 0, "cached": getattr(details, "cached_tokens", 0) or 0,
                 "output": getattr(u, "output_tokens", 0) or 0, "total": u.total_tokens}
        calls = [SimpleNamespace(name=i.name, arguments=i.arguments, call_id=i.call_id)
                 for i in resp.output if i.type == "function_call"]
        return Step(resp.output_text, calls, [i.model_dump(exclude_none=True) for i in resp.output], usage)

    kw = {"tools": [_chat_tool(t) for t in tools]} if tools else {}
    if effort:
        kw["reasoning_effort"] = effort
    kw[PROVIDERS[m["provider"]]["max_param"]] = max_out
    resp = _client(m["provider"]).chat.completions.create(
        model=m["model"], messages=[{"role": "system", "content": instructions}] + items,
        extra_body=m.get("extra") or None, **kw)
    msg = resp.choices[0].message
    u = resp.usage
    details = getattr(u, "prompt_tokens_details", None)            # null on a cache miss
    usage = {"input": u.prompt_tokens or 0, "cached": getattr(details, "cached_tokens", 0) or 0,
             "output": u.completion_tokens or 0, "total": u.total_tokens or 0}
    calls = [SimpleNamespace(name=c.function.name, arguments=c.function.arguments, call_id=c.id)
             for c in (msg.tool_calls or [])]
    # Sent back as returned, so a reasoning model's own fields (e.g. DeepSeek's reasoning_content) go with it.
    return Step(msg.content or "", calls, [msg.model_dump(exclude_none=True)], usage)


class StructuredError(ValueError):
    """structured() got an answer it can't use. The tokens were still spent, so `usage` comes with it.
    reason: "truncated" (hit max_out, usually reasoning), "refused", or "not_json"; raw = what came back."""
    def __init__(self, reason: str, usage: dict, raw: str):
        super().__init__(reason)
        self.reason, self.usage, self.raw = reason, usage, raw or ""


def structured(model_id: str, instructions: str, user_text: str, schema: dict, max_out: int) -> tuple[dict, dict]:
    """One LLM call that must answer with a JSON object matching `schema` (no tools). Returns (object, usage).
    OpenAI: strict JSON schema (the API enforces it). Others: JSON mode + the schema in the instructions, so the
    caller still checks the fields. Raises StructuredError (with the usage) if the answer is not a JSON object;
    API errors (timeout, 429, 5xx) are raised as they come."""
    m = MODELS[model_id]
    if m["provider"] == "openai":
        kw = {"reasoning": {"effort": m["effort"]}} if m["effort"] else {}
        resp = _openai.responses.create(
            model=m["model"], instructions=instructions, input=[{"role": "user", "content": user_text}],
            max_output_tokens=max_out,
            text={"format": {"type": "json_schema", "name": "answer", "schema": schema, "strict": True}}, **kw)
        u = resp.usage
        details = getattr(u, "input_tokens_details", None)
        usage = {"input": getattr(u, "input_tokens", 0) or 0, "cached": getattr(details, "cached_tokens", 0) or 0,
                 "output": getattr(u, "output_tokens", 0) or 0, "total": u.total_tokens}
        text = resp.output_text
        truncated = getattr(resp, "status", None) == "incomplete"
        refused = any(getattr(c, "type", None) == "refusal" for item in resp.output
                      if getattr(item, "type", None) == "message" for c in (getattr(item, "content", None) or []))
    else:
        kw = {PROVIDERS[m["provider"]]["max_param"]: max_out}
        if m["effort"]:
            kw["reasoning_effort"] = m["effort"]
        resp = _client(m["provider"]).chat.completions.create(
            model=m["model"], response_format={"type": "json_object"}, extra_body=m.get("extra") or None,
            messages=[{"role": "system", "content": f"{instructions}\n\nAnswer with one JSON object matching this "
                                                    f"JSON schema:\n{json.dumps(schema)}"},
                      {"role": "user", "content": user_text}], **kw)
        u = resp.usage
        details = getattr(u, "prompt_tokens_details", None)
        usage = {"input": u.prompt_tokens or 0, "cached": getattr(details, "cached_tokens", 0) or 0,
                 "output": u.completion_tokens or 0, "total": u.total_tokens or 0}
        text = resp.choices[0].message.content or ""
        truncated = resp.choices[0].finish_reason == "length"
        refused = bool(getattr(resp.choices[0].message, "refusal", None))
    if refused:
        raise StructuredError("refused", usage, text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise StructuredError("truncated" if truncated else "not_json", usage, text) from e
    if not isinstance(obj, dict):
        raise StructuredError("not_json", usage, text)
    return obj, usage


def tool_output(model_id: str, call_id: str, output: str) -> dict:
    """A tool's result, in the history shape of the model's provider."""
    if MODELS[model_id]["provider"] == "openai":
        return {"type": "function_call_output", "call_id": call_id, "output": output}
    return {"role": "tool", "tool_call_id": call_id, "content": output}


def _chat_tool(t: dict) -> dict:
    """Responses-format tool -> Chat Completions format (nested; no `strict`: Sarvam and Groq don't document it)."""
    return {"type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
