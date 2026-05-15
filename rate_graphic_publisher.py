#!/usr/bin/env python3
"""
rate_graphic_publisher.py — Cyrus Global Exchange Automatic Rate Publisher

Price Rules:
  - Cyrus SELL = Bahmani SELL  (exactly the same, no adjustment)
  - Cyrus BUY  = Bahmani BUY + 500 تومان

Poster order: CAD → USD → EUR → USDT → USACAN
Missing OCR data → poster still generated with --- placeholders.
Only skipped if template file itself is missing.
"""

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytesseract
from PIL import Image, ImageDraw, ImageFont
from telethon import TelegramClient, events

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAS_BIDI = True
except ImportError:
    HAS_BIDI = False
    logging.warning("arabic-reshaper / python-bidi not installed")

try:
    import jdatetime
    HAS_JDATE = True
except ImportError:
    HAS_JDATE = False

import requests as _requests
from config import TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE, BOT_TOKEN

# ─── Configuration ────────────────────────────────────────────────────────────
SOURCE_CHANNEL = "SarafiBahmaniCa"
_ch = os.environ.get("PUBLISH_CHANNEL", "@cyrusGlobalExchange")
TARGET_CHANNEL = _ch if _ch.startswith("@") or _ch.lstrip("-").isdigit() else f"@{_ch}"
RATES_FILE    = Path("/var/www/exchange_bot/latest_rates.json")
GENERATED_DIR = Path("/var/www/exchange_bot/generated/rates")
ASSETS_DIR    = Path("/var/www/exchange_bot/assets/posters")
SESSION       = "rate_publisher"
TORONTO_TZ    = ZoneInfo("America/Toronto")
MONITOR_HOURS = 12   # Scan Bahmani every hour for this many hours after start

# Price adjustment rules
BUY_ADJ  = int(os.environ.get("PUBLISHER_BUY_ADJ", "500"))
# SELL_ADJ is ZERO — Cyrus sell = Bahmani sell exactly
SELL_ADJ = 0

FONT_BOLD       = "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"
FONT_REGULAR    = "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"
FONT_BOLD_EN    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

TEMPLATES = {
    "CAD":    ASSETS_DIR / "updated_cyrus_exchange_canada_poster.png",
    "USD":    ASSETS_DIR / "updated_cyrus_exchange_usa_poster.png",
    "EUR":    ASSETS_DIR / "updated_cyrus_exchange_europe_poster.png",
    "USDT":   ASSETS_DIR / "updated_cyrus_exchange_usdt_poster.png",
    "USACAN": ASSETS_DIR / "updated_cyrus_exchange_usacan_poster.png",
}

COORDS_FILE = Path("/var/www/exchange_bot/poster_coordinates.json")

def _load_coords() -> dict:
    try:
        return json.loads(COORDS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Cannot load {COORDS_FILE}: {exc}")

POSTER_COORDS: dict = _load_coords()

PUBLISH_ORDER = [
    ("CAD",    "دلار کانادا",  "Canada"),
    ("USD",    "دلار آمریکا", "USA"),
    ("EUR",    "یورو",         "Europe"),
    ("USDT",   "تتر (USDT)",   "USDT"),
    ("USACAN", "دلار آمریکا / کانادا", "USA/CAN"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rate_publisher")


# ─── Persian / font helpers ───────────────────────────────────────────────────

def fa(text: str) -> str:
    if not text or not HAS_BIDI:
        return text
    return get_display(arabic_reshaper.reshape(text))


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        log.warning(f"Font unavailable: {path}")
        return ImageFont.load_default()


def persian_date_full(dt: datetime) -> str:
    """Return full Persian date string: e.g. 'پنج‌شنبه ۲۴ اردیبهشت ۱۴۰۵'"""
    day_names = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یک‌شنبه"]
    _to_fa = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    if HAS_JDATE:
        jd       = jdatetime.datetime.fromgregorian(datetime=dt)
        day_fa   = day_names[dt.weekday()]          # Mon=0…Sun=6
        month_fa = jd.j_months_fa[jd.month - 1]    # Persian month name from jdatetime
        return (f"{day_fa} {str(jd.day).translate(_to_fa)} "
                f"{month_fa} {str(jd.year).translate(_to_fa)}")
    return dt.strftime("%A %d %B %Y")


# ─── Background colour sampling ───────────────────────────────────────────────

def sample_zone_bg(px_rgb, y1: int, y2: int, x1: int, x2: int) -> tuple:
    """
    Return the background colour of a zone by averaging its darkest pixels.
    Works even when text (bright) pixels dominate the zone center.
    """
    samples = []
    for sy in range(y1, y2, 4):
        for sx in range(x1, x2, 15):
            r, g, b = px_rgb[sx, sy]
            samples.append((r, g, b, (r + g + b) / 3))
    if not samples:
        return (0, 0, 0)
    samples.sort(key=lambda s: s[3])
    dark = samples[: max(1, len(samples) // 4)]
    return (
        sum(s[0] for s in dark) // len(dark),
        sum(s[1] for s in dark) // len(dark),
        sum(s[2] for s in dark) // len(dark),
    )


# ─── Poster generator ─────────────────────────────────────────────────────────

def _write_in_box(draw, px_rgb, box: list, text: str, font_path: str,
                  text_color: tuple, max_font: int = 80, min_font: int = 14):
    """Clear box, sample bg, center-write text. Shrinks font until text fits."""
    x1, y1, x2, y2 = box
    zone_w = x2 - x1
    zone_h = y2 - y1

    bg = sample_zone_bg(px_rgb, y1, y2, x1, x2)
    draw.rectangle([x1, y1, x2, y2], fill=(*bg, 255))

    font_size = min(max_font, max(min_font, int(zone_h * 0.60)))
    font      = load_font(font_path, font_size)

    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]

    while tw > zone_w - 8 and font_size > min_font:
        font_size -= 2
        font = load_font(font_path, font_size)
        bb = draw.textbbox((0, 0), text, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]

    tx = x1 + (zone_w - tw) // 2
    ty = y1 + (zone_h - th) // 2

    draw.text((tx + 1, ty + 1), text, font=font, fill=(0, 0, 0, 150))
    draw.text((tx,     ty),     text, font=font, fill=text_color)
    return font_size, bg


def generate_single_poster(currency: str, cur_name_fa: str,
                            buy: int | None, sell: int | None,
                            toronto_now: datetime) -> Path | None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    template_path = TEMPLATES.get(currency)
    if not template_path or not template_path.exists():
        log.error(f"[{currency}] Missing template: {template_path}")
        return None

    coords = POSTER_COORDS.get(currency)
    if not coords:
        log.error(f"[{currency}] No coordinates in {COORDS_FILE}")
        return None

    log.info(f"[{currency}] Generating poster  sell={sell}  buy={buy}")

    img    = Image.open(template_path).convert("RGBA")
    draw   = ImageDraw.Draw(img)
    px_rgb = img.convert("RGB").load()

    sell_str = f"{sell:,}" if sell is not None else "---"
    buy_str  = f"{buy:,}"  if buy  is not None else "---"

    # ── 1. Write prices strictly inside defined boxes ──────────────────────
    for box in coords.get("sell_boxes", []):
        fs, bg = _write_in_box(draw, px_rgb, box, sell_str,
                               FONT_BOLD_EN, (255, 255, 255, 255))
        log.info(f"  [{currency}] SELL='{sell_str}' box={box} font={fs}px bg={bg}")

    for box in coords.get("buy_boxes", []):
        fs, bg = _write_in_box(draw, px_rgb, box, buy_str,
                               FONT_BOLD_EN, (255, 255, 255, 255))
        log.info(f"  [{currency}] BUY='{buy_str}'  box={box} font={fs}px bg={bg}")

    # ── 2. Update date — strictly inside date_box, calendar icon untouched ─
    date_box = coords.get("date_box")
    if date_box:
        x1, y1, x2, y2 = date_box
        bg = sample_zone_bg(px_rgb, y1, y2, x1, x2)
        draw.rectangle([x1, y1, x2, y2], fill=(*bg, 255))

        box_h   = y2 - y1
        fa_size = max(14, int(box_h * 0.38))
        en_size = max(12, int(box_h * 0.27))

        font_fa = load_font(FONT_BOLD,       fa_size)
        font_en = load_font(FONT_REGULAR_EN, en_size)

        date_fa = fa(persian_date_full(toronto_now))
        date_en = toronto_now.strftime("%d %B %Y")

        x_c   = (x1 + x2) // 2
        fa_cy = coords.get("date_fa_cy", y1 + box_h // 3)
        en_cy = coords.get("date_en_cy", y1 + int(box_h * 0.73))

        draw.text((x_c, fa_cy), date_fa, font=font_fa,
                  fill=(240, 220, 160, 255), anchor="mm")
        draw.text((x_c, en_cy), date_en, font=font_en,
                  fill=(220, 200, 140, 255), anchor="mm")

        log.info(f"[{currency}] Date box={date_box}  "
                 f"Persian='{date_fa}'@cy={fa_cy}  "
                 f"Gregorian='{date_en}'@cy={en_cy}")

    # ── 3. Save ────────────────────────────────────────────────────────────
    ts_str   = toronto_now.strftime("%Y-%m-%d-%H%M")
    out_path = GENERATED_DIR / f"{ts_str}-{currency.lower()}.png"
    img.convert("RGB").save(str(out_path), "PNG")
    log.info(f"[{currency}] Saved → {out_path}")
    return out_path


# ─── Rate extraction ──────────────────────────────────────────────────────────

_CUR_KEYWORDS: dict[str, list[str]] = {
    "CAD":  ["دلار کانادا", "کانادا", "کاناد", "CAD", "canada"],
    "EUR":  ["یورو", "اروپا", "EUR", "euro"],
    "USDT": ["تتر", "USDT", "usdt", "tether", "تتهر"],
    "USD":  ["دلار آمریکا", "دلار امریکا", "امریکا", "USD", "dollar", "دلار"],
}
_BUY_KWS  = ["خرید", "buy", "خریدار", "ما می‌خریم"]
_SELL_KWS = ["فروش", "sell", "حواله", "ترنسفر", "transfer"]


def _normalize_ocr(text: str) -> str:
    for fa_d, en_d in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
        text = text.replace(fa_d, en_d)
    text = text.replace("،", ",")
    text = re.sub(r"(\d{1,3})\.(\d{3})\b", r"\1,\2", text)
    return text


def _extract_bahmani_rates(text: str) -> dict[str, dict]:
    text = _normalize_ocr(text)
    price_pat = re.compile(r"(\d{2,3},\d{3}|\d{5,6})")
    rates: dict[str, dict] = {}

    for m in price_pat.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            val = int(raw)
        except ValueError:
            continue
        if not (10_000 <= val <= 500_000):
            continue

        ctx       = text[max(0, m.start() - 200): min(len(text), m.end() + 200)]
        ctx_lower = ctx.lower()

        currency = None
        for cur in ("CAD", "EUR", "USDT", "USD"):
            if any(kw.lower() in ctx_lower for kw in _CUR_KEYWORDS[cur]):
                currency = cur
                break
        if not currency:
            continue

        direction: str | None = None
        if any(kw in ctx_lower for kw in _BUY_KWS):
            direction = "buy"
        elif any(kw in ctx_lower for kw in _SELL_KWS):
            direction = "sell"

        rates.setdefault(currency, {})

        if direction == "buy" and "buy" not in rates[currency]:
            rates[currency]["buy"] = val
            log.info(f"  [Bahmani] {currency} خرید = {val:,}")
        elif direction == "sell" and "sell" not in rates[currency]:
            rates[currency]["sell"] = val
            log.info(f"  [Bahmani] {currency} فروش = {val:,}")
        elif direction is None and "sell" not in rates[currency]:
            rates[currency]["sell"] = val
            log.info(f"  [Bahmani] {currency} (undirected→sell) = {val:,}")

    return rates


async def fetch_bahmani_rates(client: TelegramClient) -> dict | None:
    log.info(f"Scanning @{SOURCE_CHANNEL} ...")
    try:
        messages = await client.get_messages(SOURCE_CHANNEL, limit=15)
    except Exception as exc:
        log.error(f"Could not fetch from @{SOURCE_CHANNEL}: {exc}")
        return None

    collected: dict[str, dict] = {}

    for msg in messages:
        combined = ""

        if getattr(msg, "photo", None):
            try:
                img_bytes = await msg.download_media(bytes)
                img = Image.open(io.BytesIO(img_bytes))
                combined = (pytesseract.image_to_string(img, lang="fas") + "\n"
                            + pytesseract.image_to_string(img, lang="eng"))
                log.info(f"  OCR message {msg.id}")
            except Exception as exc:
                log.error(f"  OCR error msg {msg.id}: {exc}")

        text = getattr(msg, "text", None) or getattr(msg, "message", None) or ""
        if text.strip():
            combined += "\n" + text

        if not combined.strip():
            continue

        found = _extract_bahmani_rates(combined)
        for cur, prices in found.items():
            collected.setdefault(cur, {})
            for direction, price in prices.items():
                if direction not in collected[cur]:
                    collected[cur][direction] = price

    if not collected:
        log.warning(f"No rates extracted from @{SOURCE_CHANNEL}")
        return None

    log.info(f"Bahmani source rates: {collected}")
    return collected


def _fallback_from_cache(collected: dict) -> dict:
    try:
        cache = json.loads(
            Path("/var/www/exchange_bot/current_price.json").read_text()
        )
    except Exception:
        return collected

    MAX_AGE_S = 7200
    now_ts    = datetime.now(timezone.utc).timestamp()

    for cur_code in ("USD", "CAD", "EUR", "USDT"):
        if cur_code in collected and collected[cur_code]:
            continue

        entry = cache.get(cur_code, {})
        upd   = entry.get("updated_at", 0)
        try:
            upd_ts = datetime.fromisoformat(upd).timestamp() if isinstance(upd, str) else float(upd)
        except Exception:
            upd_ts = 0

        if now_ts - upd_ts > MAX_AGE_S:
            continue

        our_buy  = entry.get("our_buy")  or entry.get("buy")
        our_sell = entry.get("our_sell") or entry.get("price")

        if our_buy or our_sell:
            collected.setdefault(cur_code, {})
            if our_buy  and "buy"  not in collected[cur_code]:
                collected[cur_code]["buy"]  = int(our_buy)
            if our_sell and "sell" not in collected[cur_code]:
                collected[cur_code]["sell"] = int(our_sell)
            log.info(f"  [{cur_code}] cache fallback: buy={our_buy} sell={our_sell}")

    return collected


def adjust_rates(source: dict) -> dict:
    """
    Apply Cyrus pricing rules:
      SELL = Bahmani SELL  (no change — SELL_ADJ = 0)
      BUY  = Bahmani BUY + 500
    """
    result = {}
    for cur, prices in source.items():
        result[cur] = {}
        b_sell = prices.get("sell")
        b_buy  = prices.get("buy")

        if b_sell is not None:
            result[cur]["sell"] = b_sell          # Cyrus sell = Bahmani sell exactly
            log.info(f"  [{cur}] Cyrus SELL = Bahmani SELL = {b_sell:,}")
        if b_buy is not None:
            result[cur]["buy"] = b_buy + BUY_ADJ  # Cyrus buy = Bahmani buy + 500
            log.info(f"  [{cur}] Cyrus BUY  = {b_buy:,} + {BUY_ADJ} = {b_buy + BUY_ADJ:,}")

    log.info(f"Cyrus adjusted rates: {result}")
    return result


def log_pricing_formula_analysis(bahmani_rates: dict):
    """
    Logs observations about Bahmani's pricing source.
    Result: cannot confirm formula without live IRR reference data.
    """
    log.info("=== Bahmani Pricing Formula Investigation ===")
    u = bahmani_rates.get("USDT", {})
    c = bahmani_rates.get("CAD",  {})
    d = bahmani_rates.get("USD",  {})

    if u.get("sell") and u.get("buy"):
        spread = u["sell"] - u["buy"]
        mid    = (u["sell"] + u["buy"]) / 2
        log.info(f"  USDT sell={u['sell']:,} buy={u['buy']:,} spread={spread:,} ({spread/mid*100:.1f}% of mid)")

    if u.get("sell") and c.get("sell"):
        ratio = u["sell"] / c["sell"]
        log.info(f"  USDT/CAD rate ratio: {ratio:.4f}  (market CAD/USD ≈ 0.72–0.74 expected)")

    if d.get("sell") and u.get("sell"):
        diff = u["sell"] - d["sell"]
        log.info(f"  USDT sell vs USD sell difference: {diff:,} toman")

    log.info("  Conclusion: No confirmed backup formula — live Bahmani data required.")
    log.info("  If Bahmani does not post, mark rates unavailable and wait.")
    log.info("==============================================")


# ─── State management ─────────────────────────────────────────────────────────

def load_stored_rates() -> dict:
    try:
        if RATES_FILE.exists():
            return json.loads(RATES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_rates(rates: dict, now: datetime):
    data = {"rates": rates, "updated_at": now.isoformat()}
    RATES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"Rates saved → {RATES_FILE}")


def rates_changed(new: dict, stored: dict) -> bool:
    old = stored.get("rates", {})
    for cur, prices in new.items():
        for direction, price in prices.items():
            if old.get(cur, {}).get(direction) != price:
                return True
    return False


# ─── Posting ──────────────────────────────────────────────────────────────────

def post_to_channel(image_path: Path) -> int:
    """Post image-only (no caption) via Bot API. Returns message_id."""
    log.info(f"Posting {image_path.name} → {TARGET_CHANNEL}")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as f:
        resp = _requests.post(
            url,
            data={"chat_id": TARGET_CHANNEL},
            files={"photo": f},
            timeout=30,
        )
    data = resp.json()
    if data.get("ok"):
        msg_id = data["result"]["message_id"]
        log.info(f"Posted {image_path.name} → message_id={msg_id}")
        return msg_id
    err = data.get("description", "unknown error")
    log.error(f"Bot API error: {err}")
    raise RuntimeError(err)


# ─── Main scan-and-post ───────────────────────────────────────────────────────

async def scan_and_post(client: TelegramClient, force: bool = False):
    toronto_now = datetime.now(TORONTO_TZ)

    log.info(f"=== scan_and_post force={force} {toronto_now.strftime('%Y-%m-%d %H:%M %Z')} ===")
    log.info(f"Source: https://t.me/{SOURCE_CHANNEL}")

    source_rates = await fetch_bahmani_rates(client)
    if not source_rates:
        source_rates = {}
        log.warning("Bahmani scan returned nothing — trying cache fallback")

    adjusted = adjust_rates(source_rates) if source_rates else {}
    adjusted = _fallback_from_cache(adjusted)

    if source_rates:
        log_pricing_formula_analysis(source_rates)

    if not adjusted:
        log.warning("No rates from any source — skipping post")
        return

    stored  = load_stored_rates()
    changed = rates_changed(adjusted, stored)

    if not force and not changed:
        log.info("Rates unchanged — no post needed")
        return

    reason = "forced" if force else "rates changed"
    log.info(f"Generating {len(PUBLISH_ORDER)} posters ({reason})")

    posted_ids = []
    try:
        for cur_code, cur_name_fa, cur_name_en in PUBLISH_ORDER:
            prices = adjusted.get(cur_code, {})
            buy    = prices.get("buy")
            sell   = prices.get("sell")

            if sell is None and buy is None:
                log.warning(f"[{cur_code}] No price data — generating poster with --- placeholders")

            poster_path = generate_single_poster(
                cur_code, cur_name_fa, buy, sell, toronto_now
            )
            if poster_path is None:
                continue

            msg_id = post_to_channel(poster_path)
            posted_ids.append((cur_code, msg_id))
            await asyncio.sleep(2)

        if posted_ids:
            save_rates(adjusted, toronto_now)
            log.info(f"=== Posted {len(posted_ids)} poster(s) ===")
            for cur, mid in posted_ids:
                log.info(f"  {cur}: Telegram message_id={mid}")
        else:
            log.warning("No posters posted")
    except Exception as exc:
        log.error(f"scan_and_post failed: {exc}")

    # Final report
    log.info("--- Final Rate Report (5 posters) ---")
    for cur_code, _, cur_name_en in PUBLISH_ORDER:
        b = adjusted.get(cur_code, {})
        log.info(f"  {cur_code} ({cur_name_en}): sell={b.get('sell','---')}  buy={b.get('buy','---')}")
    log.info("--------------------------------------")


# ─── Scheduler ────────────────────────────────────────────────────────────────

async def run_publisher():
    log.info("Rate Graphic Publisher starting (12-hour active monitoring mode)")
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION, int(TELETHON_API_ID), TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)
    log.info("Telethon connected")

    monitor_start     = datetime.now(TORONTO_TZ)
    monitor_end       = monitor_start + timedelta(hours=MONITOR_HOURS)
    last_daily_date   = None
    last_checked_hour = -1

    log.info(f"12-hour monitoring: {monitor_start.strftime('%H:%M')} → "
             f"{monitor_end.strftime('%H:%M')} Toronto")

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def on_new_bahmani_message(event):
        log.info(f"New Bahmani message id={event.id} — immediate scan")
        await scan_and_post(client, force=False)

    while True:
        try:
            now   = datetime.now(TORONTO_TZ)
            today = now.date()
            hour  = now.hour

            if now <= monitor_end:
                # 12-hour window: check every hour for new rates
                if hour != last_checked_hour and now.minute < 10:
                    remaining = int((monitor_end - now).total_seconds() / 3600)
                    log.info(f"[12h monitor] Hourly check {hour:02d}:00  "
                             f"(~{remaining}h remaining in window)")
                    await scan_and_post(client, force=False)
                    last_checked_hour = hour
            else:
                # After 12-hour window: daily 9 AM forced post + hourly change check
                if hour == 9 and last_daily_date != today:
                    log.info("9 AM daily post triggered")
                    await scan_and_post(client, force=True)
                    last_daily_date   = today
                    last_checked_hour = hour
                elif hour != last_checked_hour and now.minute < 10:
                    log.info(f"Hourly check {hour:02d}:00")
                    await scan_and_post(client, force=False)
                    last_checked_hour = hour

        except Exception as exc:
            log.error(f"Scheduler error: {exc}")

        await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(run_publisher())
