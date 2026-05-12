import io
import logging
import asyncio
import os
import re as _re
import subprocess
import tempfile
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
        log.info("[STT_CORRECTION] '%s' → '%s'", original[:80], text[:80])
    return text


async def voice_to_text(audio_bytes) -> str | None:
    """Transcribe voice bytes via Groq Whisper. Returns None gracefully on any failure."""
    if not GROQ_API_KEY:
        log.warning("[VOICE] GROQ_API_KEY not set — STT disabled")
        return None

    def _transcribe():
        try:
            url = "https://api.groq.com/openai/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            files = {"file": ("audio.ogg", io.BytesIO(audio_bytes), "audio/ogg")}
            data  = {"model": "whisper-large-v3", "language": "fa"}
            r = requests.post(url, headers=headers, files=files, data=data, timeout=20)
            if r.status_code == 401:
                log.error("[VOICE] Groq API key invalid — STT disabled")
                return None
            if r.status_code != 200:
                log.error("[VOICE] Groq STT error %s: %s", r.status_code, r.text[:200])
                return None
            return r.json().get("text")
        except Exception as e:
            log.error("[VOICE] Groq STT exception: %s", e)
            return None

    raw = await asyncio.to_thread(_transcribe)
    return _correct_stt(raw)


# ── ConvAI audio → Telegram voice note ───────────────────────────────────────

def _audio_to_ogg(audio_bytes: bytes) -> str:
    """
    Convert raw audio bytes from ElevenLabs ConvAI (MP3 stream) to OGG/OPUS
    temp file required by Telegram voice notes. Returns file path.
    This is format conversion only — NOT text-to-speech synthesis.
    """
    in_fd,  in_path  = tempfile.mkstemp(suffix=".mp3")
    ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg")
    try:
        with os.fdopen(in_fd, "wb") as f:
            f.write(audio_bytes)
        os.close(ogg_fd)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", in_path,
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
            os.unlink(in_path)
        except OSError:
            pass
    return ogg_path


async def send_convai_audio(client, uid, audio_bytes: bytes) -> bool:
    """
    Convert ElevenLabs ConvAI audio bytes to OGG and send as Telegram voice note.
    Retries upload up to _MAX_UPLOAD_RETRIES times. Returns True on success.
    """
    if not audio_bytes:
        log.error("[VOICE] send_convai_audio: no audio bytes for uid=%s", uid)
        return False

    log.info("[VOICE] Converting ConvAI audio for uid=%s (%d bytes)", uid, len(audio_bytes))

    ogg_path = None
    try:
        ogg_path = await asyncio.to_thread(_audio_to_ogg, audio_bytes)

        if not os.path.exists(ogg_path) or os.path.getsize(ogg_path) == 0:
            log.error("[VOICE] OGG conversion produced empty file for uid=%s", uid)
            return False

        log.debug("[VOICE] OGG ready: %s (%d bytes)", ogg_path, os.path.getsize(ogg_path))

        last_exc = None
        for attempt in range(1, _MAX_UPLOAD_RETRIES + 1):
            try:
                await client.send_file(uid, ogg_path, voice_note=True)
                log.info("[VOICE] ConvAI audio sent to uid=%s (attempt %d)", uid, attempt)
                return True
            except Exception as e:
                last_exc = e
                log.warning("[VOICE] Upload attempt %d/%d failed uid=%s: %s",
                            attempt, _MAX_UPLOAD_RETRIES, uid, e)
                if attempt < _MAX_UPLOAD_RETRIES:
                    await asyncio.sleep(1.5 * attempt)

        log.error("[VOICE] All %d upload attempts failed uid=%s: %s",
                  _MAX_UPLOAD_RETRIES, uid, last_exc)
        return False

    except Exception as e:
        log.error("[VOICE] send_convai_audio unexpected error uid=%s: %s", uid, e)
        return False
    finally:
        if ogg_path:
            try:
                os.unlink(ogg_path)
            except OSError:
                pass
