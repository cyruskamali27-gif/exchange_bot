"""
ElevenLabs Conversational AI — DISABLED.
All text generation is handled by Gemini.
"""

import logging

log = logging.getLogger("elevenlabs_agent")
log.info("[ELEVEN_AGENT] ElevenLabs agent disabled — Gemini handles all text replies")


async def chat(uid: int, text: str) -> str:
    """ElevenLabs agent disabled. Raises so exchange_brain falls back to Gemini."""
    raise RuntimeError("ElevenLabs agent disabled — using Gemini")
