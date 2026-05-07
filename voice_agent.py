import io
import asyncio
import requests
from config import GROQ_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID
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


async def send_voice_message(client, uid, text):
    optimized = voice_optimize(text)

    def _synthesize():
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "text": optimized,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.75,
                "style": 0.3,
                "use_speaker_boost": True,
            },
        }
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            print("TTS ERROR:", r.status_code, r.text)
            return None
        return r.content

    audio = await asyncio.to_thread(_synthesize)
    if not audio:
        return False

    try:
        await client.send_file(uid, io.BytesIO(audio), voice_note=True)
        return True
    except Exception as e:
        print("SEND VOICE ERROR:", e)
        return False
