"""
call_agent.py — Twilio outbound calls — DISABLED.
All voice/phone call functionality has been removed.
"""

import logging

log = logging.getLogger("call_agent")
log.info("[CALL_AGENT] Disabled — Twilio/voice call system removed")


async def call_customer(phone_number: str, message_text: str) -> dict:
    log.warning("[CALL_AGENT] call_customer called but disabled")
    return {"success": False, "error": "call_agent_disabled"}


def get_call_status(call_sid: str) -> dict:
    return {"error": "call_agent_disabled"}
