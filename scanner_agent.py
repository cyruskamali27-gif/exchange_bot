import asyncio
import logging
import re
import time

from telethon import TelegramClient, events
from telegram import Bot

from config import (
    TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE,
    ADMIN_ID, BOT_TOKEN, EXCLUDED_IDS,
    VOICE_REPLIES_ENABLED, PRICE_REPLIES_ALWAYS_TEXT,
)
from negotiation_agent import send_intro_message, process_message, conversations
from contacted_users import is_contacted, add as add_contacted
from voice_agent import voice_to_text, send_voice_message
from natural_replies import get_reply
from agent_memory import log_conversation, detect_customer_tone

logging.basicConfig(level=logging.INFO)

last_message_time = {}

_PRICE_KEYWORDS = [
    "قیمت", "نرخ", "چنده", "چقدره", "چقدر", "rate", "price",
    "تتر", "دلار", "کانادا", "usdt", "usd", "cad",
]


def is_price_request(text):
    """True when the message is primarily a rate/price inquiry (no buy/sell intent)."""
    if not text:
        return False
    t = text.lower()
    has_price_word = any(k in t for k in _PRICE_KEYWORDS)
    has_deal_word  = any(k in t for k in ["خرید", "فروش", "buy", "sell"])
    # Pure price query: has price keyword but no deal action
    return has_price_word and not has_deal_word


def detect_currency(text):
    """Detect which currency the customer is asking about."""
    t = text.lower()
    if any(k in t for k in ["تتر", "usdt"]):
        return "USDT"
    if any(k in t for k in ["آمریکا", "امریکا", " usd"]):
        return "USD"
    return "CAD"  # default


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


async def run():
    client = TelegramClient('exchange_agent', TELETHON_API_ID, TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)
    print("✅ Scanner running...")

    @client.on(events.NewMessage)
    async def group_handler(event):
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

    @client.on(events.NewMessage(incoming=True))
    async def private_handler(event):
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

        last_message_time[uid] = time.time()

        # Decide reply mode
        price_req = is_price_request(text)
        if PRICE_REPLIES_ALWAYS_TEXT and price_req:
            use_voice = False   # price quotes always in text
        elif customer_sent_voice and VOICE_REPLIES_ENABLED:
            use_voice = True    # mirror voice only if voice accent is enabled
        else:
            use_voice = False   # text in → text out (default)

        conv = conversations.get(uid, {"type": "buyer", "amount": 10, "currency": "CAD"})
        # Update detected currency if customer mentioned one
        detected_cur = detect_currency(text)
        if detected_cur != "CAD" or "currency" not in conv:
            conv["currency"] = detected_cur
        reply = await process_message(client, uid, "user", text, conv["type"], conv["amount"])

        if use_voice:
            ok = await send_voice_message(client, uid, reply)
            if not ok:
                await client.send_message(uid, reply)
        else:
            await client.send_message(uid, reply)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(run())
