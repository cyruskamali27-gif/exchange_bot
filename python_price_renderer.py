#!/usr/bin/env python3
"""
python_price_renderer.py
ONLY Python writes final exchange-rate numbers onto posters.

Reads rates from latest_rates.json.
Formula: Cyrus SELL = Bahmani SELL exactly, Cyrus BUY = Bahmani BUY + 500.
Saves rendered posters to /var/www/exchange_bot/rendered_prices/

Public API
----------
render_prices(poster_type, design_path, toronto_now) -> Path
    Write prices onto a design image. Returns rendered PNG path.

get_expected_prices(poster_type) -> dict
    Return the exact sell/buy strings Python will write (for QC comparison).
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from smart_text_engine import fit_text_inside_box, get_today_date_lines

log = logging.getLogger("price_renderer")

RATES_FILE   = Path("/var/www/exchange_bot/latest_rates.json")
RENDERED_DIR = Path("/var/www/exchange_bot/rendered_prices")
TEMPLATES_DIR = Path("/var/www/exchange_bot/assets/posters")
TORONTO_TZ   = ZoneInfo("America/Toronto")

FONT_BOLD_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

BUY_ADJ  = 500   # Cyrus BUY  = Bahmani BUY + 500 تومان
SELL_ADJ = 0     # Cyrus SELL = Bahmani SELL exactly

# ── Coordinate profiles per poster type ───────────────────────────────────────
# sell_boxes / buy_boxes match the existing locked coordinate system.
# date_strip uses the compact no-fill profile.
COORDS: dict[str, dict] = {
    "canada": {
        "currency_key": "CAD",
        "template":     "updated_cyrus_exchange_canada_poster.png",
        "format":       "toman",
        "sell_boxes":   [[545, 342, 878, 422], [545, 464, 878, 545], [545, 585, 878, 666]],
        "buy_boxes":    [[545, 706, 878, 787], [545, 827, 878, 908]],
        "date_strip":   [260, 222, 1100, 342],
    },
    "usa": {
        "currency_key": "USD",
        "template":     "updated_cyrus_exchange_usa_poster.png",
        "format":       "toman",
        "sell_boxes":   [[503, 342, 852, 426], [503, 464, 852, 549], [503, 587, 852, 671]],
        "buy_boxes":    [[503, 706, 852, 790], [503, 826, 852, 910]],
        "date_strip":   [260, 210, 1100, 342],
    },
    "usacan": {
        "currency_key": "USACAN",
        "template":     "updated_cyrus_exchange_usacan_poster.png",
        "format":       "decimal",
        "sell_boxes":   [[728, 385, 1012, 532]],
        "buy_boxes":    [[728, 700, 1012, 862]],
        "date_strip":   [260, 225, 1100, 385],
    },
    "usdt": {
        "currency_key": "USDT",
        "template":     "updated_cyrus_exchange_usdt_poster.png",
        "format":       "toman",
        "sell_boxes":   [[684, 375, 972, 595]],
        "buy_boxes":    [[684, 645, 972, 870]],
        "date_strip":   [260, 210, 1100, 375],
    },
    "eur": {
        "currency_key": "EUR",
        "template":     "updated_cyrus_exchange_europe_poster.png",
        "format":       "decimal",
        "sell_boxes":   [[684, 375, 972, 600]],
        "buy_boxes":    [[684, 638, 972, 862]],
        "date_strip":   [260, 210, 1100, 375],
    },
}


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, max(10, size))
    except Exception:
        return ImageFont.load_default()


def _load_rates() -> dict:
    try:
        return json.loads(RATES_FILE.read_text(encoding="utf-8")).get("rates", {})
    except Exception as exc:
        raise RuntimeError(f"Cannot read {RATES_FILE}: {exc}")


def get_expected_prices(poster_type: str) -> dict:
    """
    Return the exact sell/buy strings that Python will write.
    Used by QC to know what to look for.
    """
    key    = poster_type.lower()
    coords = COORDS.get(key)
    if not coords:
        raise ValueError(f"Unknown poster_type: {poster_type}")

    rates    = _load_rates()
    cur_key  = coords["currency_key"]
    cur_data = rates.get(cur_key, {})
    fmt      = coords["format"]

    sell_raw = cur_data.get("sell")
    buy_raw  = cur_data.get("buy")

    if sell_raw is not None:
        sell_val = float(sell_raw)
        adj      = 0 if fmt == "decimal" else BUY_ADJ   # BUY_ADJ is in Toman, not for FX ratios
        buy_val  = float(buy_raw) + adj if buy_raw is not None else None
    else:
        sell_val = buy_val = None

    if fmt == "decimal":
        sell_str = f"{sell_val:.2f}" if sell_val is not None else "---"
        buy_str  = f"{buy_val:.2f}"  if buy_val  is not None else "---"
    else:
        sell_str = f"{int(sell_val):,}" if sell_val is not None else "---"
        buy_str  = f"{int(buy_val):,}"  if buy_val  is not None else "---"

    return {
        "poster_type":   key,
        "currency_key":  cur_key,
        "sell_str":      sell_str,
        "buy_str":       buy_str,
        "sell_raw":      sell_raw,
        "buy_raw":       buy_raw,
        "buy_adjusted":  buy_val,
        "format":        fmt,
    }


def _restore_box(img: Image.Image, original: Image.Image, box: list, inset: int = 3):
    """Paste original template pixels back into box interior — preserves gradients, glow, texture."""
    x1, y1, x2, y2 = [int(v) for v in box]
    region = original.crop((x1 + inset, y1 + inset, x2 - inset, y2 - inset))
    img.paste(region, (x1 + inset, y1 + inset))


def _erase_box(draw: ImageDraw.ImageDraw, px_rgb, box: list):
    """Fallback: erase old placeholder digits by filling with sampled background colour.
    Used only for AI-generated designs where no original template is available."""
    x1, y1, x2, y2 = [int(v) for v in box]
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
    draw.rectangle([x1 + 3, y1 + 3, x2 - 3, y2 - 3], fill=(*bg, 255))
    return bg


def _write_centered(draw: ImageDraw.ImageDraw, box: list, text: str,
                    color=(255, 255, 255, 255)):
    """Write text centered inside box at auto-fitted font size."""
    x1, y1, x2, y2 = [int(v) for v in box]
    zone_w = x2 - x1
    zone_h = y2 - y1

    font_size = min(80, max(14, int(zone_h * 0.60)))
    font = _load_font(FONT_BOLD_EN, font_size)
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]

    while tw > zone_w - 8 and font_size > 14:
        font_size -= 2
        font = _load_font(FONT_BOLD_EN, font_size)
        bb = draw.textbbox((0, 0), text, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]

    tx = x1 + (zone_w - tw) // 2
    ty = y1 + (zone_h - th) // 2
    draw.text((tx + 1, ty + 1), text, font=font, fill=(0, 0, 0, 90))
    draw.text((tx, ty), text, font=font, fill=color)
    return font_size


def render_prices(
    poster_type: str,
    design_path: Path | None = None,
    toronto_now: datetime | None = None,
) -> Path:
    """
    Read exact rates from latest_rates.json, render onto design image.

    Parameters
    ----------
    poster_type  : "canada" | "usa" | "usacan" | "usdt" | "eur"
    design_path  : AI-generated design PNG, or None to use existing template
    toronto_now  : datetime for date rendering (defaults to now)

    Returns
    -------
    Path to saved rendered PNG in rendered_prices/
    """
    RENDERED_DIR.mkdir(parents=True, exist_ok=True)

    key    = poster_type.lower()
    coords = COORDS.get(key)
    if not coords:
        raise ValueError(f"Unknown poster_type '{poster_type}'. Valid: {list(COORDS)}")

    prices = get_expected_prices(key)
    sell_str = prices["sell_str"]
    buy_str  = prices["buy_str"]
    log.info(f"[renderer] {key.upper()}  sell='{sell_str}'  buy='{buy_str}'")

    # Open image source
    if design_path and Path(design_path).exists():
        img = Image.open(design_path).convert("RGBA")
        log.info(f"[renderer] Using AI design: {Path(design_path).name}")
    else:
        tmpl = TEMPLATES_DIR / coords["template"]
        if not tmpl.exists():
            raise FileNotFoundError(f"Template not found: {tmpl}")
        img = Image.open(tmpl).convert("RGBA")
        log.info(f"[renderer] Using existing template: {coords['template']}")

    draw   = ImageDraw.Draw(img)
    px_rgb = img.convert("RGB").load()

    # Write sell prices into each sell box
    for box in coords.get("sell_boxes", []):
        _erase_box(draw, px_rgb, box)
        fs = _write_centered(draw, box, sell_str)
        log.info(f"[renderer]   SELL box={box} font={fs}px")

    # Write buy prices into each buy box
    for box in coords.get("buy_boxes", []):
        _erase_box(draw, px_rgb, box)
        fs = _write_centered(draw, box, buy_str)
        log.info(f"[renderer]   BUY  box={box} font={fs}px")

    # Render date strip
    date_strip = coords.get("date_strip")
    if date_strip:
        if toronto_now is None:
            toronto_now = datetime.now(TORONTO_TZ)
        date_data = get_today_date_lines(toronto_now)
        fit_text_inside_box(
            img, date_strip,
            date_data["lines"], date_data["font_paths"], date_data["colors"],
            align="center", vertical_align="center",
            min_font_size=10, max_font_size=22,
            padding_x=16, padding_y=10, line_spacing=4,
            rtl_lines=date_data["rtl_lines"],
            shadow=True, border_color=None, border_width=0,
            clean_bg=False, stroke_width=2,
        )

    toronto_now = toronto_now or datetime.now(TORONTO_TZ)
    ts       = toronto_now.strftime("%Y%m%d_%H%M%S")
    out_path = RENDERED_DIR / f"{key}_{ts}.png"
    img.convert("RGB").save(str(out_path), "PNG")
    log.info(f"[renderer] Saved → {out_path}")
    return out_path
