import asyncio
import logging
import re
import time

from telethon import TelegramClient, events
from telegram import Bot

from config import TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE, ADMIN_ID, BOT_TOKEN, EXCLUDED_IDS
from negotiation_agent import send_intro_message, process_message, conversations
from contacted_users import is_contacted, add as add_contacted
from voice_agent import voice_to_text, send_voice_message


logging.basicConfig(level=logging.INFO)

last_message_time = {}

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
        await bot.send_message(ADMIN_ID, f"New user: {uid} | {amount}")

    @client.on(events.NewMessage(incoming=True))
    async def private_handler(event):

        if event.out or not event.is_private:
            return

        sender = await event.get_sender()
        uid = sender.id

        if uid in EXCLUDED_IDS:
            return

        use_voice = False

        if event.message.voice:
            await event.respond("🎧 در حال پردازش...")
            audio = await event.download_media(bytes)
            text = await voice_to_text(audio)
            use_voice = True
        else:
            text = event.message.message or ""

        if not text:
            return

        now = time.time()
        if now - last_message_time.get(uid, 0) > 60:
            use_voice = True

        last_message_time[uid] = now

        conv = conversations.get(uid, {"type": "buyer", "amount": 10})

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

