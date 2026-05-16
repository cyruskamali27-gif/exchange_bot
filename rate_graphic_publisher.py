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

FONT_BOLD       = "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"
FONT_REGULAR    = "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"
FONT_BOLD_EN    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

TEMPLATES = {
    "CAD":    ASSETS_DIR / "updated_cyrus_exchange_canada_poster.png",
    "USD":    ASSETS_DIR / "updated_cyrus_exchange_usa_poster.png",
    "EUR":    ASSETS_DIR / "updated_cyrus_exchange_europe_poster.png",
    "USDT":   ASSETS_DIR / "updated_cyrus_exchange_usdt_poster.png",
    "USACAN": ASSETS_DIR / "updated_cyrus_exchange_usacan_poster.png",
}

COORDS_FILE   = Path("/var/www/exchange_bot/poster_coordinates.json")
DEBUG_DIR     = Path("/var/www/exchange_bot/debug_dates")
QC_REPORT_DIR = Path("/var/www/exchange_bot/qc_reports")

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


# ─── Date rendering helpers ───────────────────────────────────────────────────

def _draw_date_in_box(
    draw: ImageDraw.ImageDraw,
    px_rgb,
    date_box: list,
    currency: str,
    toronto_now: datetime,
) -> dict:
    """
    Clean date_box with matched background, then draw Persian line (top) and
    English line (below), both centered horizontally.  Vertical positions are
    computed dynamically from actual rendered text heights — no hardcoded cy.
    Returns layout dict used for debug preview and QC.
    """
    x1, y1, x2, y2 = date_box
    box_w = x2 - x1
    box_h = y2 - y1

    bg = sample_zone_bg(px_rgb, y1, y2, x1, x2)
    draw.rectangle([x1, y1, x2, y2], fill=(*bg, 255))

    date_fa_raw      = persian_date_full(toronto_now)
    date_en          = f"{toronto_now.day} {toronto_now.strftime('%B %Y')}"
    date_fa_rendered = fa(date_fa_raw)

    LINE_GAP = max(3, int(box_h * 0.08))
    PADDING  = max(3, int(box_h * 0.06))

    fa_size = max(10, int(box_h * 0.44))
    while fa_size >= 10:
        fa_font = load_font(FONT_BOLD, fa_size)
        bb = draw.textbbox((0, 0), date_fa_rendered, font=fa_font)
        if (bb[2] - bb[0]) <= box_w - 8:
            break
        fa_size -= 1

    en_size = max(8, int(box_h * 0.31))
    while en_size >= 8:
        en_font = load_font(FONT_REGULAR_EN, en_size)
        bb = draw.textbbox((0, 0), date_en, font=en_font)
        if (bb[2] - bb[0]) <= box_w - 8:
            break
        en_size -= 1

    fa_font = load_font(FONT_BOLD, fa_size)
    en_font = load_font(FONT_REGULAR_EN, en_size)

    fa_bb = draw.textbbox((0, 0), date_fa_rendered, font=fa_font)
    en_bb = draw.textbbox((0, 0), date_en, font=en_font)

    fa_w = fa_bb[2] - fa_bb[0]
    fa_h = fa_bb[3] - fa_bb[1]
    en_w = en_bb[2] - en_bb[0]
    en_h = en_bb[3] - en_bb[1]

    total_h   = fa_h + LINE_GAP + en_h
    block_top = y1 + max(PADDING, (box_h - total_h) // 2)

    fa_y = block_top
    en_y = fa_y + fa_h + LINE_GAP

    fa_x = x1 + (box_w - fa_w) // 2
    en_x = x1 + (box_w - en_w) // 2

    SHADOW   = (0, 0, 0, 180)
    COLOR_FA = (240, 220, 160, 255)
    COLOR_EN = (220, 200, 140, 255)

    draw.text((fa_x + 1, fa_y + 1), date_fa_rendered, font=fa_font, fill=SHADOW)
    draw.text((fa_x,     fa_y),     date_fa_rendered, font=fa_font, fill=COLOR_FA)
    draw.text((en_x + 1, en_y + 1), date_en,          font=en_font, fill=SHADOW)
    draw.text((en_x,     en_y),     date_en,          font=en_font, fill=COLOR_EN)

    log.info(
        f"[{currency}] Date '{date_fa_raw}' / '{date_en}'  "
        f"fa={fa_size}px en={en_size}px  "
        f"fa_y={fa_y}→{fa_y+fa_h}  en_y={en_y}→{en_y+en_h}  "
        f"box=[{y1},{y2}] bg={bg}"
    )
    return {
        "date_box": date_box,
        "date_fa":  date_fa_raw,
        "date_en":  date_en,
        "fa_y": fa_y, "fa_h": fa_h, "fa_w": fa_w, "fa_size": fa_size,
        "en_y": en_y, "en_h": en_h, "en_w": en_w, "en_size": en_size,
        "bg":   bg,
    }


def _save_date_debug(img: Image.Image, currency: str, layout: dict) -> Path:
    """Save annotated debug image: red box = date_box, green = center line,
    blue = Persian text bounds, cyan = English text bounds."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    dbg = img.copy()
    d   = ImageDraw.Draw(dbg)

    x1, y1, x2, y2 = layout["date_box"]
    fa_y = layout["fa_y"]
    fa_h = layout["fa_h"]
    en_y = layout["en_y"]
    en_h = layout["en_h"]

    d.rectangle([x1 - 2, y1 - 2, x2 + 2, y2 + 2], outline=(255, 0, 0), width=3)
    mid_y = (y1 + y2) // 2
    d.line([(x1, mid_y), (x2, mid_y)], fill=(0, 220, 0), width=1)
    d.rectangle([x1, fa_y, x2, fa_y + fa_h], outline=(80, 80, 255), width=1)
    d.rectangle([x1, en_y, x2, en_y + en_h], outline=(0, 200, 200), width=1)

    out = DEBUG_DIR / f"debug_date_{currency.lower()}.png"
    dbg.convert("RGB").save(str(out), "PNG")
    return out


def _qc_date(currency: str, toronto_now: datetime, layout: dict) -> tuple[bool, list]:
    """Validate that rendered date is today, inside the box, and non-overlapping."""
    errors = []
    x1, y1, x2, y2 = layout["date_box"]

    expected_en = f"{toronto_now.day} {toronto_now.strftime('%B %Y')}"
    if layout["date_en"] != expected_en:
        errors.append(f"English date: got '{layout['date_en']}', expected '{expected_en}'")

    if HAS_JDATE:
        jd      = jdatetime.datetime.fromgregorian(datetime=toronto_now)
        year_fa = str(jd.year).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
        if year_fa not in layout["date_fa"]:
            errors.append(f"Persian year {year_fa} not found in '{layout['date_fa']}'")

    if layout["fa_y"] < y1:
        errors.append(f"Persian text above box (fa_y={layout['fa_y']} < {y1})")
    if layout["en_y"] + layout["en_h"] > y2:
        errors.append(f"English text below box ({layout['en_y'] + layout['en_h']} > {y2})")

    if layout["fa_y"] + layout["fa_h"] > layout["en_y"]:
        errors.append(
            f"Overlap: Persian bottom={layout['fa_y'] + layout['fa_h']} "
            f"> English top={layout['en_y']}"
        )

    if layout["fa_w"] < 20:
        errors.append(f"Persian text too narrow (fa_w={layout['fa_w']})")
    if layout["en_w"] < 20:
        errors.append(f"English text too narrow (en_w={layout['en_w']})")

    return len(errors) == 0, errors


def _save_qc_error(currency: str, errors: list, toronto_now: datetime):
    QC_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = {"currency": currency, "timestamp": toronto_now.isoformat(), "errors": errors}
    ts    = toronto_now.strftime("%Y%m%d_%H%M%S")
    fname = QC_REPORT_DIR / f"qc_fail_{currency.lower()}_{ts}.json"
    fname.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.error(f"[{currency}] Date QC FAILED → {fname}")
    for e in errors:
        log.error(f"  • {e}")


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


def draw_text_in_box(draw, date_box: list, text: str, font_path: str,
                     max_font: int, min_font: int, color: tuple,
                     is_persian: bool = False,
                     v_center_y: int | None = None) -> int:
    """
    Render text centered horizontally inside date_box, auto-shrinking to fit.
    When is_persian=True, applies arabic_reshaper + bidi before rendering.
    v_center_y pins the vertical center of the text; if None, centers in box.
    Returns the actual font size used.
    """
    x1, y1, x2, y2 = date_box
    box_w = x2 - x1
    box_h = y2 - y1

    render_text = fa(text) if is_persian else text

    font_size = max_font
    font = load_font(font_path, font_size)
    bb = draw.textbbox((0, 0), render_text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]

    while tw > box_w - 8 and font_size > min_font:
        font_size -= 1
        font = load_font(font_path, font_size)
        bb = draw.textbbox((0, 0), render_text, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]

    tx = x1 + (box_w - tw) // 2
    ty = (v_center_y - th // 2) if v_center_y is not None else (y1 + (box_h - th) // 2)

    draw.text((tx + 1, ty + 1), render_text, font=font, fill=(0, 0, 0, 180))
    draw.text((tx,     ty),     render_text, font=font, fill=color)
    return font_size


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

    if currency in ("USACAN", "EUR"):
        sell_str = f"{sell:.2f}" if sell is not None else "---"
        buy_str  = f"{buy:.2f}"  if buy  is not None else "---"
    else:
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

    # ── 2. Date — strictly inside date_box, calendar icon untouched ─────────
    date_box = coords.get("date_box")
    if date_box:
        layout     = _draw_date_in_box(draw, px_rgb, date_box, currency, toronto_now)
        debug_path = _save_date_debug(img, currency, layout)
        log.info(f"[{currency}] Debug preview → {debug_path}")

        qc_ok, qc_errors = _qc_date(currency, toronto_now, layout)
        if not qc_ok:
            _save_qc_error(currency, qc_errors, toronto_now)
            return None

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

    # Tight window for a fully-missing currency; wider window when
    # only one direction is missing (e.g. sell found, buy absent).
    MAX_AGE_FULL_S = 7200        # 2 h — whole currency missing
    MAX_AGE_BUY_S  = 7 * 86400  # 7 d — buy-only gap

    now_ts = datetime.now(timezone.utc).timestamp()

    for cur_code in ("USD", "CAD", "EUR", "USDT"):
        cur_data = collected.get(cur_code, {})
        has_sell = cur_data.get("sell") is not None
        has_buy  = cur_data.get("buy")  is not None

        if has_sell and has_buy:
            continue  # already complete

        entry = cache.get(cur_code, {})
        upd   = entry.get("updated_at", 0)
        try:
            upd_ts = datetime.fromisoformat(upd).timestamp() if isinstance(upd, str) else float(upd)
        except Exception:
            upd_ts = 0

        age_s   = now_ts - upd_ts
        max_age = MAX_AGE_BUY_S if has_sell else MAX_AGE_FULL_S

        if age_s > max_age:
            log.info(f"  [{cur_code}] cache too old ({age_s/3600:.1f}h) — skipping")
            continue

        our_buy  = entry.get("our_buy")  or entry.get("buy")
        our_sell = entry.get("our_sell") or entry.get("price")

        collected.setdefault(cur_code, {})

        if our_buy and not has_buy:
            buy_val = int(our_buy)
            # Sanity: buy must be below sell to avoid inverted prices
            live_sell = collected[cur_code].get("sell")
            if live_sell and buy_val >= live_sell:
                buy_val = live_sell - 2000
                log.warning(f"  [{cur_code}] cache buy capped to {buy_val:,} (was >= live sell)")
            collected[cur_code]["buy"] = buy_val
            log.info(f"  [{cur_code}] cache fallback buy={buy_val:,} (age={age_s/3600:.1f}h)")

        if our_sell and not has_sell:
            collected[cur_code]["sell"] = int(our_sell)
            log.info(f"  [{cur_code}] cache fallback sell={our_sell} (age={age_s/3600:.1f}h)")

    return collected


def _derive_eur_cad_rate(usd_prices: dict, cad_prices: dict) -> dict | None:
    """
    Derive EUR/CAD rate (how many CAD per 1 EUR) from USD/CAD Toman prices
    and a live EUR/USD exchange rate.

    EUR_toman  = USD_toman / (EUR per 1 USD)
    EUR/CAD    = EUR_toman / CAD_toman
    """
    usd_sell = usd_prices.get("sell")
    usd_buy  = usd_prices.get("buy")
    cad_sell = cad_prices.get("sell")
    cad_buy  = cad_prices.get("buy")
    if not usd_sell or not cad_sell:
        return None
    try:
        resp = _requests.get(
            "https://open.er-api.com/v6/latest/USD",
            timeout=8,
        )
        data = resp.json()
        eur_per_usd = data.get("rates", {}).get("EUR")   # e.g. 0.856 (EUR per 1 USD)
        if not eur_per_usd or eur_per_usd <= 0:
            return None
        eur_sell_toman = usd_sell / eur_per_usd
        eur_buy_toman  = (usd_buy / eur_per_usd) if usd_buy else None

        eur_cad_sell = round(eur_sell_toman / cad_sell, 2)
        eur_cad_buy  = (round(eur_buy_toman  / cad_buy, 2)
                        if eur_buy_toman and cad_buy else None)
        log.info(f"  [EUR] EUR/USD={eur_per_usd:.4f}  "
                 f"EUR_toman≈{eur_sell_toman:,.0f}  "
                 f"EUR/CAD sell={eur_cad_sell} buy={eur_cad_buy}")
        result: dict = {"sell": eur_cad_sell}
        if eur_cad_buy is not None:
            result["buy"] = eur_cad_buy
        return result
    except Exception as exc:
        log.warning(f"  [EUR] live rate fetch failed: {exc}")
        return None


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

    # USACAN: USD/CAD rate in CAD (e.g. 1.41), derived from Toman prices
    usd_p = adjusted.get("USD", {})
    cad_p = adjusted.get("CAD", {})
    if usd_p.get("sell") and cad_p.get("sell") and "USACAN" not in adjusted:
        usacan_sell = round(usd_p["sell"] / cad_p["sell"], 2)
        usacan_buy  = (round(usd_p["buy"] / cad_p["buy"], 2)
                       if usd_p.get("buy") and cad_p.get("buy") else None)
        adjusted["USACAN"] = {"sell": usacan_sell, "buy": usacan_buy}
        log.info(f"  [USACAN] derived sell={usacan_sell} buy={usacan_buy} CAD/USD")

    # EUR: derive EUR/CAD rate from USD/CAD Toman prices + live EUR/USD rate
    if not adjusted.get("EUR", {}).get("sell"):
        eur_derived = _derive_eur_cad_rate(adjusted.get("USD", {}),
                                           adjusted.get("CAD", {}))
        if eur_derived:
            adjusted.setdefault("EUR", {}).update(eur_derived)

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
