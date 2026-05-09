import logging
from telegram import Bot
from natural_replies import voice_optimize
from voice_agent import generate_call_audio_url
from config import (
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER,
    BOT_TOKEN, ADMIN_ID,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("call_agent")

# ─── Credential check ────────────────────────────────────────────────────────

def _twilio_configured():
    return (
        TWILIO_ACCOUNT_SID and TWILIO_ACCOUNT_SID != "PASTE_ACCOUNT_SID_HERE"
        and TWILIO_AUTH_TOKEN and TWILIO_AUTH_TOKEN != "PASTE_AUTH_TOKEN_HERE"
        and TWILIO_FROM_NUMBER
    )


def _get_client():
    from twilio.rest import Client
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ─── TwiML builders ──────────────────────────────────────────────────────────

def _build_twiml_play(audio_url: str) -> str:
    """TwiML that plays a hosted MP3 (ElevenLabs + office ambience)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Play>{audio_url}</Play>'
        '<Pause length="1"/>'
        f'<Play>{audio_url}</Play>'
        '</Response>'
    )


def _build_twiml_say(message_text: str) -> str:
    """Fallback TwiML using Twilio Polly TTS when ElevenLabs is unavailable."""
    safe = (
        message_text
        .replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Say language="fa-IR" voice="Polly.Zeynep">{safe}</Say>'
        '<Pause length="1"/>'
        f'<Say language="fa-IR" voice="Polly.Zeynep">{safe}</Say>'
        '</Response>'
    )


# ─── Core call function ───────────────────────────────────────────────────────

async def call_customer(phone_number: str, message_text: str) -> dict:
    """
    Place an outbound Twilio call using ElevenLabs audio + office ambience.
    Falls back to Polly <Say> if audio generation fails.
    """
    if not phone_number or not phone_number.strip():
        log.warning("call_customer: phone_number is empty — notifying admin")
        await _notify_admin_no_phone(message_text)
        return {"success": False, "error": "no_phone", "notified_admin": True}

    if not _twilio_configured():
        log.error("Twilio credentials not configured in .env")
        return {"success": False, "error": "twilio_not_configured", "notified_admin": False}

    phone = phone_number.strip()
    if not phone.startswith("+"):
        log.warning("call_customer: phone '%s' has no country code — prepending +1", phone)
        phone = "+1" + phone.lstrip("0")

    # Try ElevenLabs + ambience first; fall back to Polly <Say>
    audio_url = await generate_call_audio_url(message_text)
    if audio_url:
        twiml = _build_twiml_play(audio_url)
        log.info("Using ElevenLabs audio: %s", audio_url)
    else:
        twiml = _build_twiml_say(voice_optimize(message_text))
        log.warning("ElevenLabs failed — falling back to Polly TTS")

    log.info("Initiating call → %s | message: %s", phone, message_text[:60])

    try:
        client = _get_client()
        call = client.calls.create(
            twiml=twiml,
            to=phone,
            from_=TWILIO_FROM_NUMBER,
        )
        log.info("Call queued | SID=%s | status=%s | to=%s", call.sid, call.status, phone)
        return {"success": True, "call_sid": call.sid, "status": call.status, "to": phone}

    except Exception as e:
        log.error("Twilio call failed → %s | error: %s", phone, e)
        return {"success": False, "error": str(e), "notified_admin": False}


async def _notify_admin_no_phone(message_text):
    try:
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"📞 تماس ناموفق — شماره وجود ندارد\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"پیام: {message_text[:200]}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"لطفاً شماره مشتری را اضافه کنید."
            )
        )
    except Exception as e:
        log.error("Failed to notify admin: %s", e)


# ─── Call status checker ─────────────────────────────────────────────────────

def get_call_status(call_sid: str) -> dict:
    """Look up a call by SID and return its current status."""
    if not _twilio_configured():
        return {"error": "twilio_not_configured"}
    try:
        client = _get_client()
        call = client.calls(call_sid).fetch()
        return {
            "call_sid": call.sid,
            "status": call.status,
            "duration": call.duration,
            "to": call.to,
            "from": call.from_,
            "start_time": str(call.start_time),
        }
    except Exception as e:
        log.error("get_call_status error: %s", e)
        return {"error": str(e)}


# ─── Dry-run ─────────────────────────────────────────────────────────────────

def dry_run(phone_number: str, message_text: str):
    """
    Validate inputs and print what would be sent — no actual API call.
    Returns True if everything looks ready.
    """
    print("─── DRY RUN ───────────────────────────────")
    print(f"  Twilio configured : {_twilio_configured()}")
    print(f"  From              : {TWILIO_FROM_NUMBER}")
    print(f"  To                : {phone_number or '(empty)'}")
    print(f"  Message           : {message_text[:80]}")
    print(f"  TwiML preview     :")
    print("  " + _build_twiml(message_text)[:200])
    print("───────────────────────────────────────────")
    if not phone_number:
        print("  ❌ No phone number — admin would be notified")
        return False
    if not _twilio_configured():
        print("  ❌ Twilio credentials not set in .env")
        return False
    print("  ✅ Ready for live call")
    return True


# ─── CLI entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, asyncio

    if len(sys.argv) == 3 and sys.argv[1] == "dry":
        dry_run(sys.argv[2], "سلام، این یک تماس آزمایشی از صرافی است.")

    elif len(sys.argv) == 4 and sys.argv[1] == "call":
        result = asyncio.run(call_customer(sys.argv[2], sys.argv[3]))
        print(result)

    else:
        print("Usage:")
        print("  python3 call_agent.py dry +1XXXXXXXXXX")
        print("  python3 call_agent.py call +1XXXXXXXXXX 'پیام شما'")
