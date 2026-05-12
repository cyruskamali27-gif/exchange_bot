import io
import logging
import asyncio
import os
import re as _re
import subprocess
import tempfile
import requests

from config import GROQ_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID

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


# ── ElevenLabs TTS ────────────────────────────────────────────────────────────

def _synthesize_mp3(text: str) -> bytes | None:
    """Call ElevenLabs TTS and return raw MP3 bytes, or None on failure."""
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        log.error("[TTS] ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not configured")
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.50,
            "similarity_boost": 0.85,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code != 200:
            log.error("[TTS] ElevenLabs error %s: %s", r.status_code, r.text[:200])
            return None
        log.debug("[TTS] synthesized %d bytes", len(r.content))
        return r.content
    except Exception as e:
        log.error("[TTS] ElevenLabs exception: %s", e)
        return None


def _mp3_to_ogg(mp3_bytes: bytes) -> str:
    """Convert MP3 bytes to an OGG/OPUS temp file. Returns the .ogg file path."""
    mp3_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg")
    try:
        with os.fdopen(mp3_fd, "wb") as f:
            f.write(mp3_bytes)
        os.close(ogg_fd)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", mp3_path,
                "-c:a", "libopus",
                "-b:a", "64k",
                "-vbr", "on",
                ogg_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        try:
            os.unlink(mp3_path)
        except OSError:
            pass
    return ogg_path


async def send_voice_message(client, uid, text):
    """
    Synthesize text via ElevenLabs TTS and send as Telegram voice note.
    Retries upload up to _MAX_UPLOAD_RETRIES times. Returns True on success.
    """
    log.info("[TTS] Generating voice for uid=%s (%d chars)", uid, len(text))

    audio = await asyncio.to_thread(_synthesize_mp3, text)
    if not audio:
        log.error("[TTS] Synthesis failed for uid=%s", uid)
        return False

    ogg_path = None
    try:
        ogg_path = await asyncio.to_thread(_mp3_to_ogg, audio)

        if not os.path.exists(ogg_path) or os.path.getsize(ogg_path) == 0:
            log.error("[TTS] OGG conversion failed for uid=%s", uid)
            return False

        log.debug("[TTS] OGG ready: %s (%d bytes)", ogg_path, os.path.getsize(ogg_path))

        last_exc = None
        for attempt in range(1, _MAX_UPLOAD_RETRIES + 1):
            try:
                await client.send_file(uid, ogg_path, voice_note=True)
                log.info("[TTS] Voice sent to uid=%s (attempt %d)", uid, attempt)
                return True
            except Exception as e:
                last_exc = e
                log.warning("[TTS] Upload attempt %d/%d failed uid=%s: %s",
                            attempt, _MAX_UPLOAD_RETRIES, uid, e)
                if attempt < _MAX_UPLOAD_RETRIES:
                    await asyncio.sleep(1.5 * attempt)

        log.error("[TTS] All %d upload attempts failed uid=%s: %s",
                  _MAX_UPLOAD_RETRIES, uid, last_exc)
        return False

    except Exception as e:
        log.error("[TTS] Unexpected error uid=%s: %s", uid, e)
        return False
    finally:
        if ogg_path:
            try:
                os.unlink(ogg_path)
            except OSError:
                pass
