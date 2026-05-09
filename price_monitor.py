"""
price_monitor.py — Real-time price feed from Telegram channels.

- Listens for new messages on price channels
- Polls every 60 seconds as fallback
- Saves to SQLite (market_prices table) and current_price.json
"""

import asyncio
import json
import re
from datetime import datetime, timezone

from telethon import TelegramClient, events

from config import TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE
from price_engine import save_market_price

CURRENT_PRICE_JSON = "/var/www/exchange_bot/current_price.json"

client = TelegramClient(
    "price_monitor_session",
    TELETHON_API_ID,
    TELETHON_API_HASH
)

CHANNELS = {
    "tetherpriceFa": "USDT",
    "tahran_sabza":  "USD",
}

# In-memory last-seen prices for JSON export
_latest: dict[str, dict] = {}


def extract_price(text: str) -> int | None:
    text = text.replace(",", "").replace("،", "")
    nums = re.findall(r"\d{5,7}", text)
    valid = [int(n) for n in nums if 50_000 <= int(n) <= 250_000]
    return max(valid) if valid else None


def _write_json():
    """Write latest prices to current_price.json (read by exchange_brain)."""
    try:
        with open(CURRENT_PRICE_JSON, "w", encoding="utf-8") as f:
            json.dump(_latest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("JSON write error:", e)


async def _process_message(message, currency: str, source: str):
    try:
        if not message.text:
            return

        msg_time = message.date.astimezone(timezone.utc)
        age_sec  = (datetime.now(timezone.utc) - msg_time).total_seconds()
        if age_sec > 3600:
            return

        price = extract_price(message.text)
        if not price:
            return

        buy_price = price - 4000

        save_market_price(
            currency=currency,
            source=source,
            sell_price=price,
            buy_price=buy_price,
        )

        _latest[currency] = {
            "price":      price,
            "buy":        buy_price,
            "source":     source,
            "updated_at": datetime.now().isoformat(),
        }
        _write_json()

        print(f"✅ {currency} | sell={price:,} | buy={buy_price:,} | @{source}")

    except Exception as e:
        print("PROCESS ERROR:", e)


@client.on(events.NewMessage)
async def handler(event):
    try:
        chat = await event.get_chat()
        username = getattr(chat, "username", None)
        if username not in CHANNELS:
            return
        await _process_message(event.message, CHANNELS[username], username)
    except Exception as e:
        print("HANDLER ERROR:", e)


async def _poll_loop():
    """Poll channels every 60 seconds as a fallback."""
    while True:
        await asyncio.sleep(60)
        for channel, currency in CHANNELS.items():
            try:
                entity   = await client.get_entity(channel)
                messages = await client.get_messages(entity, limit=3)
                for msg in messages:
                    await _process_message(msg, currency, channel)
            except Exception as e:
                print(f"POLL {channel}:", e)


async def main():
    await client.start(phone=TELETHON_PHONE)
    print("✅ price monitor running")

    # Initial fetch
    for channel, currency in CHANNELS.items():
        try:
            entity   = await client.get_entity(channel)
            messages = await client.get_messages(entity, limit=5)
            for msg in messages:
                await _process_message(msg, currency, channel)
        except Exception as e:
            print(channel, e)

    asyncio.create_task(_poll_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
