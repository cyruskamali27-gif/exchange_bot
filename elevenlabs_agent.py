"""
ElevenLabs Conversational AI text bridge.

Sends user text → gets agent text response → caller re-synthesises audio
with our chosen VOICE_ID (BognUUMX6W1qmZKB2TOw) + office ambience.

Price requests are handled locally via the live-rate API — no WebSocket needed.
"""

import asyncio
import json
import logging
import aiohttp

from config import ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID

log = logging.getLogger("elevenlabs_agent")

# conversation_id per Telegram uid — resumes context across messages
_user_conversations: dict[int, str] = {}

_LIVE_RATE_URL = "https://watherdata-sync-service.com/api/live-rate"
_WS_BASE = "wss://api.elevenlabs.io/v1/convai/conversation"
_TIMEOUT = 30  # seconds to wait for agent_response


# ─── Live rate (price bypass) ─────────────────────────────────────────────────

async def get_live_rate_text(currency: str | None = None) -> str:
    """
    Call the live-rate API and return a short Persian formatted string.
    Falls back to a generic error message on failure.
    """
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(_LIVE_RATE_URL) as resp:
                if resp.status != 200:
                    return "متأسفم، در حال حاضر قیمت در دسترس نیست."
                data = await resp.json()

        _CUR_LABEL = {"USDT": "تتر (USDT)", "USD": "دلار آمریکا (USD)", "CAD": "دلار کانادا (CAD)"}
        targets = [currency.upper()] if currency else ["USDT", "USD", "CAD"]
        lines = []
        for cur in targets:
            entry = data.get(cur)
            if not entry:
                continue
            label = _CUR_LABEL.get(cur, cur)
            sell = entry.get("sell", 0)
            buy  = entry.get("buy",  0)
            if sell and buy:
                lines.append(
                    f"📌 {label}:\n"
                    f"   فروش ما: {sell:,} تومان\n"
                    f"   خرید ما: {buy:,} تومان"
                )
        if not lines:
            return "متأسفم، در حال حاضر قیمت در دسترس نیست."
        return "\n\n".join(lines)

    except Exception as e:
        log.error("get_live_rate_text error: %s", e)
        return "متأسفم، در حال حاضر قیمت در دسترس نیست."


# ─── ElevenLabs ConvAI WebSocket chat ────────────────────────────────────────

async def chat(uid: int, text: str) -> str:
    """
    Send *text* to the ElevenLabs Conversational AI agent and return the
    agent's text reply.  Maintains per-user conversation_id for context.
    Returns a fallback Persian string on error.
    """
    if not ELEVENLABS_API_KEY or not ELEVENLABS_AGENT_ID:
        log.error("ElevenLabs credentials not configured")
        return "سیستم در حال راه‌اندازی است. لطفاً بعداً تماس بگیرید."

    prev_conv_id = _user_conversations.get(uid)
    uri = f"{_WS_BASE}?agent_id={ELEVENLABS_AGENT_ID}"
    if prev_conv_id:
        uri += f"&conversation_id={prev_conv_id}"

    headers = {"xi-api-key": ELEVENLABS_API_KEY}

    try:
        import websockets
        async with websockets.connect(uri, additional_headers=headers) as ws:
            return await _run_conversation(ws, uid, text)
    except Exception as e:
        log.error("chat WebSocket error uid=%s: %s", uid, e)
        return "ببخشید، مشکلی پیش آمد. لطفاً دوباره بنویسید."


async def _run_conversation(ws, uid: int, user_text: str) -> str:
    """
    Drive a single-turn exchange on an already-open WebSocket.

    Protocol:
      1. Wait for conversation_initiation_metadata → save conversation_id
      2. Send user_transcript
      3. Wait for agent_response (handle ping/pong along the way)
      4. Return agent text; close connection implicitly via context manager
    """
    agent_reply = ""
    initiated = False

    async def _recv_with_timeout():
        return await asyncio.wait_for(ws.recv(), timeout=_TIMEOUT)

    # Phase 1 — wait for initiation, then send our text
    while not initiated:
        raw = await _recv_with_timeout()
        msg = json.loads(raw)
        mtype = msg.get("type", "")

        if mtype == "conversation_initiation_metadata":
            conv_id = (
                msg.get("conversation_initiation_metadata_event", {})
                   .get("conversation_id")
                or msg.get("conversation_id")
            )
            if conv_id:
                _user_conversations[uid] = conv_id
                log.debug("uid=%s conversation_id=%s", uid, conv_id)
            initiated = True
            # Send user text immediately after init
            await ws.send(json.dumps({
                "type": "user_transcript",
                "user_transcript": user_text,
            }))
            log.debug("uid=%s sent user_transcript: %s", uid, user_text[:60])

        elif mtype == "ping":
            event_id = msg.get("ping_event", {}).get("event_id", 0)
            await ws.send(json.dumps({"type": "pong", "pong_event": {"event_id": event_id}}))

    # Phase 2 — wait for agent_response
    while True:
        raw = await _recv_with_timeout()
        msg = json.loads(raw)
        mtype = msg.get("type", "")

        if mtype == "ping":
            event_id = msg.get("ping_event", {}).get("event_id", 0)
            await ws.send(json.dumps({"type": "pong", "pong_event": {"event_id": event_id}}))

        elif mtype == "agent_response":
            # text lives at different paths depending on SDK version
            ar = msg.get("agent_response_event") or msg.get("agent_response") or {}
            agent_reply = (
                ar.get("agent_response")
                or ar.get("text")
                or msg.get("text", "")
            )
            log.debug("uid=%s agent_response: %s", uid, agent_reply[:80])
            break  # got what we need

        elif mtype in ("audio", "interruption", "turn_start", "turn_end", "vad_score"):
            # ignore audio/control events — we re-synthesise ourselves
            pass

        elif mtype == "conversation_ended":
            log.info("uid=%s conversation ended by agent", uid)
            _user_conversations.pop(uid, None)
            break

    return agent_reply or "ببخشید، پاسخی دریافت نشد."
