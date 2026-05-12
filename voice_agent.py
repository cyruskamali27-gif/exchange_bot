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
_MIN_AUDIO_BYTES = 5_000        # below this → treat as empty
_MIN_AUDIO_DURATION = 0.5       # seconds

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


def _pcm_to_ogg(pcm_bytes: bytes) -> tuple[str, float]:
    """
    Convert raw PCM 16kHz 16-bit mono bytes to OGG/OPUS via ffmpeg.
    Returns (ogg_path, duration_seconds). Caller must delete ogg_path when done.
    Raises subprocess.CalledProcessError on ffmpeg failure.
    """
    pcm_fd, pcm_path = tempfile.mkstemp(suffix=".pcm")
    ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg")
    try:
        with os.fdopen(pcm_fd, "wb") as f:
            f.write(pcm_bytes)
        os.close(ogg_fd)

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "s16le", "-ar", "16000", "-ac", "1",
                "-i", pcm_path,
                "-c:a", "libopus", "-b:a", "32k",
                ogg_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # probe duration
        duration = 0.0
        try:
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    ogg_path,
                ],
                capture_output=True, text=True, timeout=10,
            )
            duration = float(probe.stdout.strip() or 0)
        except Exception as e:
            log.warning("[VOICE] ffprobe failed: %s", e)

    finally:
        try:
            os.unlink(pcm_path)
        except OSError:
            pass

    return ogg_path, duration


async def send_convai_audio(client, uid: int, audio_bytes: bytes) -> bool:
    """
    Convert ElevenLabs ConvAI PCM audio to OGG/OPUS and send as Telegram voice note.
    audio_bytes is raw PCM 16kHz 16-bit mono from the ConvAI WebSocket.
    Falls back to a text message if audio is missing or invalid.
    """
    log.info("[VOICE] uid=%s raw PCM bytes received: %d", uid, len(audio_bytes) if audio_bytes else 0)

    if not audio_bytes or len(audio_bytes) < _MIN_AUDIO_BYTES:
        log.error("[VOICE] uid=%s audio too small (%d bytes) — sending text fallback",
                  uid, len(audio_bytes) if audio_bytes else 0)
        await client.send_message(uid, "Voice audio was not received from ElevenLabs agent.")
        return False

    ogg_path = None
    try:
        log.info("[VOICE] uid=%s converting PCM→OGG (s16le 16kHz mono → libopus)", uid)
        ogg_path, duration = await asyncio.to_thread(_pcm_to_ogg, audio_bytes)
        ogg_size = os.path.getsize(ogg_path)

        log.info("[VOICE] uid=%s OGG ready — size=%d bytes duration=%.2fs",
                 uid, ogg_size, duration)

        if ogg_size < _MIN_AUDIO_BYTES or duration < _MIN_AUDIO_DURATION:
            log.error("[VOICE] uid=%s OGG too small/short (size=%d dur=%.2fs) — text fallback",
                      uid, ogg_size, duration)
            await client.send_message(uid, "Voice audio was not received from ElevenLabs agent.")
            return False

        for attempt in range(1, _MAX_UPLOAD_RETRIES + 1):
            try:
                await client.send_file(uid, ogg_path, voice_note=True)
                log.info("[VOICE] uid=%s sent voice note (attempt %d) dur=%.2fs",
                         uid, attempt, duration)
                return True
            except Exception as e:
                log.warning("[VOICE] Upload attempt %d/%d failed uid=%s: %s",
                            attempt, _MAX_UPLOAD_RETRIES, uid, e)
                if attempt < _MAX_UPLOAD_RETRIES:
                    await asyncio.sleep(1.5 * attempt)

        log.error("[VOICE] All upload attempts failed uid=%s", uid)
        await client.send_message(uid, "Voice audio was not received from ElevenLabs agent.")
        return False

    except subprocess.CalledProcessError as e:
        log.error("[VOICE] uid=%s ffmpeg conversion failed: %s", uid, e.stderr[-300:] if e.stderr else e)
        await client.send_message(uid, "Voice audio was not received from ElevenLabs agent.")
        return False
    except Exception as e:
        log.error("[VOICE] uid=%s unexpected error: %s", uid, e)
        await client.send_message(uid, "Voice audio was not received from ElevenLabs agent.")
        return False
    finally:
        if ogg_path:
            try:
                os.unlink(ogg_path)
            except OSError:
                pass
