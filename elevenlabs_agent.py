"""
ElevenLabs Conversational AI — audio mode.

Voice flow:
  user text (from STT) → ConvAI WebSocket → collect audio chunks + reply text
  Caller converts PCM audio bytes to OGG and sends as Telegram voice note.

No greeting injection. No prompt overrides. No per-user state.
The ElevenLabs agent manages its own personality and greeting behavior.
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


def _pong(msg: dict) -> str:
    eid = msg.get("ping_event", {}).get("event_id", 0)
    return json.dumps({"type": "pong", "event_id": eid})


def _extract_text(msg: dict) -> str:
    ar = msg.get("agent_response_event") or {}
    raw = ar.get("agent_response") or ""
    return _re.sub(r"\s*\[[^\]]*\]\s*", " ", raw).strip()


async def chat_with_audio(uid: int, text: str) -> tuple[str, bytes]:
    """
    Send *text* to ElevenLabs ConvAI agent.
    Returns (reply_text, audio_bytes) — audio is raw PCM 16kHz.
    Text is sent as-is — no greeting injection, no prompt prepending.
    """
    if not ELEVENLABS_API_KEY or not ELEVENLABS_AGENT_ID:
        raise RuntimeError("ElevenLabs credentials not configured")

    log.info("[ELEVEN_WS] uid=%s connecting", uid)
    log.info("[ELEVEN_WS] uid=%s no greeting injected by Telegram code", uid)

    uri = f"{_WS_BASE}?agent_id={ELEVENLABS_AGENT_ID}"
    import websockets
    try:
        async with websockets.connect(
            uri,
            additional_headers={"xi-api-key": ELEVENLABS_API_KEY},
        ) as ws:
            reply_text, audio_bytes = await _run_conversation(ws, uid, text)
    except Exception as e:
        log.error("[ELEVEN_WS] uid=%s websocket error: %s", uid, e)
        raise

    if not reply_text and not audio_bytes:
        raise ValueError("No response from ElevenLabs ConvAI")

    log.info("[ELEVEN_WS] uid=%s done — text=%d chars audio=%d bytes",
             uid, len(reply_text), len(audio_bytes))
    return reply_text, audio_bytes


async def _run_conversation(ws, uid: int, user_text: str) -> tuple[str, bytes]:
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

    # ── Phase 2: send initiation (no overrides) ──────────────────────
    await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))
    log.info("[ELEVEN_P2] uid=%s sent initiation_client_data", uid)

    # ── Phase 2.5: short drain in case agent sends an opening message ─
    _deadline = loop.time() + 1.0
    while True:
        _rem = _deadline - loop.time()
        if _rem <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=_rem)
        except (asyncio.TimeoutError, TimeoutError):
            break
        pre = json.loads(raw)
        ptype = pre.get("type", "")
        if ptype == "ping":
            await ws.send(_pong(pre))
        elif ptype == "agent_response":
            log.info("[ELEVEN_P25] uid=%s drained unexpected opening message", uid)
            break
        elif ptype == "conversation_ended":
            log.warning("[ELEVEN_P25] uid=%s conversation_ended early", uid)
            raise ValueError("Conversation ended before user message")

    # ── Phase 3: send user message exactly as transcribed ────────────
    await ws.send(json.dumps({"type": "user_message", "text": user_text}))
    log.info("[ELEVEN_P3] uid=%s sent user_message: %r", uid, user_text[:80])

    # ── Phase 4: collect audio chunks + agent_response ──────────────
    reply_text   = ""
    audio_chunks: list[bytes] = []
    got_response = False
    end_deadline: float | None = None

    log.info("[ELEVEN_P4] uid=%s collecting response", uid)
    while True:
        if end_deadline is not None:
            remaining = end_deadline - loop.time()
            if remaining <= 0:
                log.info("[ELEVEN_P4] uid=%s audio drain complete", uid)
                break
            timeout = min(remaining, _TIMEOUT)
        else:
            timeout = _TIMEOUT

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("[ELEVEN_P4] uid=%s recv timeout — stopping collection", uid)
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
            log.info("[ELEVEN_P4] uid=%s conversation_ended by server", uid)
            break

        elif mtype in ("user_transcript", "agent_response_correction"):
            log.debug("[ELEVEN_P4] uid=%s skipped type=%s", uid, mtype)

        else:
            log.debug("[ELEVEN_P4] uid=%s ignored type=%s", uid, mtype)

    audio_bytes = b"".join(audio_chunks)
    if not audio_chunks:
        log.warning("[ELEVEN_P4] uid=%s no audio chunks — ConvAI returned no audio", uid)
    log.info("[ELEVEN_P4] uid=%s finished — got_response=%s chunks=%d bytes=%d",
             uid, got_response, len(audio_chunks), len(audio_bytes))
    return reply_text, audio_bytes
