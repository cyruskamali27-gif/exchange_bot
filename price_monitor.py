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
from price_engine import save_market_price, init_price_json, get_market_base

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
    """Merge latest prices into current_price.json (preserves currencies this process doesn't track)."""
    try:
        try:
            with open(CURRENT_PRICE_JSON, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
        existing.update(_latest)
        with open(CURRENT_PRICE_JSON, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
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


async def _health_check():
    """
    Every 60 s:
      1. Verify Telethon is still connected — reconnect if not.
      2. If no fresh USDT price in DB, trigger an immediate poll.
      3. Ensure current_price.json exists (seed from DB if missing).
    """
    while True:
        await asyncio.sleep(60)
        try:
            if not client.is_connected():
                print("⚡ Price monitor: reconnecting...")
                await client.connect()

            base = get_market_base("USDT")
            if not base:
                print("⚠️  Health: no fresh USDT price — polling now")
                for channel, currency in CHANNELS.items():
                    try:
                        entity = await client.get_entity(channel)
                        messages = await client.get_messages(entity, limit=3)
                        for msg in messages:
                            await _process_message(msg, currency, channel)
                    except Exception as e:
                        print(f"Health poll {channel}: {e}")

            init_price_json()   # re-seed JSON if it disappeared
        except Exception as e:
            print(f"Health check error: {e}")


async def main():
    await client.start(phone=TELETHON_PHONE)
    print("✅ price monitor running")

    # Seed JSON from DB immediately so exchange_brain has a fallback
    init_price_json()

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
    asyncio.create_task(_health_check())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
