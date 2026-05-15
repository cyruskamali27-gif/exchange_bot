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

# Contact info drawn on every poster footer
CONTACT_PHONE     = "2269627729"
CONTACT_TELEGRAM  = "@cyrusGlobalExchange"
CONTACT_INSTAGRAM = "@cyrusGlobalExchange"
CONTACT_LOCATION  = "Guelph, Ontario"

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

# ─── Price row coordinates ─────────────────────────────────────────────────────
# Pixel analysis shows:
#   - Left price column (Persian side): x ≈ 248–455
#   - Right price column (English side): x ≈ 490–700
# Both columns hold the same price. Old code only cleared x=258-460, leaving
# the right column (x≈490-700) uncleaned — that is why old prices remained.
#
# Fix: clear X_CLEAR_START→X_CLEAR_END (covers BOTH columns), then write
# the label on the left and the new price on the right.
#
# Each tuple: (y_top, y_bot, price_key, label_fa, label_en)

POSTER_ROWS: dict[str, list[tuple]] = {
    "CAD": [
        (238, 284, "sell", "فروش نقدی",       "Cash Sell"),
        (303, 348, "sell", "فروش ای‌ترنسفر", "E-Transfer"),
        (370, 413, "buy",  "خرید",            "Buy"),
    ],
    "USD": [
        (305, 350, "sell", "فروش نقدی", "Cash Sell"),
        (383, 428, "buy",  "خرید",      "Buy"),
    ],
    "EUR": [
        (336, 380, "sell", "فروش", "Sell"),
        (388, 428, "buy",  "خرید", "Buy"),
    ],
    "USDT": [
        (276, 316, "sell", "فروش تتر", "USDT Sell"),
        (356, 396, "buy",  "خرید تتر", "USDT Buy"),
    ],
    "USACAN": [
        (336, 380, "sell", "فروش", "Sell"),
        (388, 428, "buy",  "خرید", "Buy"),
    ],
}

# X range cleared for every price row — covers BOTH the left and right price columns
X_CLEAR_START = 50
X_CLEAR_END   = 755

# How many pixels at the TOP to clear (strips with old phone/handles baked in)
# Confirmed by pixel/OCR analysis:
#   USD, EUR, USDT: ~65px top strip shows old "@CyrusExchange / 416-319-0000"
#   CAD: no confirmed top-strip old info, use 0 to avoid clipping title
TOP_STRIP_H: dict[str, int] = {
    "CAD":    0,
    "USD":    65,
    "EUR":    65,
    "USDT":   52,
    "USACAN": 65,
}

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


def fa_num(n: int) -> str:
    return f"{n:,}"


def persian_date(dt: datetime) -> str:
    if HAS_JDATE:
        jd = jdatetime.datetime.fromgregorian(datetime=dt)
        months = ["فروردین","اردیبهشت","خرداد","تیر","مرداد","شهریور",
                  "مهر","آبان","آذر","دی","بهمن","اسفند"]
        return f"{jd.day} {months[jd.month-1]} {jd.year}"
    return dt.strftime("%Y-%m-%d")


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        log.warning(f"Font unavailable: {path}")
        return ImageFont.load_default()


# ─── Background colour sampling ───────────────────────────────────────────────

def sample_bg_left_edge(px, y_center: int, H: int) -> tuple:
    """
    Sample background from x=10-35 (far-left edge, always dark/background).
    Returns (R, G, B).
    """
    samples = []
    yc = max(0, min(y_center, H - 1))
    for sx in range(10, 36):
        samples.append(px[sx, yc])
    r = sum(s[0] for s in samples) // len(samples)
    g = sum(s[1] for s in samples) // len(samples)
    b = sum(s[2] for s in samples) // len(samples)
    return (r, g, b)


# ─── Poster generator ─────────────────────────────────────────────────────────

def generate_single_poster(currency: str, cur_name_fa: str,
                            buy: int | None, sell: int | None,
                            toronto_now: datetime) -> Path | None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    template_path = TEMPLATES.get(currency)
    if not template_path or not template_path.exists():
        log.error(f"[{currency}] Missing template: {template_path}")
        return None

    log.info(f"[{currency}] === Generating poster ===")
    log.info(f"[{currency}] Template: {template_path}")
    log.info(f"[{currency}] Cyrus sell={sell}  Cyrus buy={buy}")

    # Always open the ORIGINAL template — never a previously generated file
    img     = Image.open(template_path).convert("RGBA")
    img_rgb = img.convert("RGB")
    draw    = ImageDraw.Draw(img)
    W, H    = img.size
    px      = img_rgb.load()

    prices = {"sell": sell, "buy": buy}

    # ── 1. Clear TOP STRIP with old contact info ───────────────────────────
    top_h = TOP_STRIP_H.get(currency, 0)
    if top_h > 0:
        bg = sample_bg_left_edge(px, top_h // 2, H)
        draw.rectangle([0, 0, W, top_h], fill=(*bg, 255))
        log.info(f"[{currency}] Cleared top strip y=0-{top_h}  bg={bg}")

    # ── 2. Clear each price row and write label + new price ────────────────
    rows = POSTER_ROWS.get(currency, [])

    for y1, y2, price_key, label_fa, label_en in rows:
        value     = prices.get(price_key)
        value_str = fa_num(value) if value is not None else "---"

        row_h = y2 - y1
        bg    = sample_bg_left_edge(px, (y1 + y2) // 2, H)

        # Clear the full price zone (covers both left AND right old price columns)
        draw.rectangle([X_CLEAR_START, y1, X_CLEAR_END, y2], fill=(*bg, 255))
        log.info(f"  [{currency}] Cleared row {price_key} y={y1}-{y2} x={X_CLEAR_START}-{X_CLEAR_END}  bg={bg}")

        # ── Label fonts (sized to ~40% of row height) ──────────────────────
        lbl_size = max(11, int(row_h * 0.40))
        font_lbl_fa = load_font(FONT_BOLD,       lbl_size)
        font_lbl_en = load_font(FONT_REGULAR_EN, max(10, lbl_size - 2))

        # ── Price font (sized to ~65% of row height) ───────────────────────
        price_size = max(16, int(row_h * 0.65))
        font_price = load_font(FONT_BOLD_EN, price_size)

        # Layout: label occupies x=X_CLEAR_START..MIDPOINT-10
        #         price occupies x=MIDPOINT+10..X_CLEAR_END-10
        midpoint   = (X_CLEAR_START + X_CLEAR_END) // 2

        # --- Draw Persian label (right-aligned in label zone) ---------------
        fa_label  = fa(label_fa)
        lb = draw.textbbox((0, 0), fa_label, font=font_lbl_fa)
        lw, lh = lb[2] - lb[0], lb[3] - lb[1]
        lx = midpoint - 15 - lw          # right-align before midpoint
        ly = y1 + (row_h - lh) // 2
        if lx >= X_CLEAR_START:
            draw.text((lx, ly), fa_label, font=font_lbl_fa,
                      fill=(255, 220, 150, 255))

        # --- Draw English label below Persian (right-aligned) ---------------
        eb = draw.textbbox((0, 0), label_en, font=font_lbl_en)
        ew, eh = eb[2] - eb[0], eb[3] - eb[1]
        ex = midpoint - 15 - ew
        ey = ly + lh + 1
        if ex >= X_CLEAR_START and ey + eh <= y2:
            draw.text((ex, ey), label_en, font=font_lbl_en,
                      fill=(200, 175, 110, 220))

        # --- Draw price number (centered in right zone) ---------------------
        pb = draw.textbbox((0, 0), value_str, font=font_price)
        pw, ph = pb[2] - pb[0], pb[3] - pb[1]
        px_pos = midpoint + 10 + (X_CLEAR_END - midpoint - 10 - pw) // 2
        py_pos = y1 + (row_h - ph) // 2

        draw.text((px_pos + 1, py_pos + 1), value_str, font=font_price,
                  fill=(0, 0, 0, 130))
        draw.text((px_pos,     py_pos),     value_str, font=font_price,
                  fill=(255, 255, 255, 255))
        log.info(f"  [{currency}] Wrote '{value_str}' ({price_key}) at ({px_pos},{py_pos}) size={price_size}px")

    # ── 3. Clear old footer and draw fresh footer ──────────────────────────
    FOOTER_H  = 65
    footer_y1 = H - FOOTER_H

    footer_bg = sample_bg_left_edge(px, H - 30, H)
    # Use a slightly darker/consistent teal footer background
    # (all templates have teal footer ~ (7,19,19); sampled value confirms)
    draw.rectangle([0, footer_y1, W, H], fill=(*footer_bg, 255))
    log.info(f"[{currency}] Cleared footer y={footer_y1}-{H}  bg={footer_bg}")

    # Footer lines (centered, gold/cream colour)
    footer_color = (245, 226, 178, 255)   # warm cream/gold
    shadow_color = (0, 0, 0, 120)

    footer_lines = [
        ("Cyrus Global Exchange",                              FONT_BOLD_EN,    16),
        (f"Phone: {CONTACT_PHONE}  |  {CONTACT_LOCATION}",   FONT_REGULAR_EN, 12),
        (f"Telegram: {CONTACT_TELEGRAM}  |  Instagram: {CONTACT_INSTAGRAM}",
                                                               FONT_REGULAR_EN, 11),
    ]

    total_h = sum(sz + 4 for _, _, sz in footer_lines)
    cur_y   = footer_y1 + (FOOTER_H - total_h) // 2

    for text, fpath, fsize in footer_lines:
        font = load_font(fpath, fsize)
        bb   = draw.textbbox((0, 0), text, font=font)
        tw   = bb[2] - bb[0]
        tx   = (W - tw) // 2
        draw.text((tx + 1, cur_y + 1), text, font=font, fill=shadow_color)
        draw.text((tx,     cur_y),     text, font=font, fill=footer_color)
        cur_y += fsize + 4

    log.info(f"[{currency}] Footer: Phone={CONTACT_PHONE} | Tel={CONTACT_TELEGRAM} "
             f"| IG={CONTACT_INSTAGRAM} | {CONTACT_LOCATION}")

    # ── 4. Save ────────────────────────────────────────────────────────────
    ts_str   = toronto_now.strftime("%Y-%m-%d-%H%M")
    out_path = GENERATED_DIR / f"{ts_str}-{currency.lower()}.png"
    img.convert("RGB").save(str(out_path), "PNG")
    log.info(f"[{currency}] Saved: {out_path}")
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
