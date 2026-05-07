from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto
import re
import asyncio
from datetime import datetime, timezone

from config import TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE
from price_engine import save_market_price

client = TelegramClient(
    "price_monitor_session",
    TELETHON_API_ID,
    TELETHON_API_HASH
)

CHANNELS = {
    "tetherpriceFa": "USDT",
    "tahran_sabza": "USD"
}

def extract_price(text):
    text = text.replace(",", "")
    nums = re.findall(r"\d{5,7}", text)

    valid = []
    for n in nums:
        v = int(n)
        if 50000 <= v <= 200000:
            valid.append(v)

    if not valid:
        return None

    return max(valid)

async def process_message(message, currency, source):
    try:
        if not message.text:
            return

        msg_time = message.date.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)

        diff = (now - msg_time).total_seconds()

        if diff > 3600:
            return

        price = extract_price(message.text)

        if not price:
            return

        buy_price = price - 4000

        save_market_price(
            currency=currency,
            source=source,
            sell_price=price,
            buy_price=buy_price
        )

        print(f"✅ {currency} updated")
        print(f"sell: {price}")
        print(f"buy: {buy_price}")

    except Exception as e:
        print("ERROR:", e)

@client.on(events.NewMessage)
async def handler(event):
    try:
        chat = await event.get_chat()

        username = getattr(chat, "username", None)

        if username not in CHANNELS:
            return

        currency = CHANNELS[username]

        await process_message(
            event.message,
            currency,
            username
        )

    except Exception as e:
        print("HANDLER ERROR:", e)

async def main():
    await client.start(phone=TELETHON_PHONE)

    print("✅ price monitor running")

    for channel, currency in CHANNELS.items():

        try:
            entity = await client.get_entity(channel)

            messages = await client.get_messages(
                entity,
                limit=5
            )

            for msg in messages:
                await process_message(
                    msg,
                    currency,
                    channel
                )

        except Exception as e:
            print(channel, e)

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
