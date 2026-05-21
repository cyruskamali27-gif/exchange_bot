#!/usr/bin/env python3
"""
rate_graphic_publisher.py — Cyrus Global Exchange Rate Publisher

Input:  latest_rates.json
Output: final/*.png  →  @cyrusGlobalExchange
Errors: Bot API sendMessage → ADMIN_ID only (never to public channel)

Rendering rules (hard-coded — do not relax):
  • Template loaded fresh from templates_clean/ every render
  • Each price cell: restore master pixels → fill bg → write text ONCE
  • Font: Vazirmatn Bold for all text
  • Decimals: dot separator only  (1.43  not  1,43)
  • Empty / None value → blank cell (no placeholder, no zero)
  • No OCR, no iteration agents, no QC passes, no date overlays on currency posters
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests as _req
from PIL import Image, ImageDraw, ImageFont

try:
    import jdatetime
    _HAS_JDATE = True
except ImportError:
    _HAS_JDATE = False

from config import BOT_TOKEN, ADMIN_ID

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE          = Path("/var/www/exchange_bot")
TEMPLATES_DIR = BASE / "templates_clean"   # canonical clean masters — never modified
COORDS_FILE   = BASE / "poster_coordinates.json"
RATES_FILE    = BASE / "latest_rates.json"
CACHE_FILE    = BASE / "current_price.json"
OUTPUT_DIR    = BASE / "final"

# ── Channel config ─────────────────────────────────────────────────────────────
_publish_env   = os.environ.get("PUBLISH_CHANNEL", "@cyrusGlobalExchange")
TARGET_CHANNEL = (_publish_env if _publish_env.startswith("@") or _publish_env.lstrip("-").isdigit()
                  else f"@{_publish_env}")

# ── Runtime config ─────────────────────────────────────────────────────────────
TORONTO_TZ    = ZoneInfo("America/Toronto")
MONITOR_HOURS = 12
BUY_ADJ       = int(os.environ.get("PUBLISHER_BUY_ADJ", "500"))

# ── Font ───────────────────────────────────────────────────────────────────────
FONT_FA = "/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Bold.ttf"

# ── Template / output map ──────────────────────────────────────────────────────
TEMPLATE_FILES: dict[str, str] = {
    "CAD":    "canada.png",
    "USD":    "usa.png",
    "EUR":    "eur.png",
    "USDT":   "usdt.png",
    "USACAN": "usacan.png",
    "LOGO":   "logo.png",
}
OUTPUT_FILES: dict[str, Path] = {k: OUTPUT_DIR / v for k, v in TEMPLATE_FILES.items()}

# Logo date cells (1254×1254 template — top cells inside header band)
LOGO_SHAMSI_CELL    = [637, 948,  960, 1058]
LOGO_GREGORIAN_CELL = [637, 1058, 960, 1168]

# Publish order (logo is prepended at runtime)
PUBLISH_ORDER: list[tuple[str, str]] = [
    ("CAD",    "Canada"),
    ("USD",    "USA"),
    ("USACAN", "USA/CAN"),
    ("USDT",   "USDT"),
    ("EUR",    "Europe"),
]

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rate_publisher")

# ── Coordinates (loaded once at startup) ──────────────────────────────────────
def _load_coords() -> dict:
    try:
        return json.loads(COORDS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Cannot load {COORDS_FILE}: {exc}")

POSTER_COORDS: dict = _load_coords()


# ─────────────────────────────────────────────────────────────────────────────
# Admin notification
# ─────────────────────────────────────────────────────────────────────────────

def notify_admin(msg: str) -> None:
    """Send error/debug text to admin. Never called with public channel."""
    try:
        _req.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_ID, "text": f"[RatePublisher] {msg}"},
            timeout=10,
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Rendering primitives
# ─────────────────────────────────────────────────────────────────────────────

def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_FA, size)
    except Exception:
        return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str, box_xywh: tuple,
              max_size: int = 72, min_size: int = 32) -> ImageFont.FreeTypeFont:
    """Return the largest font that fits text within 90% width and 80% height of box."""
    x, y, w, h = box_xywh
    for size in range(max_size, min_size - 1, -2):
        font = _font(size)
        bb   = draw.textbbox((0, 0), text, font=font)
        if (bb[2] - bb[0]) <= w * 0.90 and (bb[3] - bb[1]) <= h * 0.80:
            return font
    return _font(min_size)


def _draw_centered(draw: ImageDraw.ImageDraw, box_xywh: tuple,
                   text: str, font, color: tuple) -> None:
    """Draw text mathematically centered inside box (x, y, width, height)."""
    x, y, w, h = box_xywh
    bb = draw.textbbox((0, 0), text, font=font)
    tx = x + (w - (bb[2] - bb[0])) / 2 - bb[0]
    ty = y + (h - (bb[3] - bb[1])) / 2 - bb[1]
    draw.text((tx, ty), text, font=font, fill=color)


def write_cell(
    working:  Image.Image,
    master:   Image.Image,
    box:      list,
    text:     str,
    color:    tuple = (255, 255, 255, 255),
    max_font: int = 60,
    min_font: int = 32,
) -> None:
    x1, y1, x2, y2 = (int(v) for v in box)
    bw, bh = x2 - x1, y2 - y1

    # Restore clean master region (wipes any previous render)
    working.paste(master.crop((x1, y1, x2, y2)), (x1, y1))

    if not text:
        return

    draw     = ImageDraw.Draw(working)
    box_xywh = (x1, y1, bw, bh)
    font     = _fit_font(draw, text, box_xywh, max_size=max_font, min_size=min_font)
    _draw_centered(draw, box_xywh, text, font, color)


# ─────────────────────────────────────────────────────────────────────────────
# Date helpers (logo poster only)
# ─────────────────────────────────────────────────────────────────────────────

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _shamsi(dt: datetime) -> str:
    if _HAS_JDATE:
        jd = jdatetime.datetime.fromgregorian(datetime=dt)
        return f"{jd.year}/{str(jd.month).zfill(2)}/{str(jd.day).zfill(2)}".translate(_FA)
    return dt.strftime("%Y/%m/%d")


def _gregorian(dt: datetime) -> str:
    return dt.strftime("%Y/%m/%d")


# ─────────────────────────────────────────────────────────────────────────────
# Poster generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_logo(now: datetime) -> Path | None:
    tpl = TEMPLATES_DIR / TEMPLATE_FILES["LOGO"]
    if not tpl.exists():
        log.error(f"[LOGO] template missing: {tpl}")
        notify_admin(f"Logo template missing: {tpl}")
        return None
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    master  = Image.open(tpl).convert("RGBA")
    working = master.copy()
    write_cell(working, master, LOGO_SHAMSI_CELL,    _shamsi(now),    (242, 222, 148, 255), max_font=44)
    write_cell(working, master, LOGO_GREGORIAN_CELL, _gregorian(now), (210, 190, 120, 255), max_font=44)
    out = OUTPUT_FILES["LOGO"]
    working.convert("RGB").save(str(out), "PNG")
    log.info(f"[LOGO] {_shamsi(now)} / {_gregorian(now)} → {out.name}")
    return out


def generate_poster(currency: str, prices: dict) -> Path | None:
    tpl_name = TEMPLATE_FILES.get(currency)
    if not tpl_name:
        log.error(f"[{currency}] no template mapping")
        return None
    tpl = TEMPLATES_DIR / tpl_name
    if not tpl.exists():
        log.error(f"[{currency}] template missing: {tpl}")
        notify_admin(f"{currency} template missing: {tpl}")
        return None
    coords = POSTER_COORDS.get(currency)
    if not coords:
        log.error(f"[{currency}] no coordinates in {COORDS_FILE.name}")
        notify_admin(f"No coordinates for {currency}")
        return None
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    master  = Image.open(tpl).convert("RGBA")
    working = master.copy()

    is_ratio = currency in ("USACAN", "EUR")

    def fmt(v: float | None) -> str:
        if v is None:
            return ""
        return f"{v:.2f}" if is_ratio else f"{int(v):,}"

    WHITE = (255, 255, 255, 255)
    mf = 90 if currency == "CAD" else 60

    sell_boxes = coords.get("sell_boxes", [])
    sell_keys  = coords.get("sell_keys", ["sell"] * len(sell_boxes))
    for box, key in zip(sell_boxes, sell_keys):
        write_cell(working, master, box, fmt(prices.get(key)), WHITE, max_font=mf)

    buy_boxes = coords.get("buy_boxes", [])
    buy_keys  = coords.get("buy_keys", ["buy"] * len(buy_boxes))
    for box, key in zip(buy_boxes, buy_keys):
        write_cell(working, master, box, fmt(prices.get(key)), WHITE, max_font=mf)

    out = OUTPUT_FILES[currency]
    working.convert("RGB").save(str(out), "PNG")
    log.info(f"[{currency}] {prices} → {out.name}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Telegram posting (Bot API)
# ─────────────────────────────────────────────────────────────────────────────

def post_image(path: Path) -> int:
    with open(path, "rb") as f:
        resp = _req.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": TARGET_CHANNEL},
            files={"photo": f},
            timeout=30,
        )
    data = resp.json()
    if data.get("ok"):
        return data["result"]["message_id"]
    raise RuntimeError(data.get("description", "Bot API error"))


# ─────────────────────────────────────────────────────────────────────────────
# Rate loading
# ─────────────────────────────────────────────────────────────────────────────

def load_rates() -> dict:
    """Load from latest_rates.json; fallback to current_price.json."""
    rates: dict = {}
    try:
        data = json.loads(RATES_FILE.read_text(encoding="utf-8"))
        for cur, prices in data.get("rates", {}).items():
            rates[cur] = {k: v for k, v in prices.items() if v is not None}
        log.info(f"Rates loaded from {RATES_FILE.name}")
    except Exception as exc:
        log.warning(f"Cannot read {RATES_FILE.name}: {exc}")
        notify_admin(f"Cannot read rates file: {exc}")

    # Fallback: current_price.json for USD/CAD/USDT sell price
    if not rates:
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            for cur in ("USD", "CAD", "USDT"):
                entry = cache.get(cur, {})
                s = entry.get("our_sell") or entry.get("price")
                b = entry.get("our_buy")  or entry.get("buy")
                if s or b:
                    rates[cur] = {}
                    if s:
                        rates[cur]["sell"] = int(s)
                    if b:
                        rates[cur]["buy"]  = int(b)
            if rates:
                log.info(f"Rates from fallback {CACHE_FILE.name}")
        except Exception as exc:
            log.warning(f"Cannot read {CACHE_FILE.name}: {exc}")

    return rates


def save_rates(rates: dict, now: datetime) -> None:
    RATES_FILE.write_text(
        json.dumps({"rates": rates, "updated_at": now.isoformat()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def rates_changed(new: dict, stored: dict) -> bool:
    old = stored.get("rates", {})
    for cur, prices in new.items():
        for k, v in prices.items():
            if old.get(cur, {}).get(k) != v:
                return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Derived rates
# ─────────────────────────────────────────────────────────────────────────────

def _derive_usacan(rates: dict) -> dict | None:
    usd = rates.get("USD", {})
    cad = rates.get("CAD", {})
    if not usd.get("sell") or not cad.get("sell"):
        return None
    sell = round(usd["sell"] / cad["sell"], 2)
    buy  = (round(usd["buy"] / cad["buy"], 2)
            if usd.get("buy") and cad.get("buy") else None)
    r = {"sell": sell}
    if buy:
        r["buy"] = buy
    return r


def _derive_eur(rates: dict) -> dict | None:
    usd = rates.get("USD", {})
    cad = rates.get("CAD", {})
    if not usd.get("sell") or not cad.get("sell"):
        return None
    try:
        resp        = _req.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        eur_per_usd = resp.json().get("rates", {}).get("EUR")
        if not eur_per_usd:
            return None
        sell = round(usd["sell"] / eur_per_usd / cad["sell"], 2)
        buy  = (round(usd["buy"] / eur_per_usd / cad["buy"], 2)
                if usd.get("buy") and cad.get("buy") else None)
        log.info(f"[EUR] EUR/USD={eur_per_usd:.4f}  EUR/CAD sell={sell}")
        r = {"sell": sell}
        if buy:
            r["buy"] = buy
        return r
    except Exception as exc:
        log.warning(f"[EUR] live rate fetch failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Bahmani text parser (text-only — no image download, no OCR)
# ─────────────────────────────────────────────────────────────────────────────

_CUR_KWS: dict[str, list[str]] = {
    "CAD":  ["دلار کانادا", "کانادا", "کاناد", "CAD"],
    "EUR":  ["یورو", "اروپا", "EUR"],
    "USDT": ["تتر", "USDT", "tether"],
    "USD":  ["دلار آمریکا", "دلار امریکا", "USD", "دلار"],
}
_BUY_KWS  = ["خرید", "buy", "خریدار"]
_SELL_KWS = ["فروش", "sell", "حواله"]


def _norm(text: str) -> str:
    for fa, en in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
        text = text.replace(fa, en)
    return re.sub(r"(\d{1,3})\.(\d{3})\b", r"\1,\2", text.replace("،", ","))


def _parse_bahmani(text: str) -> dict:
    text   = _norm(text)
    result: dict = {}
    for m in re.finditer(r"(\d{2,3},\d{3}|\d{5,6})", text):
        val = int(m.group().replace(",", ""))
        if not 10_000 <= val <= 500_000:
            continue
        ctx = text[max(0, m.start() - 200): m.end() + 200].lower()
        cur = next((c for c, kws in _CUR_KWS.items() if any(k.lower() in ctx for k in kws)), None)
        if not cur:
            continue
        direction = "buy" if any(k in ctx for k in _BUY_KWS) else "sell"
        result.setdefault(cur, {})
        if direction not in result[cur]:
            result[cur][direction] = val
            log.info(f"  [Bahmani] {cur} {direction} = {val:,}")
    return result


def _apply_adj(parsed: dict) -> dict:
    out = {}
    for cur, p in parsed.items():
        out[cur] = {}
        if p.get("sell") is not None:
            out[cur]["sell"] = p["sell"]
        if p.get("buy") is not None:
            out[cur]["buy"] = p["buy"] + BUY_ADJ
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Main post cycle
# ─────────────────────────────────────────────────────────────────────────────

async def post_all(force: bool = False, fresh_text: str | None = None) -> None:
    now = datetime.now(TORONTO_TZ)
    log.info(f"=== post_all force={force}  {now:%Y-%m-%d %H:%M %Z} ===")

    # 1 — resolve rates
    rates: dict = {}
    if fresh_text:
        parsed = _parse_bahmani(fresh_text)
        if parsed:
            rates = _apply_adj(parsed)
            log.info(f"Rates from Bahmani text (adj): {rates}")
    if not rates:
        rates = load_rates()

    # 2 — derive ratios (always fresh — never stored)
    usacan = _derive_usacan(rates)
    if usacan:
        rates["USACAN"] = usacan
        log.info(f"  [USACAN] sell={usacan['sell']}  buy={usacan.get('buy')}")
    eur = _derive_eur(rates)
    if eur:
        rates["EUR"] = eur

    if not rates:
        log.warning("No rate data — skipping post")
        notify_admin("No rate data available — post skipped")
        return

    # 3 — change detection
    stored: dict = {}
    try:
        stored = json.loads(RATES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    if not force and not rates_changed(rates, stored):
        log.info("Rates unchanged — skipping")
        return

    reason = "forced" if force else "rates changed"
    log.info(f"Posting: {reason}")

    # 4 — logo
    logo = generate_logo(now)
    if logo:
        try:
            mid = post_image(logo)
            log.info(f"[LOGO] message_id={mid}")
        except Exception as exc:
            log.error(f"[LOGO] post failed: {exc}")
            notify_admin(f"LOGO post failed: {exc}")
        await asyncio.sleep(2)
    else:
        notify_admin("LOGO generation failed — template missing?")

    # 5 — currency posters
    for cur_code, name_en in PUBLISH_ORDER:
        prices = rates.get(cur_code, {})
        try:
            path = generate_poster(cur_code, prices)
            if path:
                mid = post_image(path)
                log.info(f"[{cur_code}] message_id={mid}")
                await asyncio.sleep(2)
        except Exception as exc:
            log.error(f"[{cur_code}] failed: {exc}")
            notify_admin(f"{cur_code} ({name_en}) failed: {exc}")

    save_rates(rates, now)
    log.info("=== done ===")


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────────────────

async def run_publisher() -> None:
    log.info("Rate Publisher starting (bot-token mode — no Telethon)")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    monitor_end       = datetime.now(TORONTO_TZ) + timedelta(hours=MONITOR_HOURS)
    last_daily_date   = None
    last_checked_hour = -1

    while True:
        try:
            now   = datetime.now(TORONTO_TZ)
            today = now.date()
            hour  = now.hour

            if now <= monitor_end:
                if hour != last_checked_hour and now.minute < 10:
                    remaining = int((monitor_end - now).total_seconds() / 3600)
                    log.info(f"[12h] Hourly check {hour:02d}:00 (~{remaining}h left)")
                    await post_all(force=False)
                    last_checked_hour = hour
            else:
                if hour == 9 and last_daily_date != today:
                    log.info("9 AM daily post")
                    await post_all(force=True)
                    last_daily_date   = today
                    last_checked_hour = hour
                elif hour != last_checked_hour and now.minute < 10:
                    log.info(f"Hourly check {hour:02d}:00")
                    await post_all(force=False)
                    last_checked_hour = hour

        except Exception as exc:
            log.error(f"Scheduler error: {exc}")
            notify_admin(f"Scheduler error: {exc}")

        await asyncio.sleep(60)


if __name__ == "__main__":
    import sys
    if "--force-clean" in sys.argv:
        asyncio.run(post_all(force=True))
    else:
        asyncio.run(run_publisher())
