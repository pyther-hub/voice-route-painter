"""Speech-to-text: audio bytes -> Roman Hinglish transcript, via Sarvam.

    text = await stt.transcribe(audio_bytes, "audio/webm", keyterms=["Agra", "Jaipur"])

Settings come from the spike in exp.ipynb: model saaras:v4, mode "translit" (Roman letters, so
"Lucknow se Jaipur jaana hai" comes back the way the agent is tested on). Keyterms bias recognition
towards place names (Sarvam allows at most 50).

Raises STTError with a message that is safe to show the user. Each call is one span; the span stores the
size and type of the audio, never the audio itself.
"""
import json
import os

import httpx2 as httpx      # the httpx that the openai package already installed
from dotenv import load_dotenv

from app.trace import span

load_dotenv()

URL = "https://api.sarvam.ai/speech-to-text"
MODEL = "saaras:v4"
MODE = "translit"
TIMEOUT_S = 15              # every external call has a timeout (CLAUDE.md rule 7)
MAX_AUDIO_BYTES = 1_000_000   # ~1 MB: far more than 15 s of compressed speech; caps what one message can cost
MIN_AUDIO_BYTES = 1_500       # a key tap records a few hundred bytes of nothing
MAX_KEYTERMS = 50

# What MediaRecorder produces (Chrome/Firefox: webm, Safari: mp4) -> file extension for the upload.
_EXT = {"audio/webm": "webm", "audio/mp4": "mp4", "audio/ogg": "ogg", "audio/wav": "wav", "audio/mpeg": "mp3"}

# Always offered to the recognizer, after the places of the current trip.
COMMON_PLACES = ["Delhi", "Mumbai", "Jaipur", "Agra", "Lucknow", "Udaipur", "Kasol", "Manali", "Chandigarh",
                 "Varanasi", "Goa", "Kochi", "Bengaluru", "Hyderabad", "Kolkata", "Amritsar"]


class STTError(Exception):
    """Something went wrong with speech-to-text. str(e) is a line to show the user."""


def keyterms_for(trip_names: list[str]) -> list[str]:
    """The trip's current stops first (most likely to be mentioned again), then common places."""
    seen, terms = set(), []
    for name in trip_names + COMMON_PLACES:
        if name.lower() not in seen:
            seen.add(name.lower())
            terms.append(name)
    return terms[:MAX_KEYTERMS]


async def transcribe(audio: bytes, mime: str = "audio/webm", keyterms: list[str] | None = None) -> str:
    return (await transcribe_detect(audio, mime, keyterms))[0]


async def transcribe_detect(audio: bytes, mime: str = "audio/webm",
                            keyterms: list[str] | None = None) -> tuple[str, str | None]:
    """(transcript, language Sarvam detected, e.g. "hi-IN"; None if it couldn't tell). The language picks
    the voice of the spoken reply (tts.py)."""
    if len(audio) < MIN_AUDIO_BYTES:
        raise STTError("Awaaz bahut chhoti thi, space dabake thoda bolke chhodiye.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise STTError("Recording bahut lambi hai, chhoti rakhiye (15 second tak).")
    mime = mime.split(";")[0].strip().lower()       # "audio/webm;codecs=opus" -> "audio/webm"
    ext = _EXT.get(mime, "webm")

    data = {"model": MODEL, "mode": MODE, "language_code": "unknown"}
    if keyterms:
        data["keyterms"] = json.dumps(keyterms[:MAX_KEYTERMS], ensure_ascii=False)

    with span("stt", "sarvam", input={"bytes": len(audio), "mime": mime, "model": MODEL, "mode": MODE,
                                      "keyterms": data.get("keyterms")}) as sp:
        key = os.environ.get("SARVAM_API_KEY")
        if not key:
            raise STTError("SARVAM_API_KEY set nahi hai (.env dekhiye).")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.post(URL, headers={"api-subscription-key": key}, data=data,
                                      files={"file": (f"speech.{ext}", audio, mime)})
        except httpx.HTTPError as e:
            # Keep the real cause in the trace (timeout vs. connection refused vs. DNS); the user gets one line.
            sp["output"] = {"http_error": f"{type(e).__name__}: {e}"[:300]}
            if isinstance(e, httpx.TimeoutException):
                raise STTError(f"Speech service ne {TIMEOUT_S} second mein jawab nahi diya, "
                               "thodi der baad try kijiye ya text mein likhiye.") from e
            raise STTError("Speech service tak pahunch nahi paya, text mein likhiye.") from e
        if r.status_code != 200:
            # The body can say what is wrong (bad format, quota). It goes to the trace, not to the user.
            sp["output"] = {"status": r.status_code, "body": r.text[:300]}
            raise STTError("Awaaz samajh nahi aayi, dobara try kijiye ya text mein likhiye.")
        body = r.json()
        text = (body.get("transcript") or "").strip()
        sp["output"] = {"transcript": text, "language": body.get("language_code")}
        if not text:
            raise STTError("Kuch sunai nahi diya, dobara bolke dekhiye.")
        return text, body.get("language_code")
