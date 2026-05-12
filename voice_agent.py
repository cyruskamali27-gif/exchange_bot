import io
import logging
import asyncio
import re as _re
import requests

from config import GROQ_API_KEY

log = logging.getLogger("voice_agent")

_STT_FIXES = [
    (r'\bنردی\b',       'نقدی'),
    (r'\bنغدی\b',       'نقدی'),
    (r'\bنقده\b',       'نقدی'),
    (r'\bحوله\b',       'حواله'),
    (r'\bهواله\b',      'حواله'),
    (r'\bاینتراک\b',    'اینترک'),
    (r'\bاینتراکت\b',   'اینترک'),
    (r'\bانتراک\b',     'اینترک'),
    (r'\bتدر\b',        'تتر'),
    (r'\bتتره\b',       'تتر'),
    (r'\bتدره\b',       'تتر'),
    (r'\bیو‌اس‌دی\b',   'یو اس دی'),
    (r'\bخریداری\b',    'خرید'),
    (r'\bفروختن\b',     'فروش'),
]


def _correct_stt(text: str) -> str:
    if not text:
        return text
    original = text
    for pattern, replacement in _STT_FIXES:
        text = _re.sub(pattern, replacement, text)
    if text != original:
        log.info("[STT_CORRECTION] '%s' → '%s'", original[:80], text[:80])
    return text


async def voice_to_text(audio_bytes):
    """Transcribe voice bytes via Groq Whisper. Returns None gracefully on any failure."""
    if not GROQ_API_KEY:
        log.warning("[VOICE_AGENT] GROQ_API_KEY not set — falling back to text-only mode")
        return None

    def _transcribe():
        try:
            url = "https://api.groq.com/openai/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            files = {"file": ("audio.ogg", io.BytesIO(audio_bytes), "audio/ogg")}
            data = {"model": "whisper-large-v3", "language": "fa"}
            r = requests.post(url, headers=headers, files=files, data=data, timeout=20)
            if r.status_code == 401:
                log.error("[VOICE_AGENT] Groq API key invalid — voice recognition disabled")
                return None
            if r.status_code != 200:
                log.error("[VOICE_AGENT] Groq STT error %s: %s", r.status_code, r.text[:200])
                return None
            return r.json().get("text")
        except Exception as e:
            log.error("[VOICE_AGENT] Groq STT exception: %s", e)
            return None

    raw = await asyncio.to_thread(_transcribe)
    return _correct_stt(raw)


async def send_voice_message(client, uid, text):
    """Voice output disabled — text-only mode."""
    log.info("[VOICE_AGENT] Voice output disabled (text-only mode), uid=%s", uid)
    return False
