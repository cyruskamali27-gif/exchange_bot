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
    PRICE_CHANNELS_ALL, PRICE_CHANNELS_USDT, PRICE_CHANNELS_USD_CAD,
    MAX_PRICE_AGE_MINUTES,
)
from price_engine import save_market_price

logging.basicConfig(level=logging.INFO)

# Live price cache: currency → {price, source, updated_at, is_fresh}
_price_cache = {}

_CURRENCIES = {
    "USDT": ["تتر", "usdt", "USDT", "تتهر", "tether", "تتر:", "تدر"],
    "USD":  ["دلار آمریکا", "دلار امریکا", "dollar", "USD", "usd", "دلار"],
    "CAD":  ["دلار کانادا", "کانادایی", "کانادا", "CAD", "cad"],
}

_SELL_WORDS = ["فروش", "sell", "فروختن", "میفروشم", "می‌فروشم", "می فروشم", "فروشی"]
_BUY_WORDS  = ["خرید", "buy", "خریدن", "میخرم", "می‌خرم", "می خرم", "خریدار"]

_METHOD_ETRANSFER = ["حواله", "e-transfer", "etransfer", "ترنسفر", "اینترک", "interac"]
_METHOD_CASH      = ["نقدی", "نقد", "cash", "کش"]
_METHOD_CHEQUE    = ["چک", "cheque", "check"]

# Marketplace / non-currency blacklist — ignore these posts entirely
_MARKETPLACE_BLACKLIST = [
    "فروش ماشین", "فروش خودرو", "فروش آپارتمان", "فروش ملک", "فروش موبایل",
    "فروش لپتاپ", "فروش تلویزیون", "فروش یخچال", "فروش لوازم", "فروش اجناس",
    "کرایه", "اجاره", "رهن", "طلافروشی", "زیور آلات", "جواهر",
    "car for sale", "apartment", "furniture", "appliance", "phone for sale",
    "laptop for sale", "tv for sale", "dollar item", "dollar store",
    "5 dollar", "10 dollar", "20 dollar", "50 dollar",
    "فروش وسایل", "اثاثیه", "مبل", "کاناپه", "فرش",
]

# Only persist extractions above this confidence to the database.
_MIN_OCR_CONFIDENCE = 0.49


def _is_marketplace_post(text: str) -> bool:
    """Return True if this message is clearly NOT a currency exchange post."""
    if not text:
        return False
    t = text.lower()
    if any(phrase in t for phrase in _MARKETPLACE_BLACKLIST):
        return True
    # "X dollar" patterns where X is small (product price, not exchange rate)
    import re as _re
    small_dollar = _re.findall(r"(\d+)\s*(?:dollar|دلار)", t)
    if small_dollar and all(int(n) < 500 for n in small_dollar):
        # Only block if there's no currency-exchange keyword present
        exchange_kw = ["تتر", "usdt", "نرخ", "صرافی", "خرید دلار", "فروش دلار",
                       "حواله", "کانادا", "cad", "نقدی", "چک"]
        if not any(kw in t for kw in exchange_kw):
            return True
    return False


def _detect_method(text: str) -> str:
    """Detect payment method from text: etransfer | cash | cheque | general"""
    t = text.lower()
    if any(k in t for k in _METHOD_ETRANSFER):
        return "etransfer"
    if any(k in t for k in _METHOD_CASH):
        return "cash"
    if any(k in t for k in _METHOD_CHEQUE):
        return "cheque"
    return "general"


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
    Returns list of dicts: {currency, price, direction, confidence, payment_method}
    direction: 'sell' | 'buy' | None
    """
    if not text:
        return []

    # Reject marketplace / non-currency posts immediately
    if _is_marketplace_post(text):
        return []

    results = []
    # Always work on normalised text — Farsi digits → ASCII, commas cleaned
    norm = _normalize_digits(text)

    # Generic price pattern (5-6 digit with optional separator)
    price_pat = re.compile(r"(\d{2,3}[,،]?\d{3}|\d{5,6})")

    for cur, keywords in _CURRENCIES.items():
        for kw in keywords:
            # Normalise the keyword too (in case it contains Farsi digits)
            kw_norm = _normalize_digits(kw)
            # Allow any combination of whitespace, newlines, colons, or dashes
            # between the keyword and the price value
            pattern = re.compile(
                rf"{re.escape(kw_norm)}[\s\n\r:：\-–|،,]*(\d{{2,3}}[,،]?\d{{3}}|\d{{5,6}})",
                re.IGNORECASE | re.MULTILINE,
            )
            for m in pattern.finditer(norm):
                p = _parse_price(m.group(1))
                if p:
                    ctx_start = max(0, m.start() - 60)
                    ctx_end   = min(len(norm), m.end() + 60)
                    ctx = norm[ctx_start:ctx_end]
                    direction = None
                    if any(w in ctx for w in _SELL_WORDS):
                        direction = "sell"
                    elif any(w in ctx for w in _BUY_WORDS):
                        direction = "buy"
                    results.append({
                        "currency":       cur,
                        "price":          p,
                        "direction":      direction,
                        "confidence":     0.9,
                        "payment_method": _detect_method(norm),
                    })

    # Fallback: bare price pattern — lower confidence, one match per message
    if not results:
        for m in price_pat.finditer(norm):
            p = _parse_price(m.group(0))
            if p:
                ctx_start = max(0, m.start() - 60)
                ctx_end   = min(len(norm), m.end() + 60)
                ctx = norm[ctx_start:ctx_end]
                direction = None
                if any(w in ctx for w in _SELL_WORDS):
                    direction = "sell"
                elif any(w in ctx for w in _BUY_WORDS):
                    direction = "buy"
                results.append({
                    "currency":       "USDT",
                    "price":          p,
                    "direction":      direction,
                    "confidence":     0.5,
                    "payment_method": "general",
                })
                break

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
        cur            = item["currency"]
        price          = item["price"]
        direction      = item["direction"]
        confidence     = item["confidence"]
        payment_method = item.get("payment_method", "general")

        # Skip noisy low-confidence bare-pattern extractions
        if confidence < _MIN_OCR_CONFIDENCE:
            logging.debug(
                "OCR skip low-conf: %s %s conf=%.1f @%s",
                cur, price, confidence, source
            )
            continue

        # Update in-memory cache
        _price_cache[cur] = {
            "price":      price,
            "source":     source,
            "updated_at": datetime.now(),
            "is_fresh":   True,
        }

        # Persist to DB — store sell/buy separately with method tag
        sell = price if direction == "sell" else (price if direction is None else None)
        buy  = price if direction == "buy"  else None
        save_market_price(
            cur, source,
            sell_price=sell,
            buy_price=buy,
            confidence=confidence,
            payment_method=payment_method,
        )
        print(f"💱 {cur} {direction or '~'} {price:,} تومان [{payment_method}] از @{source} (conf={confidence:.1f})")


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
