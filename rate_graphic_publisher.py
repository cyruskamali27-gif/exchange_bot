#!/usr/bin/env python3
"""
rate_graphic_publisher.py — Cyrus Global Exchange Automatic Rate Publisher

Workflow:
  1. Read @SarafiBahmaniCa (Telethon user client) — text + OCR images
  2. Adjust rates: BUY +500, SELL −600 (configurable via .env)
  3. Generate 4 branded Persian posters (one per currency), in order:
       CAD → USD → EUR → USDT
  4. On first post of the day: publish a Persian/Gregorian date header graphic first
  5. Post all posters to @cyrusGlobalExchange
  6. Schedule: 9 AM Toronto daily + hourly change check
  7. Trigger: auto-post whenever Bahmani posts a new rate update

Template files (optional — place in assets/posters/ to override generated backgrounds):
  assets/posters/cad_template.png
  assets/posters/usd_template.png
  assets/posters/eur_template.png
  assets/posters/usdt_template.png
"""

import asyncio
import io
import json
import logging
import math
import os
import random
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
    logging.warning("arabic-reshaper / python-bidi not installed — text may render poorly")

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
RATES_FILE     = Path("/var/www/exchange_bot/latest_rates.json")
GENERATED_DIR  = Path("/var/www/exchange_bot/generated/rates")
ASSETS_DIR     = Path("/var/www/exchange_bot/assets/posters")
SESSION        = "rate_publisher"
TORONTO_TZ     = ZoneInfo("America/Toronto")
POSTER_W       = 1080
POSTER_H       = 1350

# Adjustment rule: BUY +500, SELL −600
BUY_ADJ  = int(os.environ.get("PUBLISHER_BUY_ADJ",  "500"))
SELL_ADJ = int(os.environ.get("PUBLISHER_SELL_ADJ", "600"))

FONT_BOLD       = "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"
FONT_REGULAR    = "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"
FONT_BOLD_EN    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Posting order — fixed
PUBLISH_ORDER = [
    ("CAD",  "دلار کانادا",  "Canada"),
    ("USD",  "دلار آمریکا", "USA"),
    ("USDT", "تتر (USDT)",   "USDT"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rate_publisher")


# ─── Persian Text Helpers ──────────────────────────────────────────────────────

def fa(text: str) -> str:
    if not text or not HAS_BIDI:
        return text
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def fa_num(n: int) -> str:
    return f"{n:,}"


def persian_date(dt: datetime) -> str:
    if HAS_JDATE:
        jd = jdatetime.datetime.fromgregorian(datetime=dt)
        months = [
            "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
            "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
        ]
        return f"{jd.day} {months[jd.month - 1]} {jd.year}"
    return dt.strftime("%Y-%m-%d")


# ─── Font Loader ───────────────────────────────────────────────────────────────

def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        log.warning(f"Font unavailable: {path} — using default")
        return ImageFont.load_default()


# ─── Seasonal Background ───────────────────────────────────────────────────────

def _get_season(dt: datetime) -> str:
    m, d = dt.month, dt.day
    if m == 12 or m <= 2 or (m == 3 and d < 21):
        return "winter"
    if (m == 3 and d >= 21) or m <= 5 or (m == 6 and d < 21):
        return "spring"
    if (m == 6 and d >= 21) or m <= 8 or (m == 9 and d < 23):
        return "summer"
    return "fall"


def _special_day(dt: datetime) -> str | None:
    key = (dt.month, dt.day)
    return {
        (1, 1):   "newyear",
        (2, 14):  "valentine",
        (3, 20):  "nowruz_eve",
        (3, 21):  "nowruz",
        (7, 1):   "canada_day",
        (10, 31): "halloween",
        (12, 25): "christmas",
        (12, 24): "christmas",
    }.get(key)


def _make_gradient(w: int, h: int, c1: tuple, c2: tuple) -> Image.Image:
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for i in range(h):
        t = i / h
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        draw.line([(0, i), (w, i)], fill=(r, g, b))
    return img


def _draw_snowflakes(draw: ImageDraw.ImageDraw, w: int, h: int):
    random.seed(42)
    for _ in range(24):
        x = random.randint(0, w)
        y = random.randint(0, h)
        r = random.randint(8, 30)
        alpha = random.randint(25, 70)
        for angle in range(0, 360, 60):
            rad = math.radians(angle)
            x2 = int(x + r * math.cos(rad))
            y2 = int(y + r * math.sin(rad))
            draw.line([(x, y), (x2, y2)], fill=(255, 255, 255, alpha), width=2)
        draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 255, 255, alpha))


def _draw_petals(draw: ImageDraw.ImageDraw, w: int, h: int):
    random.seed(7)
    colors = [(255, 180, 200, 55), (200, 255, 180, 55), (255, 240, 150, 55)]
    for _ in range(18):
        x = random.randint(0, w)
        y = random.randint(0, h)
        r = random.randint(12, 45)
        col = random.choice(colors)
        for angle in range(0, 360, 72):
            rad = math.radians(angle)
            px = int(x + r * math.cos(rad))
            py = int(y + r * math.sin(rad))
            half = r // 2
            draw.ellipse([px - half, py - half, px + half, py + half], fill=col)
        draw.ellipse([x - r // 3, y - r // 3, x + r // 3, y + r // 3],
                     fill=(255, 255, 120, 80))


def _draw_sun(draw: ImageDraw.ImageDraw, w: int, h: int):
    cx, cy = w // 2, h // 7
    random.seed(99)
    for angle in range(0, 360, 12):
        rad = math.radians(angle)
        length = random.randint(50, 110)
        x2 = int(cx + length * math.cos(rad))
        y2 = int(cy + length * math.sin(rad))
        draw.line([(cx, cy), (x2, y2)], fill=(255, 240, 80, 45), width=3)
    draw.ellipse([cx - 45, cy - 45, cx + 45, cy + 45], fill=(255, 215, 40, 130))


def _draw_leaves(draw: ImageDraw.ImageDraw, w: int, h: int):
    random.seed(13)
    colors = [(220, 80, 20, 65), (180, 120, 10, 65), (200, 40, 10, 55)]
    for _ in range(20):
        x = random.randint(-20, w + 20)
        y = random.randint(0, h)
        rw = random.randint(12, 38)
        rh = random.randint(6, 18)
        rot = math.radians(random.randint(-50, 50))
        col = random.choice(colors)
        pts = []
        for a in range(0, 360, 18):
            ra = math.radians(a)
            px = rw * math.cos(ra)
            py = rh * math.sin(ra)
            rx = px * math.cos(rot) - py * math.sin(rot) + x
            ry = px * math.sin(rot) + py * math.cos(rot) + y
            pts.append((rx, ry))
        draw.polygon(pts, fill=col)


_SEASON_PALETTES = {
    "winter":  ((8,  22,  65),  (65, 120, 195)),
    "spring":  ((20, 110, 55),  (160, 225, 140)),
    "summer":  ((195, 115, 8),  (255, 195, 75)),
    "fall":    ((115, 38,  8),  (225, 135, 55)),
}

_SPECIAL_PALETTES = {
    "christmas":  ((8,  55, 18),  (175, 8,   8)),
    "nowruz":     ((160, 235, 145), (255, 195, 45)),
    "nowruz_eve": ((140, 210, 125), (240, 175, 35)),
    "canada_day": ((175, 0,   0),  (255, 255, 255)),
    "valentine":  ((180, 10,  60),  (255, 150, 180)),
    "halloween":  ((90,  40,  0),  (220, 100, 0)),
    "newyear":    ((10,  10,  60),  (120, 0,  180)),
}

# Per-currency accent colours for single-poster layout
_CURRENCY_ACCENT = {
    "CAD":  (220, 50,  50),   # red — maple leaf
    "USD":  (50,  130, 230),  # blue — USA
    "EUR":  (60,  120, 220),  # EU blue
    "USDT": (40,  190, 130),  # teal — crypto
}


def _load_template(currency: str, w: int, h: int) -> Image.Image | None:
    path = ASSETS_DIR / f"{currency.lower()}_template.png"
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGBA")
        if img.size != (w, h):
            img = img.resize((w, h), Image.LANCZOS)
        log.info(f"🖼️  Loaded template: {path}")
        return img
    except Exception as exc:
        log.warning(f"Could not load template {path}: {exc}")
        return None


def generate_background(season: str, special: str | None,
                        currency: str = "") -> Image.Image:
    if special and special in _SPECIAL_PALETTES:
        c1, c2 = _SPECIAL_PALETTES[special]
    else:
        c1, c2 = _SEASON_PALETTES.get(season, _SEASON_PALETTES["winter"])

    # Blend in a subtle currency accent at the bottom
    accent = _CURRENCY_ACCENT.get(currency)
    if accent:
        c2 = tuple(
            min(255, int(c2[i] * 0.65 + accent[i] * 0.35)) for i in range(3)
        )

    bg = _make_gradient(POSTER_W, POSTER_H, c1, c2)
    bg_rgba = bg.convert("RGBA")
    deco = Image.new("RGBA", (POSTER_W, POSTER_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(deco)

    if season == "winter":
        _draw_snowflakes(draw, POSTER_W, POSTER_H)
    elif season == "spring":
        _draw_petals(draw, POSTER_W, POSTER_H)
    elif season == "summer":
        _draw_sun(draw, POSTER_W, POSTER_H)
    elif season == "fall":
        _draw_leaves(draw, POSTER_W, POSTER_H)

    bg_rgba = Image.alpha_composite(bg_rgba, deco)

    vignette = Image.new("RGBA", (POSTER_W, POSTER_H), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vignette)
    for i in range(80):
        alpha = int(160 * (1 - i / 80))
        vd.rectangle([i, i, POSTER_W - i, POSTER_H - i], outline=(0, 0, 0, alpha))
    bg_rgba = Image.alpha_composite(bg_rgba, vignette)
    return bg_rgba.convert("RGB")


# ─── Colour constants ─────────────────────────────────────────────────────────

GOLD   = (210, 175, 85)
WHITE  = (255, 255, 255)
LGRAY  = (190, 190, 190)
GREEN  = (100, 240, 145)
ORANGE = (255, 155, 90)
BLUE   = (100, 185, 255)
BLACK  = (0, 0, 0)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _center_text(draw: ImageDraw.ImageDraw, text: str, y: int, font, fill,
                 shadow: bool = True, width: int = POSTER_W):
    w = _text_width(draw, text, font)
    x = (width - w) // 2
    if shadow:
        draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 120))
    draw.text((x, y), text, font=font, fill=fill)


def _right_text(draw: ImageDraw.ImageDraw, text: str, y: int, right_x: int,
                font, fill):
    w = _text_width(draw, text, font)
    draw.text((right_x - w, y), text, font=font, fill=fill)


# ─── Date Header Poster ────────────────────────────────────────────────────────

HEADER_H = 420   # shorter than a full poster

def generate_date_header(toronto_now: datetime) -> Path:
    """Generates a slim Persian/Gregorian date banner for the first daily post."""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    season  = _get_season(toronto_now)
    special = _special_day(toronto_now)

    bg = generate_background(season, special)
    bg = bg.crop((0, 0, POSTER_W, HEADER_H))
    img = bg.convert("RGBA")

    # Semi-transparent panel
    overlay = Image.new("RGBA", (POSTER_W, HEADER_H), (0, 0, 0, 0))
    ov = ImageDraw.Draw(overlay)
    ov.rounded_rectangle(
        [30, 20, POSTER_W - 30, HEADER_H - 20],
        radius=22, fill=(0, 0, 0, 185), outline=(*GOLD, 200), width=3,
    )
    img = Image.alpha_composite(img, overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    pad = 60
    right_x = POSTER_W - pad
    y = 42

    f_en_title  = load_font(FONT_BOLD_EN,  36)
    f_fa_title  = load_font(FONT_BOLD,     46)
    f_date_fa   = load_font(FONT_BOLD,     52)
    f_date_en   = load_font(FONT_BOLD_EN,  34)
    f_sub       = load_font(FONT_REGULAR,  26)
    f_en_handle = load_font(FONT_REGULAR_EN, 20)

    # Gold top rule
    draw.line([(pad, y - 4), (right_x, y - 4)], fill=GOLD, width=2)

    _center_text(draw, "CYRUS GLOBAL EXCHANGE", y, f_en_title, GOLD, width=POSTER_W)
    y += 48
    _center_text(draw, fa("صرافی سیروس"), y, f_fa_title, WHITE, width=POSTER_W)
    y += 62

    draw.line([(pad, y), (right_x, y)], fill=GOLD, width=1)
    y += 20

    # Shamsi date
    pdate_str = fa(persian_date(toronto_now))
    _center_text(draw, pdate_str, y, f_date_fa, GOLD, width=POSTER_W)
    y += 66

    # Gregorian date
    gdate_str = toronto_now.strftime("%A, %d %B %Y")
    _center_text(draw, gdate_str, y, f_date_en, LGRAY, width=POSTER_W)
    y += 50

    _center_text(draw, fa("نرخ روز صرافی سیروس"), y, f_sub, (160, 200, 255),
                 shadow=False, width=POSTER_W)
    y += 38

    draw.line([(pad, y), (right_x, y)], fill=(80, 80, 80), width=1)
    y += 10
    _center_text(draw, "@cyrusGlobalExchange", y, f_en_handle, BLUE,
                 shadow=False, width=POSTER_W)

    fname = toronto_now.strftime("%Y-%m-%d-date-header.png")
    out_path = GENERATED_DIR / fname
    img.save(str(out_path), "PNG", quality=95)
    log.info(f"🗓️  Date header saved: {out_path}")
    return out_path


# ─── Single-Currency Poster ────────────────────────────────────────────────────

def generate_single_poster(currency: str, cur_name_fa: str,
                           buy: int | None, sell: int | None,
                           toronto_now: datetime) -> Path:
    """Generate one poster for a single currency."""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    season  = _get_season(toronto_now)
    special = _special_day(toronto_now)

    # Use uploaded template if present, else generate background
    template = _load_template(currency, POSTER_W, POSTER_H)
    if template:
        img = template.convert("RGBA")
    else:
        img = generate_background(season, special, currency).convert("RGBA")

    # Semi-transparent content card
    card_margin = 42
    card_rx1, card_ry1 = card_margin, 90
    card_rx2, card_ry2 = POSTER_W - card_margin, POSTER_H - 90
    card_w = card_rx2 - card_rx1

    accent = _CURRENCY_ACCENT.get(currency, GOLD)

    overlay = Image.new("RGBA", (POSTER_W, POSTER_H), (0, 0, 0, 0))
    ov = ImageDraw.Draw(overlay)
    ov.rounded_rectangle(
        [card_rx1, card_ry1, card_rx2, card_ry2],
        radius=24, fill=(0, 0, 0, 200), outline=(*GOLD, 220), width=3,
    )
    ov.rounded_rectangle(
        [card_rx1 + 8, card_ry1 + 8, card_rx2 - 8, card_ry2 - 8],
        radius=18, outline=(*GOLD, 65), width=1,
    )
    img = Image.alpha_composite(img, overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    pad_x  = card_rx1 + 44
    right_x = card_rx2 - 44
    y = card_ry1 + 48

    f_en_title  = load_font(FONT_BOLD_EN,  46)
    f_fa_title  = load_font(FONT_BOLD,     58)
    f_cur_name  = load_font(FONT_BOLD,     70)
    f_cur_code  = load_font(FONT_BOLD_EN,  38)
    f_label     = load_font(FONT_BOLD,     34)
    f_price     = load_font(FONT_BOLD_EN,  96)
    f_date      = load_font(FONT_REGULAR,  28)
    f_date_en   = load_font(FONT_REGULAR_EN, 26)
    f_small     = load_font(FONT_REGULAR,  24)
    f_tiny      = load_font(FONT_REGULAR,  20)
    f_en_handle = load_font(FONT_REGULAR_EN, 22)
    f_time_en   = load_font(FONT_BOLD_EN,  24)

    # Gold top rule
    draw.line([(pad_x, y - 10), (right_x, y - 10)], fill=GOLD, width=2)

    # English title
    _center_text(draw, "CYRUS GLOBAL EXCHANGE", y, f_en_title, GOLD)
    y += 58

    # Persian title
    _center_text(draw, fa("صرافی سیروس"), y, f_fa_title, WHITE)
    y += 72

    draw.line([(pad_x, y), (right_x, y)], fill=GOLD, width=1)
    y += 28

    # ── Currency section ──────────────────────────────────────────
    # Currency name in Persian (large)
    _center_text(draw, fa(cur_name_fa), y, f_cur_name, WHITE)
    y += 84

    # Currency code badge
    code_w = _text_width(draw, currency, f_cur_code)
    badge_x = (POSTER_W - code_w) // 2
    draw.rounded_rectangle(
        [badge_x - 18, y - 4, badge_x + code_w + 18, y + 46],
        radius=10, fill=(*accent, 80), outline=(*accent, 200), width=2,
    )
    draw.text((badge_x, y), currency, font=f_cur_code, fill=(*accent, 255))
    y += 64

    draw.line([(pad_x, y), (right_x, y)], fill=(80, 80, 80), width=1)
    y += 30

    # ── Date & time ───────────────────────────────────────────────
    pdate_str = fa(persian_date(toronto_now))
    gdate_str = toronto_now.strftime("%d %B %Y")
    pw = _text_width(draw, pdate_str, f_date)
    gw = _text_width(draw, gdate_str, f_date_en)
    sep_w = _text_width(draw, "|", f_date_en)
    total_w = pw + sep_w + gw + 24
    start_x = (POSTER_W - total_w) // 2
    draw.text((start_x, y), pdate_str, font=f_date, fill=LGRAY)
    draw.text((start_x + pw + 12, y + 2), "|", font=f_date_en, fill=(100, 100, 100))
    draw.text((start_x + pw + sep_w + 24, y + 2), gdate_str, font=f_date_en, fill=LGRAY)
    y += 44

    upd_label = fa("آخرین بروزرسانی: ")
    upd_time  = toronto_now.strftime("%H:%M")
    lw = _text_width(draw, upd_label, f_small)
    tw = _text_width(draw, upd_time, f_time_en)
    ux = (POSTER_W - lw - tw - 4) // 2
    draw.text((ux, y), upd_label, font=f_small, fill=(160, 200, 255))
    draw.text((ux + lw + 4, y + 1), upd_time, font=f_time_en, fill=BLUE)
    y += 52

    draw.line([(pad_x, y), (right_x, y)], fill=(80, 80, 80), width=1)
    y += 40

    # ── Buy / Sell prices ─────────────────────────────────────────
    mid_x = POSTER_W // 2

    if buy or sell:
        # BUY block (left half)
        buy_str = fa_num(buy) if buy else "---"
        draw.text((pad_x, y), fa("خرید"), font=f_label, fill=GREEN)
        by = y + 44
        draw.text((pad_x, by), buy_str, font=f_price, fill=WHITE)
        y_after_buy = by + 110

        # SELL block (right half)
        sell_str = fa_num(sell) if sell else "---"
        _right_text(draw, fa("فروش"), y, right_x, f_label, ORANGE)
        _right_text(draw, sell_str, by, right_x, f_price, WHITE)

        # Vertical separator between buy/sell
        draw.line([(mid_x, y - 6), (mid_x, y_after_buy)],
                  fill=(90, 90, 90), width=1)
        y = y_after_buy + 30
    else:
        unavail = fa("در دسترس نیست")
        _center_text(draw, unavail, y + 30, f_label, (100, 100, 100), shadow=False)
        y += 130

    # ── Bottom divider & disclaimer ───────────────────────────────
    draw.line([(pad_x, y), (right_x, y)], fill=(90, 90, 90), width=1)
    y += 20
    disc = fa("نرخ‌ها لحظه‌ای هستند و قبل از معامله باید تأیید شوند.")
    _center_text(draw, disc, y, f_tiny, LGRAY, shadow=False)
    y += 34
    _center_text(draw, "@cyrusGlobalExchange", y, f_en_handle, BLUE, shadow=False)

    # Bottom gold rule
    draw.line([(pad_x, card_ry2 - 20), (right_x, card_ry2 - 20)], fill=GOLD, width=2)

    # Save
    ts_str = toronto_now.strftime("%Y-%m-%d-%H%M")
    out_path = GENERATED_DIR / f"{ts_str}-{currency.lower()}.png"
    img.save(str(out_path), "PNG", quality=95)
    log.info(f"🖼️  Poster saved: {out_path}")
    return out_path


# ─── Caption Builder ──────────────────────────────────────────────────────────

def build_caption(currency: str, cur_name_fa: str, buy: int | None,
                  sell: int | None, toronto_now: datetime) -> str:
    flag = {"CAD": "🇨🇦", "USD": "🇺🇸", "EUR": "🇪🇺", "USDT": "💲"}.get(currency, "💱")
    b = f"{buy:,}"  if buy  else "---"
    s = f"{sell:,}" if sell else "---"
    lines = [
        f"{flag} نرخ {cur_name_fa} — صرافی سیروس",
        f"🕘 آخرین بروزرسانی: {toronto_now.strftime('%H:%M')}",
        "",
        f"📥 خرید: {b}",
        f"📤 فروش: {s}",
        "",
        "نرخ‌ها لحظه‌ای هستند و قبل از معامله باید تأیید شوند.",
        "@cyrusGlobalExchange",
    ]
    return "\n".join(lines)


# ─── Rate Extraction ──────────────────────────────────────────────────────────

_CUR_KEYWORDS: dict[str, list[str]] = {
    # Order matters: CAD before USD (both contain "دلار"); EUR before USD
    "CAD":  ["دلار کانادا", "کانادا", "کاناد", "CAD", "canada"],
    "EUR":  ["یورو", "اروپا", "EUR", "euro", "eur"],
    "USDT": ["تتر", "USDT", "usdt", "tether", "تتهر", "تدر"],
    "USD":  ["دلار آمریکا", "دلار امریکا", "امریکا", "USD", "dollar", "دلار"],
}
_BUY_KWS  = ["خرید", "buy", "خریدار", "ما می‌خریم"]
_SELL_KWS = ["فروش", "sell", "حواله", "ترنسفر", "transfer", "صادر"]


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

        ctx = text[max(0, m.start() - 200): min(len(text), m.end() + 200)]
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

        if currency not in rates:
            rates[currency] = {}

        if direction == "buy" and "buy" not in rates[currency]:
            rates[currency]["buy"] = val
            log.info(f"  🎯 {currency} خرید (Bahmani) = {val:,}")
        elif direction == "sell" and "sell" not in rates[currency]:
            rates[currency]["sell"] = val
            log.info(f"  🎯 {currency} فروش (Bahmani) = {val:,}")
        elif direction is None and "sell" not in rates[currency]:
            rates[currency]["sell"] = val
            log.info(f"  🎯 {currency} (undirected→sell, Bahmani) = {val:,}")

    return rates


async def fetch_bahmani_rates(client: TelegramClient) -> dict | None:
    log.info(f"📡 Scanning @{SOURCE_CHANNEL}...")
    try:
        messages = await client.get_messages(SOURCE_CHANNEL, limit=15)
    except Exception as exc:
        log.error(f"❌ Could not fetch from @{SOURCE_CHANNEL}: {exc}")
        return None

    collected: dict[str, dict] = {}

    for msg in messages:
        combined = ""

        if getattr(msg, "photo", None):
            try:
                img_bytes = await msg.download_media(bytes)
                img = Image.open(io.BytesIO(img_bytes))
                combined = (
                    pytesseract.image_to_string(img, lang="fas") + "\n"
                    + pytesseract.image_to_string(img, lang="eng")
                )
                log.info(f"🔍 OCR message {msg.id}")
            except Exception as exc:
                log.error(f"❌ OCR error message {msg.id}: {exc}")

        text = getattr(msg, "text", None) or getattr(msg, "message", None) or ""
        if text.strip():
            combined += "\n" + text

        if not combined.strip():
            continue

        found = _extract_bahmani_rates(combined)
        for cur, prices in found.items():
            if cur not in collected:
                collected[cur] = {}
            for direction, price in prices.items():
                if direction not in collected[cur]:
                    collected[cur][direction] = price

    if not collected:
        log.warning(f"⚠️  No rates extracted from @{SOURCE_CHANNEL}")
        return None

    log.info(f"📊 Bahmani source rates: {collected}")
    return collected


def _fallback_from_cache(collected: dict) -> dict:
    try:
        cache = json.loads(Path("/var/www/exchange_bot/current_price.json").read_text())
    except Exception:
        return collected

    MAX_AGE_S = 7200
    now_ts = datetime.now(timezone.utc).timestamp()

    for cur_code in ("USD", "CAD", "EUR", "USDT"):
        if cur_code in collected and collected[cur_code]:
            continue

        entry = cache.get(cur_code, {})
        upd = entry.get("updated_at", 0)
        if isinstance(upd, str):
            try:
                upd_ts = datetime.fromisoformat(upd).timestamp()
            except Exception:
                upd_ts = 0
        else:
            upd_ts = float(upd)

        if now_ts - upd_ts > MAX_AGE_S:
            log.info(f"  ⚠️  {cur_code} cache too old — skipping fallback")
            continue

        our_buy  = entry.get("our_buy")  or entry.get("buy")
        our_sell = entry.get("our_sell") or entry.get("price")

        if our_buy or our_sell:
            collected.setdefault(cur_code, {})
            if our_buy  and "buy"  not in collected[cur_code]:
                collected[cur_code]["buy"]  = int(our_buy)
            if our_sell and "sell" not in collected[cur_code]:
                collected[cur_code]["sell"] = int(our_sell)
            log.info(f"  📥 {cur_code} fallback from cache: buy={our_buy} sell={our_sell}")

    return collected


def adjust_rates(source: dict) -> dict:
    result = {}
    for cur, prices in source.items():
        result[cur] = {}
        if prices.get("buy"):
            result[cur]["buy"]  = prices["buy"]  + BUY_ADJ
        if prices.get("sell"):
            result[cur]["sell"] = prices["sell"] - SELL_ADJ
    log.info(f"📊 Adjusted rates: {result}")
    return result


# ─── State Management ─────────────────────────────────────────────────────────

def load_stored_rates() -> dict:
    try:
        if RATES_FILE.exists():
            return json.loads(RATES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_rates(rates: dict, now: datetime):
    data = {"rates": rates, "updated_at": now.isoformat()}
    RATES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"💾 Rates saved → {RATES_FILE}")


def rates_changed(new: dict, stored: dict) -> bool:
    old = stored.get("rates", {})
    for cur, prices in new.items():
        for direction, price in prices.items():
            if old.get(cur, {}).get(direction) != price:
                return True
    return False


# ─── Posting ──────────────────────────────────────────────────────────────────

def post_to_channel(image_path: Path):
    log.info(f"📤 Posting to {TARGET_CHANNEL} via Bot API...")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
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
            log.info(f"✅ Posted to {TARGET_CHANNEL} (message_id={msg_id})")
        else:
            err = data.get("description", "unknown error")
            log.error(f"❌ Bot API error: {err}")
            raise RuntimeError(err)
    except Exception as exc:
        log.error(f"❌ Post failed: {exc}")
        raise


# ─── Main Scan-and-Post Logic ─────────────────────────────────────────────────

async def scan_and_post(client: TelegramClient, force: bool = False,
                        post_date_header: bool = False):
    """
    Fetch rates, generate 4 separate posters in CAD→USD→EUR→USDT order,
    optionally prefix with a date header graphic.
    """
    toronto_now = datetime.now(TORONTO_TZ)

    source_rates = await fetch_bahmani_rates(client)
    if not source_rates:
        source_rates = {}
        log.warning("⚠️  Bahmani scan empty — will try cache fallback only")

    adjusted = adjust_rates(source_rates)
    adjusted = _fallback_from_cache(adjusted)

    if not adjusted:
        log.warning("⚠️  No rates from any source — skipping post")
        return

    stored  = load_stored_rates()
    changed = rates_changed(adjusted, stored)

    if not force and not changed:
        log.info("⏭️  Rates unchanged — no post needed")
        return

    reason = "daily 9 AM" if force else "rates changed"
    log.info(f"🎨 Generating posters ({reason})...")

    try:
        # Date header — only on first daily post
        if post_date_header:
            header_path = generate_date_header(toronto_now)
            post_to_channel(header_path)
            await asyncio.sleep(2)

        # 4 individual currency posters in fixed order
        for cur_code, cur_name_fa, _ in PUBLISH_ORDER:
            prices = adjusted.get(cur_code, {})
            buy    = prices.get("buy")
            sell   = prices.get("sell")

            poster_path = generate_single_poster(
                cur_code, cur_name_fa, buy, sell, toronto_now
            )
            post_to_channel(poster_path)
            await asyncio.sleep(2)   # small gap between posts

        save_rates(adjusted, toronto_now)
        log.info("✅ All 4 posters posted successfully")
    except Exception as exc:
        log.error(f"❌ Scan-and-post failed: {exc}")


# ─── Scheduler ────────────────────────────────────────────────────────────────

async def run_publisher():
    log.info("🚀 Rate Graphic Publisher starting...")
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION, int(TELETHON_API_ID), TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)
    log.info("✅ Telethon connected")

    last_daily_date   = None   # date of last forced 9 AM post
    last_date_hdr_date = None  # date we last posted the date header
    last_checked_hour = -1

    # ── React to new Bahmani messages in real time ────────────────
    @client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def on_new_bahmani_message(event):
        log.info("📨 New Bahmani message — triggering immediate rate check")
        nonlocal last_date_hdr_date
        now  = datetime.now(TORONTO_TZ)
        post_hdr = (last_date_hdr_date != now.date())
        if post_hdr:
            last_date_hdr_date = now.date()
        await scan_and_post(client, force=False, post_date_header=post_hdr)

    while True:
        try:
            now   = datetime.now(TORONTO_TZ)
            today = now.date()
            hour  = now.hour

            # 9 AM daily forced post
            if hour == 9 and last_daily_date != today:
                log.info("⏰ 9 AM daily post triggered")
                post_hdr = (last_date_hdr_date != today)
                if post_hdr:
                    last_date_hdr_date = today
                await scan_and_post(client, force=True, post_date_header=post_hdr)
                last_daily_date   = today
                last_checked_hour = hour

            # Hourly change check
            elif hour != last_checked_hour and now.minute < 10:
                log.info(f"⏰ Hourly check triggered ({hour:02d}:00)")
                await scan_and_post(client, force=False, post_date_header=False)
                last_checked_hour = hour

        except Exception as exc:
            log.error(f"❌ Scheduler error: {exc}")

        await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(run_publisher())
