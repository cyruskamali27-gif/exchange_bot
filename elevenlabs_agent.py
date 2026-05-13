"""
ElevenLabs Conversational AI — audio mode.

Voice flow:
  user text (from STT) → ConvAI WebSocket → collect audio chunks + reply text
  Caller converts audio bytes to OGG and sends as Telegram voice note.

Per-session conversation_config_override controls greeting/no-greeting behavior.
Banned-phrase detection triggers one regeneration with a stricter prompt.
"""

import asyncio
import base64
import json
import logging
import re as _re

from config import ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID

log = logging.getLogger("elevenlabs_agent")
log.info("[ELEVEN_AGENT] ConvAI agent ready: %s", ELEVENLABS_AGENT_ID)

_WS_BASE       = "wss://api.elevenlabs.io/v1/convai/conversation"
_TIMEOUT       = 30
_AUDIO_DRAIN_S = 3.0

# ── Per-session system prompts ────────────────────────────────────────────────

_PROMPT_FIRST = (
    "You are Sahar, voice receptionist of Cyrus Global Exchange, Toronto, Canada. "
    "Language: Persian (Farsi), modern Tehran dialect. "
    "This is the FIRST message — open with exactly: "
    "«سلام، سحر هستم از صرافی سیروس، بفرمایید در خدمتم.» "
    "Then answer the customer question briefly. "
    "Keep responses 1-2 short sentences. Calm and professional. "
    "No emojis. No مشتری گرامی. No کاربر محترم. "
    "For rate/price questions: say «برای نرخ، لطفاً پیام متنی بفرستید.»"
)

_PROMPT_SUBSEQUENT = (
    "You are Sahar, voice receptionist of Cyrus Global Exchange, Toronto, Canada. "
    "Language: Persian (Farsi), modern Tehran dialect. "
    "STRICT RULE: Do NOT say سلام. Do NOT greet. Do NOT introduce yourself. "
    "Do NOT say چطور می‌تونم کمک کنم or چجوری می‌تونم کمک کنم or any helper phrase. "
    "Start your response immediately with the direct answer. "
    "Keep responses 1-2 short sentences. Calm and professional. "
    "No emojis. No مشتری گرامی. No کاربر محترم. "
    "For rate/price questions: say «برای نرخ، لطفاً پیام متنی بفرستید.»"
)

_PROMPT_REGEN = (
    "You are Sahar, voice receptionist of Cyrus Global Exchange, Toronto, Canada. "
    "Language: Persian (Farsi), modern Tehran dialect. "
    "CRITICAL: Your very first word must be part of the answer — not a greeting, "
    "not an introduction, not a helper phrase. "
    "No سلام. No چطور. No کمک کنم. "
    "1-2 sentences maximum. Direct answer only."
)

# Prepended to user_text for non-first messages as extra LLM instruction
_NO_GREET_PREPEND = (
    "بدون سلام، بدون معرفی، بدون عبارت چطور می‌تونم کمک کنم. "
    "فقط مستقیم جواب بده.\n"
)

# ── Banned phrases — trigger one regeneration ────────────────────────────────

_BANNED = [
    "چجوری می‌تونم کمک کنم",
    "چطور می‌تونم کمک کنم",
    "چگونه می‌تونم کمک کنم",
    "how can i help you",
    "how may i assist you",
]


def _has_banned(text: str) -> bool:
    t = text.lower()
    return any(p.lower() in t for p in _BANNED)


def _pong(msg: dict) -> str:
    eid = msg.get("ping_event", {}).get("event_id", 0)
    return json.dumps({"type": "pong", "event_id": eid})


def _extract_text(msg: dict) -> str:
    ar = msg.get("agent_response_event") or {}
    raw = ar.get("agent_response") or ""
    return _re.sub(r"\s*\[[^\]]*\]\s*", " ", raw).strip()


async def chat_with_audio(uid: int, text: str, is_first_message: bool = True) -> tuple[str, bytes]:
    """
    Send *text* to ElevenLabs ConvAI agent.
    Returns (reply_text, audio_bytes) — audio is raw PCM 16kHz.
    is_first_message=True → greeting included; False → no greeting, direct answer.
    Regenerates once if a banned phrase is detected in the reply text.
    """
    if not ELEVENLABS_API_KEY or not ELEVENLABS_AGENT_ID:
        raise RuntimeError("ElevenLabs credentials not configured")

    if is_first_message:
        prompt    = _PROMPT_FIRST
        first_msg = "سلام، سحر هستم از صرافی سیروس، بفرمایید در خدمتم."
        user_text = text
        drain_s   = 5.0
    else:
        prompt    = _PROMPT_SUBSEQUENT
        first_msg = ""
        user_text = _NO_GREET_PREPEND + text
        drain_s   = 1.0

    log.info("[ELEVEN_WS] uid=%s connecting first=%s", uid, is_first_message)
    reply_text, audio_bytes = await _ws_call(uid, user_text, prompt, first_msg, drain_s)

    if _has_banned(reply_text):
        log.warning("[ELEVEN_REGEN] uid=%s banned phrase in reply=%r — regenerating",
                    uid, reply_text[:80])
        reply_text, audio_bytes = await _ws_call(
            uid, _NO_GREET_PREPEND + text, _PROMPT_REGEN, "", 1.0
        )
        if _has_banned(reply_text):
            log.warning("[ELEVEN_REGEN] uid=%s banned phrase still present after regen: %r",
                        uid, reply_text[:80])

    if not reply_text and not audio_bytes:
        raise ValueError("No response from ElevenLabs ConvAI")

    log.info("[ELEVEN_WS] uid=%s done — text=%d chars audio=%d bytes",
             uid, len(reply_text), len(audio_bytes))
    return reply_text, audio_bytes


async def _ws_call(uid: int, user_text: str, prompt: str,
                   first_message: str, drain_s: float) -> tuple[str, bytes]:
    uri = f"{_WS_BASE}?agent_id={ELEVENLABS_AGENT_ID}"
    import websockets
    async with websockets.connect(
        uri,
        additional_headers={"xi-api-key": ELEVENLABS_API_KEY},
    ) as ws:
        return await _run_conversation(ws, uid, user_text, prompt, first_message, drain_s)


async def _run_conversation(ws, uid: int, user_text: str,
                            prompt: str, first_message: str,
                            drain_s: float) -> tuple[str, bytes]:
    loop = asyncio.get_running_loop()

    async def _recv(timeout=_TIMEOUT):
        return await asyncio.wait_for(ws.recv(), timeout=timeout)

    # ── Phase 1: wait for conversation_initiation_metadata ──────────
    log.info("[ELEVEN_P1] uid=%s waiting for metadata", uid)
    while True:
        msg = json.loads(await _recv())
        mtype = msg.get("type", "")
        if mtype == "ping":
            await ws.send(_pong(msg))
        elif mtype == "conversation_initiation_metadata":
            log.info("[ELEVEN_P1] uid=%s metadata received", uid)
            break

    # ── Phase 2: send initiation with per-session prompt override ────
    init_data = {
        "type": "conversation_initiation_client_data",
        "conversation_config_override": {
            "agent": {
                "prompt": {"prompt": prompt},
                "first_message": first_message,
            }
        }
    }
    await ws.send(json.dumps(init_data))
    log.info("[ELEVEN_P2] uid=%s sent initiation has_greeting=%s", uid, bool(first_message))

    # ── Phase 2.5: drain agent opening greeting ──────────────────────
    _deadline = loop.time() + drain_s
    while True:
        _rem = _deadline - loop.time()
        if _rem <= 0:
            log.info("[ELEVEN_P25] uid=%s deadline — no greeting", uid)
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=_rem)
        except (asyncio.TimeoutError, TimeoutError):
            log.info("[ELEVEN_P25] uid=%s timeout — no greeting", uid)
            break
        pre = json.loads(raw)
        ptype = pre.get("type", "")
        if ptype == "ping":
            await ws.send(_pong(pre))
        elif ptype == "agent_response":
            log.info("[ELEVEN_P25] uid=%s greeting drained", uid)
            break
        elif ptype == "conversation_ended":
            raise ValueError("Conversation ended before user message could be sent")

    # ── Phase 3: send user message ───────────────────────────────────
    await ws.send(json.dumps({"type": "user_message", "text": user_text}))
    log.info("[ELEVEN_P3] uid=%s sent user_message: %r", uid, user_text[:80])

    # ── Phase 4: collect audio chunks + agent_response ──────────────
    reply_text   = ""
    audio_chunks: list[bytes] = []
    got_response = False
    end_deadline: float | None = None

    log.info("[ELEVEN_P4] uid=%s collecting response (timeout=%ds)", uid, _TIMEOUT)
    while True:
        if end_deadline is not None:
            remaining = end_deadline - loop.time()
            if remaining <= 0:
                break
            timeout = min(remaining, _TIMEOUT)
        else:
            timeout = _TIMEOUT

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            break

        msg   = json.loads(raw)
        mtype = msg.get("type", "")

        if mtype == "ping":
            await ws.send(_pong(msg))

        elif mtype == "audio":
            chunk_b64 = msg.get("audio_event", {}).get("audio_base_64", "")
            if chunk_b64:
                audio_chunks.append(base64.b64decode(chunk_b64))
                log.debug("[ELEVEN_P4] uid=%s audio chunk %d bytes",
                          uid, len(audio_chunks[-1]))

        elif mtype == "agent_response":
            reply_text   = _extract_text(msg)
            got_response = True
            log.info("[ELEVEN_P4] uid=%s agent_response: %r", uid, reply_text[:80])
            end_deadline = loop.time() + _AUDIO_DRAIN_S

        elif mtype == "conversation_ended":
            log.info("[ELEVEN_P4] uid=%s conversation_ended", uid)
            break

        elif mtype in ("user_transcript", "agent_response_correction"):
            log.debug("[ELEVEN_P4] uid=%s skipped type=%s", uid, mtype)

        else:
            log.debug("[ELEVEN_P4] uid=%s ignored type=%s", uid, mtype)

    audio_bytes = b"".join(audio_chunks)
    log.info("[ELEVEN_P4] uid=%s finished — got_response=%s audio_chunks=%d total_bytes=%d",
             uid, got_response, len(audio_chunks), len(audio_bytes))
    return reply_text, audio_bytes
