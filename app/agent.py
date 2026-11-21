"""The orchestrator: one synchronous loop, goal + tools, no scripted flow.

    reply = run_turn(session_id, "Lucknow se Jaipur jaana hai, beech mein Agra")

Each step = one LLM call to the session's model (app/llm.py: Sarvam or Groq). Tool calls go through
tools.run_tool(). The turn ends when the model stops calling tools (after its edits; the headline is the
code-written edit lines), when it calls ask_user or reply, or when a limit is hit.

Try it from the command line (each argument is one turn in the same session):
    python -m app.agent "Lucknow se Jaipur, beech mein Agra" "Agra hata do" "wapas pehle jaisa kar do"
"""
import json
import sys
from pathlib import Path

from app import guard, llm, places, state, tools, tts
from app.trace import span

# TODO(async): no cancellation; a new message waits for the current turn to finish.

PROMPT_VERSION = "orchestrator_v29"   # v29: dismiss_proposal (turn a proposal down by voice/text); v28: plan_with_frontier also for "A to B plus the best places"; named places are kept as anchors (refused only above 3 named places); v27: user_input.spoken_language on voice turns (detected by Sarvam STT): replies and the planner filler in that language; v26: plan_with_frontier (itinerary / compare proposals from llm.FRONTIER) + accept_proposal, input has state.pending_proposal; v25: input has detected_places (exact names found in the text, already looked up), so resolve_place is only for the rest; v24: suggest_enroute_places ids may be null (= every leg) and a stretch of the trip gets places per leg; v23: suggest tools take range_km (default 30) and show cards, ending the turn; v22: several edits per turn (the turn no longer ends after the first edit; model stops with no tool call); v21: suggest_enroute_places + suggest_nearby_places (data for the agent); v20: owner's prompt: small edit tools (set_route/add_stop/remove_stop/undo(count)), reply, resolve_place statuses, JSON input; no suggestions/food; v19: flights removed; v18: set_stops fly_from; v17: suggest_food; v16: set_flights; v15: round trips; v14: 1-2 long-drive stops; v13: long-drive suggestions by the app; v12: suggestions instead of questions; v11: resolve_place takes many names; v10: suggest_places; v9: ask_user `trips`; v8: suggest_stops (older versions kept for traces)
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md").read_text()

# Limits live in code, not in the prompt.
MAX_STEPS = 6                 # LLM calls per turn
MAX_TURN_TOKENS = 30_000      # input + output, summed over the turn
# Output tokens per call, reasoning effort and the LLM timeout are per model: llm.MODELS.

FALLBACK_REPLY = "Maaf kijiye, ye abhi nahi ho paaya. Thoda aasaan karke dobara boliye?"
ERROR_REPLY = "Kuch gadbad ho gayi, thodi der baad dobara try kijiye."


def detected_places(session_id: str, text: str) -> list[dict]:
    """Places named exactly in the user's text (no LLM), already looked up the way resolve_place does it:
    [{text, status, matches}], so the model needs no resolve_place call for them. Stops already on the trip
    are left out (the model has their ids). A hint only: a failure here must never break a turn."""
    try:
        on_trip, seen, found = set(state.stop_ids(session_id)), set(), []
        for f in places.find_exact(text):
            words = text[f["start"]:f["end"]]
            if (key := places.norm(words)) in seen:
                continue
            seen.add(key)
            r = tools.lookup(words)
            if r["status"] == "not_found" or (r["status"] == "ok" and r["matches"][0]["id"] in on_trip):
                continue
            found.append({"text": words, "status": r["status"], "matches": r["matches"]})
        return found
    except Exception:
        return []


def _context_items(session_id: str, text: str, kind: str) -> list[dict]:
    """Recent turns + this turn's input, in the JSON shape the prompt describes."""
    sess = state.session(session_id)
    items = []
    for user_text, reply in sess.turns:
        items += [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}]
    turn = {"state": {"stops": [{"id": x["id"], "name": x["name"]} for x in state.trip(session_id)["stops"]],
                      "pending_question": sess.pending_question,
                      "last_mentioned": sess.last_mentioned,
                      "pending_proposal": tools.pending_proposal(session_id)},
            "user_input": {"type": kind, "value": text}}
    if sess.speak_in:                     # a voice message: answer in the language the user spoke
        turn["user_input"]["spoken_language"] = tts.write_as(sess.speak_in)
    if kind == "text" and (found := detected_places(session_id, text)):   # a click already carries its ids
        turn["detected_places"] = found
    items.append({"role": "user", "content": json.dumps(turn, ensure_ascii=False)})
    return items


def _last_named_stop(text: str, session_id: str) -> str | None:
    """The trip stop named last in the user's text (exact names/aliases), for last_mentioned."""
    on_trip = set(state.stop_ids(session_id))
    hits = [f for f in places.find_exact(text) if f["id"] in on_trip]
    return max(hits, key=lambda f: f["start"])["id"] if hits else None


def marks_for(text: str, spans: list[dict]) -> list[dict]:
    """Spans from places.find_*() -> state.heard_marks entries (position in the text + spot on the map).
    Overlapping spans are dropped (the earlier one wins)."""
    marks, end = [], 0
    for f in sorted(spans, key=lambda f: (f["start"], -f["end"])):
        p = places.get(f["id"])
        if p and f["start"] >= end:
            marks.append({"start": f["start"], "end": f["end"], "name": p["name"], "lat": p["lat"], "lon": p["lon"]})
            end = f["end"]
    return marks


def heard_marks_now(text: str) -> list[dict]:
    """Right after the words are heard, before any LLM call: places named exactly. Display only."""
    try:
        return marks_for(text, places.find_exact(text))
    except Exception:
        return []


def _heard_marks(text: str, candidate_ids: list[str]) -> list[dict]:
    """Final marks at the end of a turn: the exact ones plus typo matches among the trip's places
    (before or after this turn, so removed ones too). Only trip places get the loose spelling match,
    so it can't pick a stranger. Display only: a failure here must never break a turn."""
    try:
        return marks_for(text, places.find_exact(text) + places.find_spans(text, candidate_ids))
    except Exception:
        return []


def run_turn(session_id: str, text: str, kind: str = "text") -> str:
    """Run one user turn to completion and return the reply line. Never raises.
    kind = "text" (typed or spoken) or "choice" (a clicked option of the pending question)."""
    sess = state.session(session_id)
    model = sess.model                                    # fixed for the whole turn, even if the user switches
    m = llm.MODELS[model]
    with span("agent", "turn", input={"text": text, "kind": kind, "prompt_version": PROMPT_VERSION,
                                      "model": model, "session": session_id}) as root:
        sess.current_text = text                          # which places the user named (plan_with_frontier's check)
        sess.frontier_used = False                        # one frontier call per turn
        sess.turn_wrote = False                           # the first edit of this turn saves the undo step
        before = state.stop_ids(session_id)               # for the "heard" highlights (removed places too)
        mentioned_before = sess.last_mentioned
        items = _context_items(session_id, text, kind)
        sess.pending_question = None                      # answered or ignored now; ask_user may set a new one
        reply, memory, tokens = None, None, 0
        lines = []                                        # code-written line of every edit this turn
        try:
            for step in range(1, MAX_STEPS + 1):
                if (blocked := guard.over_budget(session_id)):     # spend cap: no more paid calls
                    reply = blocked
                    root["error"] = "spend cap"
                    break
                label = "Understanding your message" if step == 1 else "Planning the route"
                with span("llm", f"step{step}", input=items[-1]) as sp, state.progress_step(session_id, label, kind="llm", name=model) as ui:
                    r = llm.step(model, SYSTEM_PROMPT, items, tools.openai_tools(), m["effort"], m["max_out"])
                    calls = r.calls
                    ui["result"] = (", ".join(c.name for c in calls) or "answers") + f" · {r.usage['total']} tokens"
                    tokens += r.usage["total"]
                    guard.charge(session_id, guard.llm_cost(r.usage, model))
                    sp["output"] = {"text": r.text, "model": model,
                                    "calls": [f"{c.name}({c.arguments})" for c in calls],
                                    "tokens": r.usage["total"], "usage": r.usage}

                if not calls:                  # done. With no edit, plain text counts as the reply; after edits
                    if not lines:              # the code-written lines are the headline (the model's text repeated them)
                        reply = r.text.strip() or None
                    break

                items += r.items
                for c in calls:
                    result = tools.run_tool(session_id, c.name, c.arguments)
                    items.append(llm.tool_output(model, c.call_id, json.dumps(result, ensure_ascii=False)))
                    if result.get("ok") and result.get("edit"):
                        lines.append(result["line"])
                    elif result.get("ends_turn"):
                        reply, memory = result["reply"], result.get("memory")
                # An edit does not end the turn: a request may need several (remove + add, two adds),
                # and the model makes them one step at a time. It stops by answering with no tool call.
                if reply:
                    break
                if tokens > MAX_TURN_TOKENS:
                    root["error"] = f"token cap hit ({tokens})"
                    break
        except Exception as e:                 # LLM timeout / API error: answer, don't crash
            root["error"] = f"{type(e).__name__}: {e}"
            reply = ERROR_REPLY
        sess.turn_wrote = False                # clicks after this turn save their own undo step

        if lines:                              # what changed, written by code; then the model's words, if any
            reply = " ".join(lines + ([reply] if reply and reply != ERROR_REPLY else []))
        reply = reply or FALLBACK_REPLY        # step or token cap hit without a final answer
        if sess.last_mentioned == mentioned_before:          # no edit named a stop: take the one the user named
            sess.last_mentioned = _last_named_stop(text, session_id) or sess.last_mentioned
        if lines and memory:                   # edits, then a question: the agent remembers both
            memory = " ".join(lines + [memory])
        state.add_turn(session_id, text, memory or reply)
        state.set_display(session_id, message=reply, trace_id=root["trace_id"],
                          tokens=state.ui(session_id)["tokens"] + tokens,
                          heard_marks=_heard_marks(text, before + state.stop_ids(session_id)))
        root["output"] = {"reply": reply, "tokens": tokens,
                          "stops": [s["id"] for s in state.trip(session_id)["stops"]]}
        return reply


FOLLOWUP_PROMPT = (
    "You help someone planning a road trip across India. Given their trip and the app's last reply, write 3 short "
    "things the user might want to say next, each one a message from the user (first person, at most 8 words), "
    "for example adding, removing or swapping a stop, or making it a round trip. Only road trips are planned. "
    "Use the language of the user's last message (English, or Hinglish in Latin script). Do not repeat what the "
    "reply already did. One per line, no numbering, no quotes.")
MAX_FOLLOWUPS = 3
FOLLOWUP_MAX_OUT = {"sarvam": 1000, "groq": 1000}   # a reasoning model spends part of it thinking


def followups(session_id: str, text: str, input_no: int) -> None:
    """After a turn: one small LLM call (no tools, ~200 tokens) for a few things the user might say next, shown as
    chips. Display only; skipped if there is no trip yet, and dropped if the user has sent a new message meanwhile.
    Never raises."""
    try:
        s = state.get_state(session_id)
        t, u = s["trip"], s["ui"]
        if len(t["stops"]) < 2 or guard.over_budget(session_id):
            return
        trip = " -> ".join(x["name"] for x in t["stops"])
        model = state.session(session_id).model
        with span("llm", "followups", input={"trip": trip, "session": session_id, "model": model}) as sp:
            ask = f"Trip: {trip} ({t['total_km']} km)\nUser said: {text}\nApp replied: {u['message']}"
            r = llm.step(model, FOLLOWUP_PROMPT, [{"role": "user", "content": ask}], None,
                         llm.MODELS[model]["chips_effort"], FOLLOWUP_MAX_OUT[llm.MODELS[model]["provider"]])
            guard.charge(session_id, guard.llm_cost(r.usage, model))
            lines = [l.strip(" -*\"'\t") for l in r.text.splitlines()]
            chips = [l for l in lines if 0 < len(l) <= 60][:MAX_FOLLOWUPS]
            sp["output"] = {"followups": chips, "tokens": r.usage["total"], "usage": r.usage}
        sess = state.session(session_id)
        if sess.input_no == input_no and session_id in state._sessions:    # nothing newer came in, session still open
            state.set_display(session_id, followups=chips, tokens=u["tokens"] + r.usage["total"])
    except Exception:
        pass


if __name__ == "__main__":
    sid = "cli"
    for turn in sys.argv[1:]:
        print(f"\n>>> {turn}")
        reply = run_turn(sid, turn)
        s = state.trip(sid)
        route = " → ".join(x["name"] for x in s["stops"]) or "(empty)"
        print(f"<<< {reply}\n    trip v{s['version']}: {route}  ({s['total_km']} km by road)")
