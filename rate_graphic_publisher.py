#!/usr/bin/env python3
"""
rate_graphic_publisher.py — Cyrus Global Exchange Automatic Rate Publisher

Price Rules:
  Cyrus SELL = Bahmani SELL  (exactly the same, no adjustment)
  Cyrus BUY  = Bahmani BUY + 500 toman

Templates : /var/www/exchange_bot/templates_marker/*.png  (clean masters, never modified)
Coords    : poster_coordinates.json  (exact marker boxes, no guessing, no auto-detection)
Output    : /var/www/exchange_bot/final/

No OCR, no date rendering on currency posters, no smart_text_engine.
Logo poster only: 2 date cells (Shamsi YYYY/MM/DD + Gregorian YYYY/MM/DD).
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests as _requests
from PIL import Image, ImageDraw, ImageFont
from telethon import TelegramClient, events

try:
    import jdatetime
    HAS_JDATE = True
except ImportError:
    HAS_JDATE = False

from config import TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE, BOT_TOKEN

# ─── Paths ─────────────────────────────────────────────────────────────────────
TEMPLATES_DIR = Path("/var/www/exchange_bot/templates")
COORDS_FILE   = Path("/var/www/exchange_bot/poster_coordinates.json")
RATES_FILE    = Path("/var/www/exchange_bot/latest_rates.json")
CACHE_FILE    = Path("/var/www/exchange_bot/current_price.json")
OUTPUT_DIR    = Path("/var/www/exchange_bot/final")

SOURCE_CHANNEL = "SarafiBahmaniCa"
_ch            = os.environ.get("PUBLISH_CHANNEL", "@cyrusGlobalExchange")
TARGET_CHANNEL = _ch if _ch.startswith("@") or _ch.lstrip("-").isdigit() else f"@{_ch}"

SESSION       = "rate_publisher"
TORONTO_TZ    = ZoneInfo("America/Toronto")
MONITOR_HOURS = 12
BUY_ADJ       = int(os.environ.get("PUBLISHER_BUY_ADJ", "500"))

# ─── Fonts ─────────────────────────────────────────────────────────────────────
FONT_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_FA = "/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Bold.ttf"

# ─── Template file map ─────────────────────────────────────────────────────────
# Source templates (templates_marker/) and output filenames (final/) share the same basename.
TEMPLATE_FILES = {
    "CAD":    "canada.png",
    "USD":    "usa.png",
    "EUR":    "eur.png",
    "USDT":   "usdt.png",
    "USACAN": "usacan.png",
    "LOGO":   "logo.png",
}
# Static output names — always overwritten with the latest render (matches hard_fix_renderer)
OUTPUT_FILES = {k: OUTPUT_DIR / v for k, v in TEMPLATE_FILES.items()}

# Logo date cell coordinates (from template_coordinate_profiles.json, 1254×1254 template)
LOGO_SHAMSI_CELL    = [637, 948,  960, 1058]   # top cell    → Shamsi YYYY/MM/DD
LOGO_GREGORIAN_CELL = [637, 1058, 960, 1168]   # bottom cell → Gregorian YYYY/MM/DD

# Publish order
PUBLISH_ORDER = [
    ("CAD",    "دلار کانادا",          "Canada"),
    ("USD",    "دلار آمریکا",          "USA"),
    ("USACAN", "دلار آمریکا / کانادا", "USA/CAN"),
    ("USDT",   "تتر (USDT)",           "USDT"),
    ("EUR",    "یورو",                  "Europe"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rate_publisher")

# ─── Coordinates ───────────────────────────────────────────────────────────────

def _load_coords() -> dict:
    try:
        return json.loads(COORDS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Cannot load {COORDS_FILE}: {exc}")

POSTER_COORDS: dict = _load_coords()

# ─── Font helper ───────────────────────────────────────────────────────────────

def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        log.warning(f"Font unavailable: {path}")
        return ImageFont.load_default()

# ─── Core render primitive ─────────────────────────────────────────────────────

def erase_and_write(
    working:   Image.Image,
    master:    Image.Image,
    box:       list,
    text:      str,
    font_path: str,
    color:     tuple,
    max_font:  int = 80,
    min_font:  int = 14,
) -> int:
    x1, y1, x2, y2 = [int(v) for v in box]
    bw = x2 - x1
    bh = y2 - y1

    # Restore exact master background (removes any previous render)
    master_roi = master.crop((x1, y1, x2, y2))
    working.paste(master_roi, (x1, y1))

    # Skip drawing if empty
    if not text:
        return 0

    # Find best font size
    font_size = min(max_font, max(min_font, int(bh * 0.60)))
    draw = ImageDraw.Draw(working)
    font = load_font(font_path, font_size)
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]

    while tw > bw - 8 and font_size > min_font:
        font_size -= 2
        font = load_font(font_path, font_size)
        bb = draw.textbbox((0, 0), text, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]

    tx = x1 + (bw - tw) // 2
    ty = y1 + (bh - th) // 2

    draw.text((tx, ty), text, font=font, fill=color)
    return font_size

# ─── Date helpers (logo only) ──────────────────────────────────────────────────

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

def _shamsi_yyyymmdd(dt: datetime) -> str:
    if HAS_JDATE:
        jd = jdatetime.datetime.fromgregorian(datetime=dt)
        y  = str(jd.year).translate(_FA_DIGITS)
        m  = str(jd.month).zfill(2).translate(_FA_DIGITS)
        d  = str(jd.day).zfill(2).translate(_FA_DIGITS)
        return f"{y}/{m}/{d}"
    return dt.strftime("%Y/%m/%d")

def _gregorian_yyyymmdd(dt: datetime) -> str:
    return dt.strftime("%Y/%m/%d")

# ─── Logo poster ───────────────────────────────────────────────────────────────

def generate_logo(toronto_now: datetime) -> Path | None:
    """
    Renders the logo poster.
    Only touches the two date cells — Shamsi (top) and Gregorian (bottom).
    No price boxes. All other pixels restored from master.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tpl_path = TEMPLATES_DIR / TEMPLATE_FILES["LOGO"]
    if not tpl_path.exists():
        log.error(f"[LOGO] Template missing: {tpl_path}")
        return None

    master  = Image.open(tpl_path).convert("RGBA")
    working = master.copy()

    shamsi    = _shamsi_yyyymmdd(toronto_now)
    gregorian = _gregorian_yyyymmdd(toronto_now)

    fs1 = erase_and_write(working, master, LOGO_SHAMSI_CELL,
                          shamsi,    FONT_FA, (242, 222, 148, 255), max_font=44, min_font=14)
    fs2 = erase_and_write(working, master, LOGO_GREGORIAN_CELL,
                          gregorian, FONT_EN, (210, 190, 120, 255), max_font=44, min_font=14)
    log.info(f"[LOGO] shamsi='{shamsi}' fs={fs1}px  gregorian='{gregorian}' fs={fs2}px")

    rgb = working.convert("RGB")
    # Static name (always latest — matches hard_fix_renderer output)
    static_path = OUTPUT_FILES["LOGO"]
    rgb.save(str(static_path), "PNG")
    # Timestamped archive copy
    ts = toronto_now.strftime("%Y-%m-%d-%H%M")
    archive_path = OUTPUT_DIR / f"{ts}-logo.png"
    rgb.save(str(archive_path), "PNG")
    log.info(f"[LOGO] Saved → {static_path}  (archive: {archive_path.name})")
    return static_path

# ─── Currency poster ───────────────────────────────────────────────────────────

def generate_currency_poster(
    currency:    str,
    buy:         float | None,
    sell:        float | None,
    toronto_now: datetime,
) -> Path | None:
    """
    Renders a currency rate poster.
    ONLY writes sell_boxes and buy_boxes from poster_coordinates.json.
    Zero date rendering. No OCR. No coordinate guessing.
    Background for each box is always restored from the clean master first.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    tpl_name = TEMPLATE_FILES.get(currency)
    if not tpl_name:
        log.error(f"[{currency}] No template mapping")
        return None

    tpl_path = TEMPLATES_DIR / tpl_name
    if not tpl_path.exists():
        log.error(f"[{currency}] Template missing: {tpl_path}")
        return None

    coords = POSTER_COORDS.get(currency)
    if not coords:
        log.error(f"[{currency}] No coordinates in {COORDS_FILE}")
        return None

    master  = Image.open(tpl_path).convert("RGBA")
    working = master.copy()

    if currency in ("USACAN", "EUR"):
        sell_str = f"{sell:.2f}" if sell is not None else ""
        buy_str  = f"{buy:.2f}"  if buy  is not None else ""
    else:
        sell_str = f"{int(sell):,}" if sell is not None else ""
        buy_str  = f"{int(buy):,}"  if buy  is not None else ""

    WHITE = (255, 255, 255, 255)

    for box in coords.get("sell_boxes", []):
        fs = erase_and_write(working, master, box, sell_str, FONT_EN, WHITE)
        log.info(f"  [{currency}] SELL '{sell_str}' box={box} font={fs}px")

    for box in coords.get("buy_boxes", []):
        fs = erase_and_write(working, master, box, buy_str, FONT_EN, WHITE)
        log.info(f"  [{currency}] BUY  '{buy_str}'  box={box} font={fs}px")

    rgb = working.convert("RGB")
    # Static name (always latest — matches hard_fix_renderer output)
    static_path = OUTPUT_FILES[currency]
    rgb.save(str(static_path), "PNG")
    # Timestamped archive copy
    ts = toronto_now.strftime("%Y-%m-%d-%H%M")
    archive_path = OUTPUT_DIR / f"{ts}-{currency.lower()}.png"
    rgb.save(str(archive_path), "PNG")
    log.info(f"[{currency}] Saved → {static_path}  (archive: {archive_path.name})")
    return static_path

# ─── Rate loading (file-based, no OCR) ────────────────────────────────────────

def load_rates_from_files() -> dict:
    """
    Read rates from latest_rates.json (primary) and current_price.json (fallback).
    Stored values are final (already adjusted) — no BUY_ADJ re-applied.
    """
    rates: dict[str, dict] = {}

    try:
        data = json.loads(RATES_FILE.read_text(encoding="utf-8"))
        for cur, prices in data.get("rates", {}).items():
            rates[cur] = {}
            if prices.get("sell") is not None:
                rates[cur]["sell"] = prices["sell"]
            if prices.get("buy") is not None:
                rates[cur]["buy"] = prices["buy"]
        log.info(f"Loaded rates from {RATES_FILE}")
    except Exception as exc:
        log.warning(f"Could not read {RATES_FILE}: {exc}")

    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        for cur in ("USD", "CAD", "USDT"):
            entry    = cache.get(cur, {})
            our_sell = entry.get("our_sell") or entry.get("price")
            our_buy  = entry.get("our_buy")  or entry.get("buy")
            rates.setdefault(cur, {})
            if our_sell and rates[cur].get("sell") is None:
                rates[cur]["sell"] = int(our_sell)
                log.info(f"  [{cur}] sell from cache: {int(our_sell):,}")
            if our_buy and rates[cur].get("buy") is None:
                rates[cur]["buy"]  = int(our_buy)
                log.info(f"  [{cur}] buy from cache:  {int(our_buy):,}")
    except Exception as exc:
        log.warning(f"Could not read {CACHE_FILE}: {exc}")

    return rates

# ─── Bahmani text-only rate parsing (no image download, no pytesseract) ────────

_CUR_KEYWORDS: dict[str, list[str]] = {
    "CAD":  ["دلار کانادا", "کانادا", "کاناد", "CAD", "canada"],
    "EUR":  ["یورو", "اروپا", "EUR", "euro"],
    "USDT": ["تتر", "USDT", "usdt", "tether"],
    "USD":  ["دلار آمریکا", "دلار امریکا", "امریکا", "USD", "dollar", "دلار"],
}
_BUY_KWS  = ["خرید", "buy", "خریدار", "ما می‌خریم"]
_SELL_KWS = ["فروش", "sell", "حواله", "ترنسفر", "transfer"]

def _normalize(text: str) -> str:
    for fa_d, en_d in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
        text = text.replace(fa_d, en_d)
    text = text.replace("،", ",")
    text = re.sub(r"(\d{1,3})\.(\d{3})\b", r"\1,\2", text)
    return text

def _parse_bahmani_text(text: str) -> dict[str, dict]:
    text   = _normalize(text)
    pat    = re.compile(r"(\d{2,3},\d{3}|\d{5,6})")
    result: dict[str, dict] = {}

    for m in pat.finditer(text):
        val = int(m.group(1).replace(",", ""))
        if not (10_000 <= val <= 500_000):
            continue
        ctx   = text[max(0, m.start() - 200): m.end() + 200].lower()
        cur   = next((c for c, kws in _CUR_KEYWORDS.items()
                      if any(k.lower() in ctx for k in kws)), None)
        if not cur:
            continue
        direction = (
            "buy"  if any(k in ctx for k in _BUY_KWS)  else
            "sell" if any(k in ctx for k in _SELL_KWS) else
            "sell"
        )
        result.setdefault(cur, {})
        if direction not in result[cur]:
            result[cur][direction] = val
            log.info(f"  [Bahmani] {cur} {direction} = {val:,}")

    return result

def _adjust(source: dict) -> dict:
    result = {}
    for cur, prices in source.items():
        result[cur] = {}
        if prices.get("sell") is not None:
            result[cur]["sell"] = prices["sell"]
        if prices.get("buy") is not None:
            result[cur]["buy"] = prices["buy"] + BUY_ADJ
    return result

# ─── Derived rates ─────────────────────────────────────────────────────────────

def _derive_usacan(rates: dict) -> dict | None:
    usd = rates.get("USD", {})
    cad = rates.get("CAD", {})
    if not usd.get("sell") or not cad.get("sell"):
        return None
    sell = round(usd["sell"] / cad["sell"], 2)
    buy  = (round(usd["buy"] / cad["buy"], 2)
            if usd.get("buy") and cad.get("buy") else None)
    return {"sell": sell, "buy": buy}

def _derive_eur(rates: dict) -> dict | None:
    usd = rates.get("USD", {})
    cad = rates.get("CAD", {})
    if not usd.get("sell") or not cad.get("sell"):
        return None
    try:
        resp        = _requests.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        eur_per_usd = resp.json().get("rates", {}).get("EUR")
        if not eur_per_usd:
            return None
        eur_sell_toman = usd["sell"] / eur_per_usd
        eur_buy_toman  = usd["buy"]  / eur_per_usd if usd.get("buy") else None
        result = {"sell": round(eur_sell_toman / cad["sell"], 2)}
        if eur_buy_toman and cad.get("buy"):
            result["buy"] = round(eur_buy_toman / cad["buy"], 2)
        log.info(f"  [EUR] EUR/USD={eur_per_usd:.4f}  EUR/CAD sell={result['sell']}")
        return result
    except Exception as exc:
        log.warning(f"  [EUR] live rate fetch failed: {exc}")
        return None

# ─── Posting ───────────────────────────────────────────────────────────────────

def post_to_channel(image_path: Path) -> int:
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
        mid = data["result"]["message_id"]
        log.info(f"  Posted → message_id={mid}")
        return mid
    raise RuntimeError(data.get("description", "Bot API error"))

# ─── State ─────────────────────────────────────────────────────────────────────

def load_stored_rates() -> dict:
    try:
        return json.loads(RATES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_rates(rates: dict, now: datetime):
    data = {"rates": rates, "updated_at": now.isoformat()}
    RATES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Rates saved → {RATES_FILE}")

def rates_changed(new: dict, stored: dict) -> bool:
    old = stored.get("rates", {})
    for cur, prices in new.items():
        for direction, price in prices.items():
            if old.get(cur, {}).get(direction) != price:
                return True
    return False

# ─── Main scan-and-post ────────────────────────────────────────────────────────

async def scan_and_post(
    client:     TelegramClient,
    force:      bool = False,
    fresh_text: str | None = None,
):
    toronto_now = datetime.now(TORONTO_TZ)
    log.info(f"=== scan_and_post force={force} {toronto_now.strftime('%Y-%m-%d %H:%M %Z')} ===")

    # Rate resolution: fresh Bahmani text > stored JSON files
    adjusted: dict = {}

    if fresh_text:
        parsed = _parse_bahmani_text(fresh_text)
        if parsed:
            adjusted = _adjust(parsed)
            log.info(f"Rates from Bahmani text (adjusted): {adjusted}")

    if not adjusted:
        adjusted = load_rates_from_files()

    # Always re-derive USACAN and EUR — they are ratios, never stored reliably
    usacan = _derive_usacan(adjusted)
    if usacan:
        adjusted["USACAN"] = usacan
        log.info(f"  [USACAN] derived sell={usacan['sell']} buy={usacan.get('buy')}")

    eur = _derive_eur(adjusted)
    if eur:
        adjusted["EUR"] = eur

    if not adjusted:
        log.warning("No rate data — skipping post")
        return

    stored  = load_stored_rates()
    changed = rates_changed(adjusted, stored)

    if not force and not changed:
        log.info("Rates unchanged — no post needed")
        return

    reason = "forced" if force else "rates changed"
    log.info(f"Generating 1 logo + {len(PUBLISH_ORDER)} currency posters ({reason})")

    posted = []

    logo_path = generate_logo(toronto_now)
    if logo_path:
        mid = post_to_channel(logo_path)
        posted.append(("LOGO", mid))
        await asyncio.sleep(2)
    else:
        log.warning("[LOGO] Generation failed — skipping")

    for cur_code, _name_fa, _name_en in PUBLISH_ORDER:
        prices = adjusted.get(cur_code, {})
        path   = generate_currency_poster(
            cur_code, prices.get("buy"), prices.get("sell"), toronto_now
        )
        if path:
            mid = post_to_channel(path)
            posted.append((cur_code, mid))
            await asyncio.sleep(2)

    if posted:
        save_rates(adjusted, toronto_now)
        log.info(f"=== Posted {len(posted)} image(s) ===")
        for cur, mid in posted:
            log.info(f"  {cur}: message_id={mid}")
    else:
        log.warning("No posters posted")

    log.info("--- Rate Summary ---")
    for cur_code, _, name_en in PUBLISH_ORDER:
        b = adjusted.get(cur_code, {})
        log.info(f"  {cur_code:6s} ({name_en}): sell={b.get('sell','---')}  buy={b.get('buy','---')}")
    log.info("--------------------")

# ─── Scheduler ─────────────────────────────────────────────────────────────────

async def run_publisher():
    log.info("Rate Graphic Publisher starting")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION, int(TELETHON_API_ID), TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)
    log.info("Telethon connected")

    monitor_start     = datetime.now(TORONTO_TZ)
    monitor_end       = monitor_start + timedelta(hours=MONITOR_HOURS)
    last_daily_date   = None
    last_checked_hour = -1

    log.info(f"12h window: {monitor_start.strftime('%H:%M')} → {monitor_end.strftime('%H:%M')} Toronto")

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def on_bahmani(event):
        text = (getattr(event.message, "text", None)
                or getattr(event.message, "message", None)
                or "")
        if text.strip():
            log.info(f"New Bahmani text id={event.id} — parsing")
            await scan_and_post(client, force=False, fresh_text=text)
        else:
            log.info(f"New Bahmani message id={event.id} (no text) — reloading files")
            await scan_and_post(client, force=False)

    while True:
        try:
            now   = datetime.now(TORONTO_TZ)
            today = now.date()
            hour  = now.hour

            if now <= monitor_end:
                if hour != last_checked_hour and now.minute < 10:
                    remaining = int((monitor_end - now).total_seconds() / 3600)
                    log.info(f"[12h] Hourly check {hour:02d}:00 (~{remaining}h left)")
                    await scan_and_post(client, force=False)
                    last_checked_hour = hour
            else:
                if hour == 9 and last_daily_date != today:
                    log.info("9 AM daily post")
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
