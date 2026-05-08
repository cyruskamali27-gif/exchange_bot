import asyncio
import re
import logging
from PIL import Image
import pytesseract
import io
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from config import (
    TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE,
    PRICE_CHANNELS_ALL, PRICE_CHANNELS_USDT, PRICE_CHANNELS_USD,
    MAX_PRICE_AGE_MINUTES,
)
from price_engine import save_market_price

logging.basicConfig(level=logging.INFO)

# Live price cache: currency → {price, source, updated_at, is_fresh}
_price_cache = {}

_CURRENCIES = {
    "USDT": ["تتر", "usdt", "USDT", "تتهر"],
    "USD":  ["دلار آمریکا", "دلار امریکا", "USD", "usd"],
    "CAD":  ["دلار کانادا", "کانادا", "CAD", "cad"],
}

_SELL_WORDS = ["فروش", "sell", "فروختن"]
_BUY_WORDS  = ["خرید", "buy", "خریدن"]


def _normalize_digits(text):
    for fa, en in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
        text = text.replace(fa, en)
    return text.replace("،", "").replace(",", "")


def _parse_price(raw):
    """Return integer price if in plausible toman range, else None."""
    clean = _normalize_digits(raw).replace(" ", "")
    try:
        v = int(clean)
        if 30_000 <= v <= 250_000:
            return v
    except Exception:
        pass
    return None


def extract_prices_from_text(text):
    """
    Parse text from a Telegram message.
    Returns list of dicts: {currency, price, direction, confidence}
    direction: 'sell' | 'buy' | None
    """
    if not text:
        return []

    results = []
    norm = _normalize_digits(text)

    # Generic price pattern (5-6 digit with optional separator)
    price_pat = re.compile(r"(\d{2,3}[,،]?\d{3}|\d{5,6})")

    for cur, keywords in _CURRENCIES.items():
        for kw in keywords:
            # Look for keyword followed by price
            pattern = re.compile(
                rf"{re.escape(kw)}[\s:：\-–]*(\d{{2,3}}[,،]?\d{{3}}|\d{{5,6}})",
                re.IGNORECASE,
            )
            for m in pattern.finditer(text):
                p = _parse_price(m.group(1))
                if p:
                    # Determine direction from surrounding context (±50 chars)
                    ctx_start = max(0, m.start() - 50)
                    ctx_end   = min(len(text), m.end() + 50)
                    ctx = text[ctx_start:ctx_end]
                    direction = None
                    if any(w in ctx for w in _SELL_WORDS):
                        direction = "sell"
                    elif any(w in ctx for w in _BUY_WORDS):
                        direction = "buy"
                    results.append({
                        "currency":   cur,
                        "price":      p,
                        "direction":  direction,
                        "confidence": 0.9,
                    })

    # Fallback: bare price patterns — lower confidence
    if not results:
        for m in price_pat.finditer(norm):
            p = _parse_price(m.group(0))
            if p:
                ctx_start = max(0, m.start() - 50)
                ctx_end   = min(len(norm), m.end() + 50)
                ctx = norm[ctx_start:ctx_end]
                direction = None
                if any(w in ctx for w in _SELL_WORDS):
                    direction = "sell"
                elif any(w in ctx for w in _BUY_WORDS):
                    direction = "buy"
                results.append({
                    "currency":   "USDT",  # most common in these channels
                    "price":      p,
                    "direction":  direction,
                    "confidence": 0.5,
                })
                break  # one fallback per message is enough

    return results


def extract_price_from_text(text):
    """Legacy single-value helper used by other modules."""
    items = extract_prices_from_text(text)
    if items:
        return items[0]["price"]
    return None


async def read_prices_from_image(image_bytes):
    """OCR an image and return list of extracted prices."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        text_fa = pytesseract.image_to_string(img, lang="fas")
        text_en = pytesseract.image_to_string(img, lang="eng")
        return extract_prices_from_text(text_fa + "\n" + text_en)
    except Exception as e:
        logging.error(f"OCR error: {e}")
        return []


async def read_price_from_image(image_bytes):
    """Legacy single-value helper."""
    items = await read_prices_from_image(image_bytes)
    if items:
        return items[0]["price"]
    return None


def _is_fresh(updated_at):
    if not updated_at:
        return False
    return (datetime.now() - updated_at) < timedelta(minutes=MAX_PRICE_AGE_MINUTES)


def _update_cache_and_db(items, source):
    for item in items:
        cur   = item["currency"]
        price = item["price"]
        direction   = item["direction"]
        confidence  = item["confidence"]

        # Update in-memory cache
        _price_cache[cur] = {
            "price":      price,
            "source":     source,
            "updated_at": datetime.now(),
            "is_fresh":   True,
        }

        # Persist to DB
        sell = price if direction == "sell" else (price if direction is None else None)
        buy  = price if direction == "buy"  else None
        save_market_price(
            cur, source,
            sell_price=sell,
            buy_price=buy,
            confidence=confidence,
        )
        print(f"💱 {cur} {direction or '~'} {price:,} تومان از @{source} (conf={confidence:.1f})")


async def _process_message(msg, source):
    """Extract prices from a Telethon message object and persist them."""
    msg_time   = msg.date.astimezone(timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - msg_time).total_seconds() / 60
    if age_minutes > MAX_PRICE_AGE_MINUTES:
        return

    items = []
    if getattr(msg, "photo", None):
        image_bytes = await msg.download_media(bytes)
        items = await read_prices_from_image(image_bytes)
    if not items and getattr(msg, "text", None):
        items = extract_prices_from_text(msg.text)
    if not items and getattr(msg, "message", None):
        items = extract_prices_from_text(msg.message)

    if items:
        _update_cache_and_db(items, source)


async def fetch_latest_from_channels(client):
    """Poll all price channels every 5 minutes as a fallback."""
    while True:
        for channel in PRICE_CHANNELS_ALL:
            try:
                messages = await client.get_messages(channel, limit=5)
                for msg in messages:
                    await _process_message(msg, channel)
            except Exception as e:
                logging.error(f"Fetch error {channel}: {e}")
        await asyncio.sleep(300)


async def run_ocr_monitor():
    client = TelegramClient("exchange_ocr", int(TELETHON_API_ID), TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)
    print(f"✅ OCR Price Monitor — کانال‌ها: {PRICE_CHANNELS_ALL}")

    @client.on(events.NewMessage(chats=PRICE_CHANNELS_ALL))
    async def handler(event):
        try:
            source = getattr(event.chat, "username", "unknown")
            await _process_message(event.message, source)
        except Exception as e:
            logging.error(f"Handler error: {e}")

    asyncio.create_task(fetch_latest_from_channels(client))
    await client.run_until_disconnected()


def get_current_price(currency="USDT"):
    """Return cached price for the given currency, with freshness flag."""
    entry = _price_cache.get(currency)
    if not entry:
        return {
            "price":      None,
            "source":     "none",
            "updated_at": None,
            "is_fresh":   False,
        }
    fresh = _is_fresh(entry["updated_at"])
    if not fresh:
        print(f"⚠️ قیمت {currency} قدیمی — تازه نیست")
    return {**entry, "is_fresh": fresh}


if __name__ == "__main__":
    asyncio.run(run_ocr_monitor())
