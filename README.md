# Voice Route Painter

Hold Space and say "Lucknow se Jaipur jaana hai, beech mein Agra". An AI agent draws the road route on a map of India. Change it by voice or text: "Agra hata do", "wapas pehle jaisa kar do".

- **Live demo:** https://voice-route-painter.fly.dev/
- **Engineering doc:** https://pyther-hub.github.io/voice-route-painter/ (architecture, design principles, evals and experiment results)

The focus is the agent layer: one orchestrator with a goal and tools but no scripted flow, with tracing, evals, guardrails and measured experiments. The UI is kept minimal.

## Stack

Python 3.11+, FastAPI, asyncio, Pydantic, SQLite. Plain HTML, CSS and JavaScript with Leaflet (no framework, no build step). Sarvam speech-to-text. The orchestrator runs on Sarvam or Groq models (switchable in the UI); routes come from a graph of India's main roads built from OpenStreetMap.

## Run it locally

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in SARVAM_API_KEY (and GROQ_API_KEY if you want Groq models)
uvicorn app.main:app --reload
```

Open http://localhost:8000. The microphone needs https or localhost. The text box feeds the same pipeline, so you can try it without a mic.

Tests: `pytest`. Evals (they call paid LLM APIs, so they only print a plan unless you pass `--yes`): `python -m evals.run`.

## Project notes

- Roads are built once with `python -m app.geo build` from an OpenStreetMap extract (not included).
- Secrets come from environment variables only; `.env` is gitignored.
- Roads: © OpenStreetMap contributors (ODbL).
