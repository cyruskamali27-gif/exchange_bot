#!/usr/bin/env python3
"""
rate_graphic_publisher.py — Cyrus Global Exchange Automatic Rate Publisher

Price Rules:
  - Cyrus SELL = Bahmani SELL  (exactly the same, no adjustment)
  - Cyrus BUY  = Bahmani BUY + 500 تومان

Poster order: logo_daily → CAD → USD → USA/CAN → USDT → EUR
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

from smart_text_engine import (
    draw_date_box as _smart_draw_date_box,
    qc_date_fit as _smart_qc_date,
    fit_text_inside_box as _fit_text,
    get_today_date_lines as _get_date_lines,
)

FONT_BOLD_FA = "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"
COLOR_FA     = (242, 222, 148, 255)   # gold — Persian lines
COLOR_EN     = (210, 190, 120, 255)   # slightly dimmer — Gregorian

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

LOGO_TEMPLATE = ASSETS_DIR / "logo.png"

COORDS_FILE    = Path("/var/www/exchange_bot/poster_coordinates.json")
PROFILES_FILE  = Path("/var/www/exchange_bot/template_coordinate_profiles.json")
QC_REPORT_DIR  = Path("/var/www/exchange_bot/qc_reports")

def _load_coords() -> dict:
    try:
        return json.loads(COORDS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Cannot load {COORDS_FILE}: {exc}")

def _load_profiles() -> dict:
    try:
        return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Cannot load {PROFILES_FILE}: {exc}")

POSTER_COORDS: dict   = _load_coords()
DATE_PROFILES: dict   = _load_profiles()

# logo_daily publishes first; currency posters follow in this order
PUBLISH_ORDER = [
    ("CAD",    "دلار کانادا",  "Canada"),
    ("USD",    "دلار آمریکا", "USA"),
    ("USACAN", "دلار آمریکا / کانادا", "USA/CAN"),
    ("USDT",   "تتر (USDT)",   "USDT"),
    ("EUR",    "یورو",         "Europe"),
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


# ─── Date rendering — delegated to smart_text_engine ─────────────────────────
# _draw_date_in_box and _qc_date replaced by smart_text_engine.
# smart_text_engine.draw_date_box:
#   - auto-sizes font to fill the box cleanly
#   - 3 lines: Shamsi numeric / Persian weekday / Gregorian
#   - proper RTL reshaping (arabic_reshaper + python-bidi)
#   - centers text block horizontally and vertically inside the box
#   - never guesses x/y coordinates
# smart_text_engine.qc_date_fit: validates all 3 lines, overlap, boundary.


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

def _erase_price_digits(draw: ImageDraw.ImageDraw, px_rgb, box: list) -> tuple:
    """
    Erase old placeholder price digits from the template.
    Samples the darkest (background) pixels in the box and fills the inner
    text strip with that colour — preserving row borders and edge glow.
    Returns the sampled background colour as (r, g, b).
    """
    x1, y1, x2, y2 = [int(v) for v in box]
    pad_v = 3   # minimal inset — preserves row border/glow, clears all digit pixels
    inner_y1 = y1 + pad_v
    inner_y2 = y2 - pad_v
    if inner_y2 <= inner_y1:
        inner_y1, inner_y2 = y1, y2

    samples = []
    for sy in range(y1, y2, 3):
        for sx in range(x1, x2, 8):
            try:
                r, g, b = px_rgb[sx, sy]
                samples.append((r + g + b, r, g, b))
            except Exception:
                pass
    if not samples:
        return (10, 10, 20)
    samples.sort()
    dark = samples[: max(1, len(samples) // 3)]
    bg = (
        sum(s[1] for s in dark) // len(dark),
        sum(s[2] for s in dark) // len(dark),
        sum(s[3] for s in dark) // len(dark),
    )
    draw.rectangle([x1 + 3, inner_y1, x2 - 3, inner_y2], fill=(*bg, 255))
    return bg


def _write_in_box(draw, px_rgb, box: list, text: str, font_path: str,
                  text_color: tuple, max_font: int = 80, min_font: int = 14):
    """
    Erase old template digits, then center-write new text inside box.
    Preserves original poster texture — no large black rectangles.
    """
    x1, y1, x2, y2 = box
    zone_w = x2 - x1
    zone_h = y2 - y1

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

    # Erase old template digits (tight inner strip, background-coloured)
    bg = _erase_price_digits(draw, px_rgb, box)

    # Subtle 1-px shadow + clean main text — no dark rectangle effect
    draw.text((tx + 1, ty + 1), text, font=font, fill=(0, 0, 0, 90))
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

    # ── 2. Date — compact header strip from per-template profile ───────────
    prof = DATE_PROFILES.get(currency, {})
    date_strip = prof.get("date_strip") or coords.get("date_overlay")
    if date_strip:
        date_data = _get_date_lines(toronto_now)
        _fit_text(
            img,
            date_strip,
            date_data["lines"],
            date_data["font_paths"],
            date_data["colors"],
            align="center",
            vertical_align="center",
            min_font_size=prof.get("min_font", 10),
            max_font_size=prof.get("max_font", 22),
            padding_x=prof.get("padding_x", 14),
            padding_y=prof.get("padding_y", 5),
            line_spacing=prof.get("line_spacing", 3),
            rtl_lines=date_data["rtl_lines"],
            shadow=True,
            border_color=None,
            border_width=0,
            clean_bg=prof.get("clean_bg", True),
        )
        log.info(f"[{currency}] Date strip rendered in {date_strip}")

    # ── 3. Save ────────────────────────────────────────────────────────────
    ts_str   = toronto_now.strftime("%Y-%m-%d-%H%M")
    out_path = GENERATED_DIR / f"{ts_str}-{currency.lower()}.png"
    img.convert("RGB").save(str(out_path), "PNG")
    log.info(f"[{currency}] Saved → {out_path}")
    return out_path


# ─── Logo daily poster ────────────────────────────────────────────────────────

def generate_logo_daily(toronto_now: datetime) -> Path | None:
    """
    Generate the daily logo poster.
    Writes the actual dates into the logo template's built-in date cells.
    No dark gradient overlay, no black patch, no rectangles.
    Published as the very first image in every posting cycle.
    """
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    if not LOGO_TEMPLATE.exists():
        log.error(f"[logo_daily] Logo template missing: {LOGO_TEMPLATE}")
        return None

    try:
        img = Image.open(LOGO_TEMPLATE).convert("RGBA")
        w, h = img.size   # 1254×1254

        date_data = _get_date_lines(toronto_now)

        # ── Logo template has two built-in date cells (gold-bordered dark boxes)
        # Row 1 right cell: "YYYY/MM/DD" placeholder → write Persian date
        # Row 2 right cell: "DD/MM/YYYY" placeholder → write Gregorian date
        # Cell positions measured from the 1254×1254 template via OCR.
        shamsi_cell    = [637, 948, 960, 1058]   # right cell of Shamsi row
        gregorian_cell = [637, 1058, 960, 1168]  # right cell of Gregorian row

        # Shamsi row: weekday on top line, numeric Shamsi on second line.
        # Shamsi uses DejaVu (NotoNaskhArabic lacks '/') with rtl_lines=False
        # since Persian date digits read left-to-right.
        _fit_text(
            img,
            shamsi_cell,
            [date_data["weekday"], date_data["shamsi"]],
            [FONT_BOLD_FA, FONT_BOLD_EN],
            [COLOR_FA, COLOR_FA],
            align="center",
            vertical_align="center",
            min_font_size=14,
            max_font_size=44,
            padding_x=12,
            padding_y=8,
            line_spacing=4,
            rtl_lines=[True, False],
            shadow=True,
            border_color=None,
            border_width=0,
            clean_bg=True,    # erase "YYYY/MM/DD" placeholder first
        )

        # Gregorian row: single line, English
        _fit_text(
            img,
            gregorian_cell,
            [date_data["gregorian"]],
            [FONT_BOLD_EN],
            [COLOR_EN],
            align="center",
            vertical_align="center",
            min_font_size=14,
            max_font_size=44,
            padding_x=12,
            padding_y=10,
            line_spacing=0,
            rtl_lines=[False],
            shadow=True,
            border_color=None,
            border_width=0,
            clean_bg=True,    # erase "DD/MM/YYYY" placeholder first
        )

        log.info(
            f"[logo_daily] Date → '{date_data['weekday']}' / "
            f"'{date_data['shamsi']}' / '{date_data['gregorian']}'"
        )

        ts_str   = toronto_now.strftime("%Y-%m-%d-%H%M")
        out_path = GENERATED_DIR / f"{ts_str}-logo_daily.png"
        img.convert("RGB").save(str(out_path), "PNG")
        log.info(f"[logo_daily] Saved → {out_path}")
        return out_path

    except Exception as exc:
        log.error(f"[logo_daily] Generation failed: {exc}", exc_info=True)
        return None


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
    log.info(f"Generating logo_daily + {len(PUBLISH_ORDER)} posters ({reason})")

    posted_ids = []
    try:
        # ── 0. Logo daily — always first ─────────────────────────────────────
        logo_path = generate_logo_daily(toronto_now)
        if logo_path:
            logo_mid = post_to_channel(logo_path)
            posted_ids.append(("logo_daily", logo_mid))
            await asyncio.sleep(2)
        else:
            log.warning("[logo_daily] Generation failed — skipping logo post")

        # ── 1-5. Currency rate posters ────────────────────────────────────────
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
            log.info(f"=== Posted {len(posted_ids)} image(s) ===")
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
