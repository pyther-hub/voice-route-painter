"""exp-02 variants: arm B's loop and arm C's apply_edits tool. Experiment code only; app/ is unchanged.

- run_turn_end_on_edit: app/agent.py run_turn (commit 256a261) with ONE change: after a step that made at least
  one edit and had no failed call, the turn ends (no extra LLM step to say "done").
- apply_edits: one tool that takes every edit as a list of ops, checks them all on a copy of the route, then
  writes once (atomic: all or nothing) and ends the turn with the code-written lines.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402,F401  (repo root on sys.path)
from app import agent, guard, llm, places, state, tools  # noqa: E402
from app.trace import span  # noqa: E402


# ---------------------------------------------------------------- arm B: loop ends after a successful edit step

def run_turn_end_on_edit(session_id: str, text: str, kind: str = "text") -> str:
    sess = state.session(session_id)
    model = sess.model
    m = llm.MODELS[model]
    with span("agent", "turn", input={"text": text, "kind": kind, "prompt_version": agent.PROMPT_VERSION,
                                      "model": model, "session": session_id}) as root:
        sess.current_text = text
        sess.turn_wrote = False
        before = state.stop_ids(session_id)
        mentioned_before = sess.last_mentioned
        items = agent._context_items(session_id, text, kind)
        sess.pending_question = None
        reply, memory, tokens = None, None, 0
        lines = []
        try:
            for step in range(1, agent.MAX_STEPS + 1):
                if (blocked := guard.over_budget(session_id)):
                    reply = blocked
                    root["error"] = "spend cap"
                    break
                label = "Understanding your message" if step == 1 else "Planning the route"
                with span("llm", f"step{step}", input=items[-1]) as sp, state.progress_step(session_id, label):
                    r = llm.step(model, agent.SYSTEM_PROMPT, items, tools.openai_tools(), m["effort"], m["max_out"])
                    calls = r.calls
                    tokens += r.usage["total"]
                    guard.charge(session_id, guard.llm_cost(r.usage, model))
                    sp["output"] = {"text": r.text, "model": model,
                                    "calls": [f"{c.name}({c.arguments})" for c in calls],
                                    "tokens": r.usage["total"], "usage": r.usage}
                if not calls:
                    if not lines:
                        reply = r.text.strip() or None
                    break
                items += r.items
                step_edits, step_failed = 0, False
                for c in calls:
                    result = tools.run_tool(session_id, c.name, c.arguments)
                    items.append(llm.tool_output(model, c.call_id, json.dumps(result, ensure_ascii=False)))
                    if result.get("ok") and result.get("edit"):
                        lines.append(result["line"])
                        step_edits += 1
                    elif result.get("ends_turn"):
                        reply, memory = result["reply"], result.get("memory")
                    elif not result.get("ok"):
                        step_failed = True
                if reply:
                    break
                if step_edits and not step_failed:          # THE CHANGE: edits done, no extra "done" step
                    break
                if tokens > agent.MAX_TURN_TOKENS:
                    root["error"] = f"token cap hit ({tokens})"
                    break
        except Exception as e:
            root["error"] = f"{type(e).__name__}: {e}"
            reply = agent.ERROR_REPLY
        sess.turn_wrote = False
        if lines:
            reply = " ".join(lines + ([reply] if reply and reply != agent.ERROR_REPLY else []))
        reply = reply or agent.FALLBACK_REPLY
        if sess.last_mentioned == mentioned_before:
            sess.last_mentioned = agent._last_named_stop(text, session_id) or sess.last_mentioned
        if lines and memory:
            memory = " ".join(lines + [memory])
        state.add_turn(session_id, text, memory or reply)
        state.set_display(session_id, message=reply, trace_id=root["trace_id"],
                          tokens=state.ui(session_id)["tokens"] + tokens,
                          heard_marks=agent._heard_marks(text, before + state.stop_ids(session_id)))
        root["output"] = {"reply": reply, "tokens": tokens,
                          "stops": [s["id"] for s in state.trip(session_id)["stops"]]}
        return reply


# ---------------------------------------------------------------- arm C: one atomic apply_edits tool

OPS = ("set_route", "add", "remove", "undo")
_NULLABLE_STR = {"type": ["string", "null"]}
OP_SCHEMA = {"type": "object", "additionalProperties": False,
             "required": ["op", "stops", "place_id", "after", "before", "position", "count"],
             "properties": {
                 "op": {"type": "string", "enum": list(OPS)},
                 "stops": {"type": ["array", "null"], "items": {"type": "string"},
                           "description": "set_route: ordered place ids ([] clears). Else null."},
                 "place_id": {**_NULLABLE_STR, "description": "add / remove: the place id. Else null."},
                 "after": {**_NULLABLE_STR, "description": "add: id of the stop it goes right after, or null."},
                 "before": {**_NULLABLE_STR, "description": "add: id of the stop it goes right before, or null."},
                 "position": {"type": ["string", "null"], "enum": ["end", None],
                              "description": "add: 'end' for the last stop, or null."},
                 "count": {"type": ["integer", "null"], "description": "undo: how many changes back. Else null."}}}
SCHEMA = tools._schema({"ops": {"type": "array", "items": OP_SCHEMA,
                                "description": "The edits, in order. All are applied or none."}})


def _err(i: int, op: dict, msg: str, hint: str) -> dict:
    return {"ok": False, "error": f"op {i + 1} ({op.get('op')}): {msg}; nothing was changed", "hint": hint}


def apply_edits(session_id: str, ops: list) -> dict:
    sess = state.session(session_id)
    ids, history = state.stop_ids(session_id), [h["stops"] for h in sess.history]
    undo_n, lines, touched = 0, [], []
    if not ops:
        return {"ok": False, "error": "ops is empty", "hint": "Send at least one op, or use reply."}
    for i, op in enumerate(ops):                         # 1. check everything on a copy
        kind = op.get("op")
        if kind == "undo":
            n = op.get("count") or 1
            if lines:
                return _err(i, op, "undo must come before other ops", "Put undo first, or use it alone.")
            if not isinstance(n, int) or not 1 <= n <= len(history) - undo_n:
                return _err(i, op, f"only {len(history) - undo_n} change(s) to undo", "Tell the user with reply.")
            undo_n += n
            ids = history[-undo_n]
            lines.append("Undone." if n == 1 else f"Undid {n} changes.")
        elif kind == "set_route":
            new = op.get("stops")
            if not isinstance(new, list):
                return _err(i, op, "stops must be a list", "Pass the ordered ids, or [] to clear.")
            ids = list(new)
            lines.append(" → ".join(tools._name(x) for x in ids) if ids else "Cleared the trip.")
            touched += ids[-1:]
        elif kind == "add":
            pid, after, before, pos = op.get("place_id"), op.get("after"), op.get("before"), op.get("position")
            if sum(x is not None for x in (after, before, pos)) != 1 or pos not in (None, "end"):
                return _err(i, op, "pass exactly one of after, before or position ('end')",
                            "Name a stop on the route as after or before, or use position 'end'.")
            if not ids:
                return _err(i, op, "the route is empty", "Start a new trip with a set_route op.")
            anchor = after or before
            if anchor is not None and anchor not in ids:
                return _err(i, op, f"'{anchor}' is not on the route (at this op)", "Use a stop on the route.")
            k = len(ids) if pos == "end" else ids.index(after) + 1 if after is not None else ids.index(before)
            where = "at the end" if pos == "end" else f"after {tools._name(after)}" if after else \
                f"before {tools._name(before)}"
            ids = ids[:k] + [pid] + ids[k:]
            lines.append(f"Added {tools._name(pid)} {where}.")
            touched.append(pid)
        elif kind == "remove":
            pid = op.get("place_id")
            if pid not in ids:
                return _err(i, op, f"'{pid}' is not on the route (at this op)", "Tell the user with reply.")
            ids = [x for x in ids if x != pid]
            lines.append(f"Removed {tools._name(pid)}.")
            touched.append(pid)
        else:
            return _err(i, op, f"unknown op {kind!r}", f"Use one of {list(OPS)}.")
    final = ids
    if final:
        try:
            state.validate_stops(final)
        except ValueError as e:
            return {"ok": False, "error": f"the route after these ops is invalid: {e}; nothing was changed",
                    "hint": "Use ids from resolve_place or the current stops, and keep at least 2 stops "
                            "(or clear with set_route [])."}
    added = tools._unnamed_new_stops(session_id, [x for x in final if x not in state.stop_ids(session_id)])
    if len(added) > tools.MAX_AGENT_ADDED:
        return {"ok": False, "error": f"{len(added)} new stops the user did not name ({', '.join(added)}); at most "
                                      f"{tools.MAX_AGENT_ADDED} allowed; nothing was changed",
                "hint": "Keep the places the user named."}
    for _ in range(undo_n):                              # 2. write (cannot fail now): undo steps, then the rest
        state.undo(session_id, lines[0])
    if final != state.stop_ids(session_id):
        state.update_state(session_id, final, " ".join(lines))
    tools._offer_long_legs(session_id)
    if touched:
        sess.last_mentioned = touched[-1]
    line = " ".join(lines)
    return {"ok": True, "ends_turn": True, "reply": line, "memory": line,
            "stops": [s["name"] for s in state.trip(session_id)["stops"]]}


APPLY_EDITS = {
    "fn": apply_edits,
    "description": "Change the route: a list of edits (set_route, add, remove, undo) applied in order, all or none. "
                   "Ends your turn. On an error nothing changed: fix the ops and call again.",
    "parameters": SCHEMA,
}
BATCH_TOOLS = {k: v for k, v in tools.TOOLS.items() if k not in ("set_route", "add_stop", "remove_stop", "undo")}
BATCH_TOOLS = {"resolve_place": BATCH_TOOLS.pop("resolve_place"), "apply_edits": APPLY_EDITS, **BATCH_TOOLS}

_original_validate = tools._validate


def validate(schema: dict, args) -> str | None:
    """The gateway's flat validator, plus the list of op objects apply_edits takes."""
    if schema is not SCHEMA:
        return _original_validate(schema, args)
    if not isinstance(args, dict) or set(args) != {"ops"}:
        return "expected exactly the argument ['ops']"
    if not isinstance(args["ops"], list) or not all(isinstance(o, dict) for o in args["ops"]):
        return "'ops' must be a list of objects"
    for i, o in enumerate(args["ops"]):
        if o.get("op") not in OPS:
            return f"op {i + 1}: 'op' must be one of {list(OPS)}"
        if extra := set(o) - set(OP_SCHEMA["properties"]):
            return f"op {i + 1}: unexpected fields {sorted(extra)}"
    return None
