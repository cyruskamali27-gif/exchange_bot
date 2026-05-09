import io
import os
import asyncio
import subprocess
import tempfile
import uuid
import requests
from config import (
    GROQ_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID,
    ENABLE_OFFICE_AMBIENCE, OFFICE_AMBIENCE_FILE, OFFICE_AMBIENCE_VOLUME,
)
from natural_replies import voice_optimize


async def voice_to_text(audio_bytes):
    def _transcribe():
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        files = {"file": ("audio.ogg", io.BytesIO(audio_bytes), "audio/ogg")}
        data = {"model": "whisper-large-v3", "language": "fa"}
        r = requests.post(url, headers=headers, files=files, data=data)
        if r.status_code != 200:
            print("STT ERROR:", r.status_code, r.text)
            return None
        return r.json().get("text")

    return await asyncio.to_thread(_transcribe)


def _synthesize_mp3(text: str) -> bytes | None:
    """Call ElevenLabs TTS and return raw MP3 bytes, or None on failure."""
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
    r = requests.post(url, headers=headers, json=payload)
    if r.status_code != 200:
        print("TTS ERROR:", r.status_code, r.text)
        return None
    return r.content


def mix_office_ambience(voice_mp3_bytes: bytes) -> bytes:
    """
    Mix a very low-volume office ambience track into the voice MP3.
    The ambience file is looped to match voice duration.
    Returns original bytes unchanged if ambience is disabled or file missing.
    """
    if not ENABLE_OFFICE_AMBIENCE:
        return voice_mp3_bytes
    if not os.path.exists(OFFICE_AMBIENCE_FILE):
        print(f"AMBIENCE: file not found at {OFFICE_AMBIENCE_FILE} — skipping mix")
        return voice_mp3_bytes

    in_fd,  in_path  = tempfile.mkstemp(suffix=".mp3")
    out_fd, out_path = tempfile.mkstemp(suffix=".mp3")
    try:
        with os.fdopen(in_fd, "wb") as f:
            f.write(voice_mp3_bytes)
        os.close(out_fd)

        vol = OFFICE_AMBIENCE_VOLUME
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", in_path,
                "-stream_loop", "-1", "-i", OFFICE_AMBIENCE_FILE,
                "-filter_complex",
                f"[1:a]volume={vol}[bg];[0:a][bg]amix=inputs=2:duration=first",
                "-q:a", "4",
                out_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(out_path, "rb") as f:
            return f.read()

    except Exception as e:
        print(f"AMBIENCE MIX ERROR: {e} — sending clean voice")
        return voice_mp3_bytes
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


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


def save_mp3_for_hosting(mp3_bytes: bytes, host_dir: str = "/var/www/html/tts_cache") -> str:
    """
    Save MP3 bytes to a web-accessible directory and return the filename.
    Used by Twilio <Play> calls.
    """
    os.makedirs(host_dir, exist_ok=True)
    filename = f"voice_{uuid.uuid4().hex[:12]}.mp3"
    path = os.path.join(host_dir, filename)
    with open(path, "wb") as f:
        f.write(mp3_bytes)
    return filename


async def send_voice_message(client, uid, text):
    """Generate ElevenLabs TTS, mix in office ambience, send as Telegram voice note."""
    optimized = voice_optimize(text)
    audio = await asyncio.to_thread(_synthesize_mp3, optimized)
    if not audio:
        return False

    # Mix office ambience at low volume
    audio = await asyncio.to_thread(mix_office_ambience, audio)

    ogg_path = None
    try:
        ogg_path = await asyncio.to_thread(_mp3_to_ogg, audio)
        await client.send_file(uid, ogg_path, voice_note=True)
        return True
    except Exception as e:
        print("SEND VOICE ERROR:", e)
        return False
    finally:
        if ogg_path:
            try:
                os.unlink(ogg_path)
            except OSError:
                pass


async def generate_call_audio_url(text: str, base_url: str = "https://watherdata-sync-service.com") -> str | None:
    """
    Generate ElevenLabs audio + office ambience, host it on nginx,
    return the public URL for Twilio <Play>. Returns None on failure.
    """
    optimized = voice_optimize(text)
    audio = await asyncio.to_thread(_synthesize_mp3, optimized)
    if not audio:
        return None

    audio = await asyncio.to_thread(mix_office_ambience, audio)

    filename = await asyncio.to_thread(save_mp3_for_hosting, audio)
    return f"{base_url}/tts_cache/{filename}"
