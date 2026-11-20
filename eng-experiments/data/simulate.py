"""Build data/examples.json from the hand-written blocks in data/source/*.txt.

    .venv/bin/python eng-experiments/data/simulate.py

Block format (one example per block, blank line between blocks; `#` lines are comments):

    @ tags, comma separated
    S delhi agra jaipur                 trip before the turn (place ids; omit the line = empty trip)
    H delhi jaipur / delhi              earlier trips, oldest first (for undo; optional)
    Q Kaunsa Udaipur? = udaipur, udaipur-tripura      pending question + its options' place ids (optional)
    T Agra hata do aur Jaipur ke baad Udaipr jodo     the user's message
    O rm agra; add udaipur after jaipur                the intended edits (see OPS)
    A ask udaipur udaipur-tripura                      another accepted outcome (optional)
    M Agra=agra; Jaipur=jaipur; Udaipr=udaipur         place names in the text -> gold id ("a|b" = ambiguous)

OPS (applied in order, with the same rules as app/tools.py):
    set a b c          replace the route (`set` alone = clear)
    add x after y | add x before y | add x end
    rm x               remove every copy of x (a round trip's start goes at both ends)
    undo n             go back n trips in the history
    ask a b c          the user must be asked; a, b, c = the candidates (none of them may be added)
    none               nothing may change

`expect` is computed from the ops, so the expected trip is right by construction:
    edits  -> {"stops": final, "asks": false}
    ask    -> {"asks": true, "not_has": [candidates not already on the trip]}
    none   -> {"unchanged": true}
    with an A line -> {"any_of": [expect(O), expect(A)]}
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
SOURCE = HERE / "source"
OUT = HERE / "examples.json"


class BadExample(ValueError):
    pass


def parse_ops(text: str) -> list[list[str]]:
    return [op.split() for op in text.split(";") if op.strip()]


def apply_ops(start: list[str], history: list[list[str]], ops: list[list[str]]) -> tuple[list[str], bool]:
    """(final stops, asks). history = earlier trips oldest first, without the empty one at the start."""
    stops, hist, asks = list(start), [[]] + [list(h) for h in history], False
    for op in ops:
        kind, args = op[0], op[1:]
        if kind == "set":
            if stops != args:
                hist.append(stops)
            stops = list(args)
        elif kind == "add":
            if len(args) == 2 and args[1] == "end":
                k = len(stops)
            elif len(args) == 3 and args[1] in ("after", "before"):
                if args[2] not in stops:
                    raise BadExample(f"anchor {args[2]!r} not on the trip {stops}")
                k = stops.index(args[2]) + (1 if args[1] == "after" else 0)
            else:
                raise BadExample(f"bad add: {op}")
            if args[0] in stops:
                raise BadExample(f"{args[0]!r} is already on the trip")
            hist.append(stops)
            stops = stops[:k] + [args[0]] + stops[k:]
        elif kind == "rm":
            if args[0] not in stops:
                raise BadExample(f"rm {args[0]!r}: not on the trip {stops}")
            hist.append(stops)
            stops = [s for s in stops if s != args[0]]
        elif kind == "undo":
            n = int(args[0])
            if n > len(hist):
                raise BadExample(f"undo {n}: only {len(hist)} trips in the history")
            stops = hist[-n]
            hist = hist[:-n]
        elif kind == "ask":
            asks = True
        elif kind == "none":
            pass
        else:
            raise BadExample(f"unknown op {kind!r}")
    return stops, asks


def expect_for(start: list[str], history: list[list[str]], ops: list[list[str]]) -> dict:
    kinds = {op[0] for op in ops}
    if "ask" in kinds:
        cands = [x for op in ops if op[0] == "ask" for x in op[1:]]
        return {"asks": True, "not_has": [c for c in cands if c not in start]}
    if kinds == {"none"}:
        return {"unchanged": True}
    stops, _ = apply_ops(start, history, ops)
    return {"stops": stops, "asks": False}


def find_spans(text: str, pairs: list[tuple[str, str]]) -> list[dict]:
    """Each named surface -> its character span, searched left to right (falls back to anywhere, any case)."""
    out, pos = [], 0
    for surface, gold in pairs:
        i = text.find(surface, pos)
        if i < 0:
            i = text.find(surface)
        if i < 0:
            i = text.lower().find(surface.lower())
        if i < 0:
            raise BadExample(f"mention {surface!r} not in the text")
        out.append({"text": surface, "start": i, "end": i + len(surface), "ids": gold.split("|")})
        pos = i + len(surface)
    return out


def parse_block(block: str) -> dict:
    fields = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        if key not in "@SHQKTOAM" or len(key) != 1:
            raise BadExample(f"unknown line {line!r}")
        fields[key] = value.strip()
    if "T" not in fields or "O" not in fields:
        raise BadExample("needs a T and an O line")
    start = fields.get("S", "").split()
    history = [h.split() for h in fields["H"].split("/")] if fields.get("H") else []
    ops = parse_ops(fields["O"])
    expect = expect_for(start, history, ops)
    if "A" in fields:
        expect = {"any_of": [expect, expect_for(start, history, parse_ops(fields["A"]))]}
    pq = None
    if fields.get("Q"):
        question, _, ids = fields["Q"].partition("=")
        pq = {"question": question.strip(), "options": [i.strip() for i in ids.split(",") if i.strip()]}
    pairs = []
    for m in fields.get("M", "").split(";"):
        if m.strip():
            surface, _, gold = m.rpartition("=")
            pairs.append((surface.strip(), gold.strip()))
    text = fields["T"]
    # "long" comes from the text's length (build()), never from a hand tag
    return {"tags": [t.strip() for t in fields.get("@", "").split(",") if t.strip() and t.strip() != "long"],
            "start": {"stops": start, "history": history, "pending_question": pq},
            "kind": fields.get("K", "text"), "text": text,
            "ops": [" ".join(op) for op in ops], "alt_ops": [" ".join(op) for op in parse_ops(fields.get("A", ""))],
            "mentions": find_spans(text, pairs), "expect": expect}


def build() -> list[dict]:
    examples, errors = [], []
    for f in sorted(SOURCE.glob("*.txt")):
        blocks = [b for b in re.split(r"\n\s*\n", f.read_text()) if b.strip() and
                  not all(l.startswith("#") for l in b.strip().splitlines())]
        for n, block in enumerate(blocks, 1):
            try:
                ex = parse_block(block)
            except (BadExample, ValueError) as e:
                errors.append(f"{f.name} block {n}: {e}\n    {block.strip().splitlines()[-1][:90]}")
                continue
            examples.append(ex)
    for i, ex in enumerate(examples, 1):
        ex["id"] = f"e{i:03d}"
        ex["length"] = "short" if len(ex["text"]) < 40 else "medium" if len(ex["text"]) < 120 else "long"
    if errors:
        sys.exit("Errors:\n" + "\n".join(errors))
    return [{k: ex[k] for k in ("id", "tags", "length", "start", "kind", "text", "ops", "alt_ops", "mentions",
                                 "expect")} for ex in examples]


if __name__ == "__main__":
    ex = build()
    OUT.write_text(json.dumps(ex, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(ex)} examples -> {OUT}")
