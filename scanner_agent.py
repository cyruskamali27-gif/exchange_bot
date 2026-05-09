import asyncio
import logging
import re
import time

from telethon import TelegramClient, events
from telegram import Bot

from config import (
    TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE,
    ADMIN_ID, BOT_TOKEN, EXCLUDED_IDS,
    VOICE_REPLIES_ENABLED,
    TEST_GROUP_ONLY, TEST_GROUP_ID,
)
from negotiation_agent import send_intro_message, process_message, conversations
from contacted_users import is_contacted, add as add_contacted
from voice_agent import voice_to_text, send_voice_message
from natural_replies import get_reply
from agent_memory import log_conversation, detect_customer_tone

logging.basicConfig(level=logging.INFO)

last_message_time = {}

# Tracks first message time + type per user; reset after 5 min idle
_conv_start = {}
_CONV_RESET_IDLE = 300  # seconds

# Keywords that force text-only reply (rules 4)
_PRICE_KEYWORDS = [
    "قیمت", "نرخ", "چنده", "چقدره", "چقدر",
    "rate", "price", "buy price", "sell price",
    "تتر", "usdt",
    "دلار", "usd", "cad", "کانادا",
    "نرخ خرید", "نرخ فروش",
]

# 1 minute: after this, text-started conversations switch to voice
_VOICE_ESCALATE_SECONDS = 60


def is_price_request(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in _PRICE_KEYWORDS)


def detect_currency(text):
    t = text.lower()
    if any(k in t for k in ["تتر", "usdt"]):
        return "USDT"
    if any(k in t for k in ["آمریکا", "امریکا", " usd"]):
        return "USD"
    return "CAD"


def extract_amount(text):
    nums = re.findall(r'\d+', text.replace(',', ''))
    for n in nums:
        v = float(n)
        if 10 <= v <= 50000:
            return v
    return 10


def detect_type(text):
    t = text.lower()
    if "فروش" in t or "sell" in t:
        return "seller"
    if "خرید" in t or "buy" in t:
        return "buyer"
    return None


def _resolve_voice(uid: int, customer_sent_voice: bool, price_req: bool) -> bool:
    """
    Decide whether to reply with voice.

    Rule 1: text in  → text out
    Rule 2: voice in → voice out
    Rule 3: text-started convo > 60 s → voice (escalate)
    Rule 4: price request → always text, regardless of above
    """
    if not VOICE_REPLIES_ENABLED:
        return False

    # Rule 4 always wins
    if price_req:
        return False

    now = time.time()
    start = _conv_start.get(uid)

    # Reset stale conversation tracking
    if start and (now - last_message_time.get(uid, now)) > _CONV_RESET_IDLE:
        _conv_start.pop(uid, None)
        start = None

    # Record first message of this conversation
    if start is None:
        _conv_start[uid] = {"time": now, "type": "voice" if customer_sent_voice else "text"}
        start = _conv_start[uid]

    # Rule 2: voice in → voice out
    if customer_sent_voice:
        return True

    # Rule 3: text-started convo older than 60 s → escalate to voice
    if start["type"] == "text" and (now - start["time"]) > _VOICE_ESCALATE_SECONDS:
        return True

    # Rule 1: text in → text out
    return False


async def _handle_message(client, uid, text, customer_sent_voice=False):
    """Core message handler shared by private and test-group paths."""
    last_message_time[uid] = time.time()

    price_req = is_price_request(text)
    use_voice = _resolve_voice(uid, customer_sent_voice, price_req)

    conv = conversations.get(uid, {"type": "buyer", "amount": 10, "currency": "CAD"})
    detected_cur = detect_currency(text)
    if detected_cur != "CAD" or "currency" not in conv:
        conv["currency"] = detected_cur

    reply = await process_message(
        client, uid, "user", text, conv["type"], conv["amount"],
        is_price_request=price_req,
    )
    return reply, use_voice


async def run():
    client = TelegramClient('exchange_agent', TELETHON_API_ID, TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)

    if TEST_GROUP_ONLY:
        print(f"⚠️  TEST_GROUP_ONLY=true — فقط گروه تست ({TEST_GROUP_ID}) فعال است")
        print("   پیام‌های خصوصی و سایر گروه‌ها نادیده گرفته می‌شوند")
    print("✅ Scanner running...")

    # ── Test-group conversation handler ──────────────────────────
    @client.on(events.NewMessage(chats=[TEST_GROUP_ID], incoming=True))
    async def test_group_handler(event):
        if event.out:
            return

        sender = await event.get_sender()
        uid = sender.id

        if uid in EXCLUDED_IDS:
            return

        customer_sent_voice = False

        if event.message.voice:
            customer_sent_voice = True
            audio = await event.download_media(bytes)
            text  = await voice_to_text(audio)
            if not text:
                await event.respond("نشنیدم، دوباره بگو.")
                return
        else:
            text = event.message.message or ""

        if not text or len(text) < 2:
            return

        reply, use_voice = await _handle_message(client, uid, text, customer_sent_voice)

        if use_voice:
            ok = await send_voice_message(client, TEST_GROUP_ID, reply)
            if not ok:
                await event.respond(reply)
        else:
            await event.respond(reply)

    # ── Production group scanner (outreach to new leads) ─────────
    @client.on(events.NewMessage)
    async def group_handler(event):
        if TEST_GROUP_ONLY:
            return

        if event.out or event.is_private:
            return

        text = event.message.message or ""
        if len(text) < 3:
            return

        t = detect_type(text)
        if not t:
            return

        sender = await event.get_sender()
        uid = sender.id

        if uid in EXCLUDED_IDS or is_contacted(uid):
            return

        amount = extract_amount(text)
        add_contacted(uid)

        await asyncio.sleep(2)

        reply = await send_intro_message(client, uid, "user", t, amount)
        await client.send_message(uid, reply)

        bot = Bot(token=BOT_TOKEN)
        deal_label = "فروش" if t == "seller" else "خرید"
        await bot.send_message(
            ADMIN_ID,
            f"👤 مشتری جدید: `{uid}` | {amount} CAD | {deal_label}",
            parse_mode="Markdown"
        )

    # ── Private chat handler ──────────────────────────────────────
    @client.on(events.NewMessage(incoming=True))
    async def private_handler(event):
        if TEST_GROUP_ONLY:
            return

        if event.out or not event.is_private:
            return

        sender = await event.get_sender()
        uid = sender.id

        if uid in EXCLUDED_IDS:
            return

        customer_sent_voice = False

        if event.message.voice:
            customer_sent_voice = True
            audio = await event.download_media(bytes)
            text  = await voice_to_text(audio)
            if not text:
                await event.respond("نشنیدم، دوباره بگو.")
                return
        else:
            text = event.message.message or ""

        if not text:
            return

        reply, use_voice = await _handle_message(client, uid, text, customer_sent_voice)

        if use_voice:
            ok = await send_voice_message(client, uid, reply)
            if not ok:
                await client.send_message(uid, reply)
        else:
            await client.send_message(uid, reply)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(run())
