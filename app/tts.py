"""Text-to-speech: the agent's question -> spoken audio (WAV), via Sarvam Bulbul. Only questions are spoken
(main.speak_reply), never replies saying what was done.

    wav = await tts.speak("Lucknow se Jaipur, beech mein Agra.", "hi-IN")

The language is the one Sarvam speech-to-text detected in the user's voice message (stt.transcribe_detect),
so a question is spoken in the language the user spoke. Only voice turns get one (main.py).

Raises TTSError (never shown as an error to the user: the text reply is already on screen). Each call is one
span; the span stores the text length, language and voice, never the audio.
"""
import base64
import os

import httpx2 as httpx      # the httpx that the openai package already installed
from dotenv import load_dotenv

from app.trace import span

load_dotenv()

URL = "https://api.sarvam.ai/text-to-speech"
MODEL = os.environ.get("SARVAM_TTS_MODEL") or "bulbul:v3"
SPEAKER = os.environ.get("SARVAM_TTS_SPEAKER") or "shubh"    # bulbul:v3 voices: shubh, ritu, priya, aditya, ...
PACE = 1.1                  # a little faster than default: replies are short status lines
TIMEOUT_S = 15              # every external call has a timeout (CLAUDE.md rule 7)
MAX_CHARS = 500             # replies are one line; caps what one call can cost (API allows 2,500)

# Languages Bulbul speaks. STT can detect more (Assamese, Urdu, ...); those fall back to DEFAULT_LANGUAGE.
LANGUAGES = {"bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN"}
DEFAULT_LANGUAGE = "hi-IN"   # most users speak Hinglish; Bulbul reads code-mixed Roman text with a Hindi voice


# How text in each language should be written so the screen and Bulbul agree. Hindi stays Hinglish in Latin
# letters (how the whole app writes it); the other languages use their own script, which Bulbul reads best.
WRITE_AS = {
    "hi-IN": "Hinglish (Hindi in Latin letters)", "en-IN": "English",
    "bn-IN": "Bengali, in Bengali script", "gu-IN": "Gujarati, in Gujarati script",
    "kn-IN": "Kannada, in Kannada script", "ml-IN": "Malayalam, in Malayalam script",
    "mr-IN": "Marathi, in Devanagari script", "od-IN": "Odia, in Odia script",
    "pa-IN": "Punjabi, in Gurmukhi script", "ta-IN": "Tamil, in Tamil script", "te-IN": "Telugu, in Telugu script",
}


def write_as(language: str | None) -> str:
    """The language a spoken line must be written in, for the LLM prompts ("Tamil, in Tamil script")."""
    return WRITE_AS[language_for(language)]


class TTSError(Exception):
    pass


def language_for(detected: str | None) -> str:
    """STT's detected language -> a language Bulbul can speak."""
    return detected if detected in LANGUAGES else DEFAULT_LANGUAGE


async def speak(text: str, language: str) -> bytes:
    text = " ".join(text.split())[:MAX_CHARS]
    if not text:
        raise TTSError("nothing to say")
    body = {"text": text, "language_code": language_for(language), "model": MODEL, "speaker": SPEAKER,
            "pace": PACE}
    with span("tts", "sarvam", input={"chars": len(text), "language": body["language_code"], "model": MODEL,
                                      "speaker": SPEAKER}) as sp:
        key = os.environ.get("SARVAM_API_KEY")
        if not key:
            raise TTSError("SARVAM_API_KEY is not set")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.post(URL, headers={"api-subscription-key": key}, json=body)
        except httpx.HTTPError as e:
            raise TTSError(f"network: {e!r}") from e
        if r.status_code != 200:
            sp["output"] = {"status": r.status_code, "body": r.text[:300]}
            raise TTSError(f"status {r.status_code}")
        audios = r.json().get("audios") or []
        wav = base64.b64decode("".join(audios)) if audios else b""
        sp["output"] = {"bytes": len(wav)}
        if not wav:
            raise TTSError("empty audio")
        return wav
