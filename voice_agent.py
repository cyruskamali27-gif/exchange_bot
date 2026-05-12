import io
import logging
import asyncio
import re as _re
import requests

from config import GROQ_API_KEY

log = logging.getLogger("voice_agent")

_MAX_UPLOAD_RETRIES = 3

# ── Persian STT correction ────────────────────────────────────────────────────

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
        log.info("[STT] correction: '%s' → '%s'", original[:80], text[:80])
    return text


async def voice_to_text(audio_bytes: bytes) -> str | None:
    """Transcribe Telegram voice bytes via Groq Whisper (STT). Returns None on failure."""
    if not GROQ_API_KEY:
        log.warning("[STT] GROQ_API_KEY not set")
        return None

    def _transcribe():
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("audio.ogg", io.BytesIO(audio_bytes), "audio/ogg")},
                data={"model": "whisper-large-v3", "language": "fa"},
                timeout=20,
            )
            if r.status_code == 401:
                log.error("[STT] Groq API key invalid")
                return None
            if r.status_code != 200:
                log.error("[STT] Groq error %s: %s", r.status_code, r.text[:200])
                return None
            return r.json().get("text")
        except Exception as e:
            log.error("[STT] exception: %s", e)
            return None

    raw = await asyncio.to_thread(_transcribe)
    return _correct_stt(raw)


async def send_convai_audio(client, uid: int, audio_bytes: bytes) -> bool:
    """
    Send ElevenLabs ConvAI audio response directly to Telegram as a voice note.
    audio_bytes is the raw audio stream collected from the ConvAI WebSocket.
    No format conversion — bytes are uploaded as-is.
    """
    if not audio_bytes:
        log.error("[VOICE] No audio bytes to send for uid=%s", uid)
        return False

    log.info("[VOICE] Sending ConvAI audio to uid=%s (%d bytes)", uid, len(audio_bytes))

    for attempt in range(1, _MAX_UPLOAD_RETRIES + 1):
        try:
            buf = io.BytesIO(audio_bytes)
            buf.name = "response.ogg"
            await client.send_file(uid, buf, voice_note=True)
            log.info("[VOICE] Sent to uid=%s (attempt %d)", uid, attempt)
            return True
        except Exception as e:
            log.warning("[VOICE] Upload attempt %d/%d failed uid=%s: %s",
                        attempt, _MAX_UPLOAD_RETRIES, uid, e)
            if attempt < _MAX_UPLOAD_RETRIES:
                await asyncio.sleep(1.5 * attempt)

    log.error("[VOICE] All upload attempts failed uid=%s", uid)
    return False
