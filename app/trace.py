"""Tracing: every agent run, LLM call and tool call becomes one row in traces.db.

    with span("tool", "resolve_place", input=args) as sp:
        sp["output"] = result

Nesting is automatic: a span opened inside another gets that span as `parent` and shares its
`trace_id` (tracked with contextvars, which also follow asyncio.to_thread).
Tracing must never break a turn: every DB/print error is swallowed.
"""
import contextvars
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# Viewer: static/trace.html (the "Trace" link in the UI) reads read_trace() via /api/trace/<id>.
# Raw access:  sqlite3 traces.db "select * from spans"   (deployed: fly ssh console, then sqlite3 /data/traces.db)

# DATA_DIR = where files that must outlive a deploy live (on Fly: a volume mounted at /data). Unset = project root.
DB_PATH = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parent.parent) / "traces.db"
PRINT = os.environ.get("TRACE_PRINT", "1") != "0"   # TRACE_PRINT=0 silences the console output
_current = contextvars.ContextVar("current_span", default=None)

SCHEMA = """create table if not exists spans (
    trace_id text, span_id text, parent text, kind text, name text,
    input text, output text, duration_ms real, error text, started_at real)"""


def _json(value) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return repr(value)


def _short(text: str | None, n: int = 160) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "…"


def _write(row: dict) -> None:
    try:
        with sqlite3.connect(DB_PATH, timeout=2) as db:
            db.execute(SCHEMA)
            db.execute("insert into spans values (:trace_id, :span_id, :parent, :kind, :name,"
                       " :input, :output, :duration_ms, :error, :started_at)", row)
    except Exception as e:
        print(f"[trace] write failed (ignored): {e!r}")


def _parse(text: str | None):
    try:
        return json.loads(text) if text is not None else None
    except (ValueError, TypeError):
        return text


def read_trace(trace_id: str) -> dict | None:
    """One turn's spans (in start order) + a list of the turns in the same session, newest first.
    Returns None if the id is malformed or unknown."""
    if not (len(trace_id) == 12 and all(c in "0123456789abcdef" for c in trace_id)):
        return None
    cols = ["trace_id", "span_id", "parent", "kind", "name", "input", "output", "duration_ms", "error", "started_at"]
    try:
        with sqlite3.connect(DB_PATH, timeout=2) as db:
            db.execute(SCHEMA)
            rows = db.execute("select * from spans where trace_id = ? order by started_at", (trace_id,)).fetchall()
            if not rows:
                return None
            spans = [{**dict(zip(cols, r)), "input": _parse(r[5]), "output": _parse(r[6])} for r in rows]
            root = next((s for s in spans if s["parent"] is None), spans[0])
            session = (root["input"] or {}).get("session") if isinstance(root["input"], dict) else None
            turns = []
            if session:
                like = f'%"session": {json.dumps(session)}%'
                for tid, inp, out, err, started in db.execute(
                        "select trace_id, input, output, error, started_at from spans "
                        "where kind = 'agent' and parent is null and input like ? "
                        "order by started_at desc limit 50", (like,)):
                    inp, out = _parse(inp) or {}, _parse(out) or {}
                    if isinstance(inp, dict) and inp.get("session") == session:
                        turns.append({"trace_id": tid, "text": inp.get("text"), "started_at": started,
                                      "reply": out.get("reply") if isinstance(out, dict) else None,
                                      "error": err})
    except sqlite3.Error as e:
        print(f"[trace] read failed: {e!r}")
        return None
    return {"trace_id": trace_id, "session": session, "spans": spans, "turns": turns}


@contextmanager
def span(kind: str, name: str, input=None):
    parent = _current.get()
    sp = {
        "trace_id": parent["trace_id"] if parent else uuid.uuid4().hex[:12],
        "span_id": uuid.uuid4().hex[:8],
        "parent": parent["span_id"] if parent else None,
        "kind": kind, "name": name, "input": input, "output": None, "error": None,
    }
    token = _current.set(sp)
    start = time.perf_counter()
    started_at = time.time()
    try:
        yield sp
    except Exception as e:
        sp["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        _current.reset(token)
        try:
            row = {**sp, "input": _json(sp["input"]), "output": _json(sp["output"]),
                   "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                   "started_at": started_at}
            depth = "  " if row["parent"] else ""
            status = f"ERROR {row['error']}" if row["error"] else "ok"
            PRINT and print(f"[trace] {depth}{kind}:{name} {row['duration_ms']:.0f}ms {status}"
                  f"\n         {depth}in={_short(row['input'])}\n         {depth}out={_short(row['output'])}")
            _write(row)
        except Exception as e:
            print(f"[trace] failed (ignored): {e!r}")
