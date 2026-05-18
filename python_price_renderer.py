#!/usr/bin/env python3
"""
python_price_renderer.py
ONLY Python writes final exchange-rate numbers onto posters.

Reads rates from latest_rates.json.
Formula: Cyrus SELL = Bahmani SELL exactly, Cyrus BUY = Bahmani BUY + 500.
Saves rendered posters to /var/www/exchange_bot/rendered_prices/

Marker system
-------------
Templates live in two parallel directories:

  templates_clean/   — pixel-perfect originals, used as the rendering base.
  templates_marker/  — same images with pure #FF00FF (RGB 255,0,255) outline
                       rectangles drawn around every changing price cell.
                       The colour does not appear anywhere else in the originals.

At render time the renderer:
  1. Opens the marker file for the requested poster type.
  2. Auto-detects all pink rectangles (ordered top → bottom → sell first, buy last).
  3. Opens the corresponding clean file (or an AI design_path when supplied).
  4. For each detected box: erases the old baked-in placeholder → writes the new
     price centred inside the same region.
  5. Saves the result to rendered_prices/.

Public API
----------
render_prices(poster_type, design_path, toronto_now) -> Path
    Write live prices onto a poster.  Returns the saved PNG path.

get_expected_prices(poster_type) -> dict
    Return the exact sell/buy strings the renderer will write (for QC).
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from smart_text_engine import get_today_date_lines  # kept for external callers

log = logging.getLogger("price_renderer")

# ── Paths ─────────────────────────────────────────────────────────────────────
RATES_FILE   = Path("/var/www/exchange_bot/latest_rates.json")
RENDERED_DIR = Path("/var/www/exchange_bot/rendered_prices")
MARKER_DIR   = Path("/var/www/exchange_bot/templates_marker")
CLEAN_DIR    = Path("/var/www/exchange_bot/templates_clean")
TORONTO_TZ   = ZoneInfo("America/Toronto")

FONT_BOLD_EN     = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
PINK             = (255, 0, 255)   # #FF00FF — the marker colour
MARKER_THICKNESS = 4               # px — must match create_marker_templates.py

BUY_ADJ  = 500   # Cyrus BUY  = Bahmani BUY + 500 تومان
SELL_ADJ = 0     # Cyrus SELL = Bahmani SELL exactly

# ── Per-poster configuration ──────────────────────────────────────────────────
# sell_count: how many top-most detected boxes are sell rows (rest are buy rows).
# format:     "toman"   → integer with commas  e.g. "127,000"
#             "decimal" → two decimal places   e.g. "1.38"
POSTER_CFG: dict[str, dict] = {
    "canada": {"currency_key": "CAD",    "format": "toman",   "sell_count": 3, "file": "canada.png"},
    "usa":    {"currency_key": "USD",    "format": "toman",   "sell_count": 3, "file": "usa.png"},
    "usdt":   {"currency_key": "USDT",   "format": "toman",   "sell_count": 1, "file": "usdt.png"},
    "usacan": {"currency_key": "USACAN", "format": "decimal", "sell_count": 1, "file": "usacan.png"},
    "eur":    {"currency_key": "EUR",    "format": "decimal", "sell_count": 1, "file": "eur.png"},
}


# ── Marker detection ──────────────────────────────────────────────────────────

def _detect_pink_boxes(marker_path: Path) -> list[list[int]]:
    """
    Scan a marker template for #FF00FF rectangles and return their bounding
    boxes as [[x1, y1, x2, y2], ...] sorted top-to-bottom then left-to-right.

    Algorithm
    ---------
    1. Find every row that contains pink pixels; record its x extent and count.
    2. Group consecutive pink rows into contiguous bands.
    3. Within each band, classify rows as "edge" (≥25 % of box width is pink,
       i.e. a horizontal top/bottom bar) or "interior" (only the thin left/right
       side rails ≈ 2×THICKNESS pixels).
    4. Build stripes of consecutive edge rows.
    5. Detect "double stripes" — where two adjacent boxes share a boundary (logo
       date cells).  A stripe taller than THICKNESS×2-1 is split at its midpoint.
    6. Pair stripes as (top_edge, bottom_edge) to recover each box.
    """
    img = Image.open(marker_path).convert("RGB")
    px  = img.load()
    W, H = img.size

    # Step 1: collect pink-pixel data per row
    row_info: dict[int, tuple[int, int, int]] = {}  # y → (x_min, x_max, count)
    for y in range(H):
        xs = [x for x in range(W) if px[x, y] == PINK]
        if xs:
            row_info[y] = (min(xs), max(xs), len(xs))

    if not row_info:
        return []

    # Step 2: group into contiguous bands
    sorted_ys = sorted(row_info)
    bands: list[tuple[int, int, int, int]] = []
    by1 = by2 = sorted_ys[0]
    bx1, bx2 = row_info[by1][0], row_info[by1][1]

    for y in sorted_ys[1:]:
        if y == by2 + 1:
            by2 = y
            bx1 = min(bx1, row_info[y][0])
            bx2 = max(bx2, row_info[y][1])
        else:
            bands.append((by1, by2, bx1, bx2))
            by1 = by2 = y
            bx1, bx2 = row_info[y][0], row_info[y][1]
    bands.append((by1, by2, bx1, bx2))

    # Steps 3-6: detect boxes within each band
    boxes: list[list[int]] = []

    for band_y1, band_y2, band_x1, band_x2 in bands:
        box_w          = band_x2 - band_x1 + 1
        edge_threshold = max(20, box_w * 0.25)  # ≥25 % width → horizontal bar

        # Edge rows = top/bottom bar rows of a rectangle
        edge_ys = sorted(
            y for y in range(band_y1, band_y2 + 1)
            if y in row_info and row_info[y][2] >= edge_threshold
        )

        if not edge_ys:
            boxes.append([band_x1, band_y1, band_x2, band_y2])
            continue

        # Build stripes of consecutive edge rows
        stripes: list[tuple[int, int]] = []
        sy1 = sy2 = edge_ys[0]
        for ey in edge_ys[1:]:
            if ey == sy2 + 1:
                sy2 = ey
            else:
                stripes.append((sy1, sy2))
                sy1 = sy2 = ey
        stripes.append((sy1, sy2))

        # Split double stripes (two adjacent boxes sharing one boundary)
        split: list[tuple[int, int]] = []
        for sy1, sy2 in stripes:
            if sy2 - sy1 + 1 >= MARKER_THICKNESS * 2 - 1:
                mid = (sy1 + sy2) // 2
                split.append((sy1, mid))
                split.append((mid, sy2))
            else:
                split.append((sy1, sy2))

        # Pair (top_stripe, bottom_stripe) → one box
        for i in range(0, len(split) - 1, 2):
            t_y1, t_y2 = split[i]
            b_y1, b_y2 = split[i + 1]
            boxes.append([band_x1, t_y1, band_x2, b_y2])

    return sorted(boxes, key=lambda b: (b[1], b[0]))


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _smart_erase_box(img: Image.Image, box: list, threshold: int = 80, passes: int = 4):
    """
    Remove baked-in template price text while preserving gradient/glow/texture.

    Alternates horizontal and vertical interpolation passes so that text pixels
    surrounded on all sides by other text pixels are still reached.  No fill
    rectangle is drawn.
    """
    x1, y1, x2, y2 = [int(v) for v in box]
    inset = 5
    ix1, iy1 = x1 + inset, y1 + inset
    ix2, iy2 = x2 - inset, y2 - inset
    if ix1 >= ix2 or iy1 >= iy2:
        return

    px    = img.load()
    bright = threshold * 3

    def _is_text(x, y):
        return sum(px[x, y][:3]) > bright

    def _interp(ca, cb, t):
        return (
            int(ca[0] * (1 - t) + cb[0] * t),
            int(ca[1] * (1 - t) + cb[1] * t),
            int(ca[2] * (1 - t) + cb[2] * t),
            255,
        )

    for _ in range(passes):
        # Horizontal pass
        for y in range(iy1, iy2):
            bg = [x for x in range(ix1, ix2) if not _is_text(x, y)]
            if not bg:
                continue
            for x in range(ix1, ix2):
                if not _is_text(x, y):
                    continue
                lx = next((b for b in reversed(bg) if b < x), None)
                rx = next((b for b in bg           if b > x), None)
                if lx and rx:
                    px[x, y] = _interp(px[lx, y], px[rx, y], (x - lx) / (rx - lx))
                elif lx:
                    px[x, y] = (*px[lx, y][:3], 255)
                elif rx:
                    px[x, y] = (*px[rx, y][:3], 255)

        # Vertical pass
        for x in range(ix1, ix2):
            bg = [y for y in range(iy1, iy2) if not _is_text(x, y)]
            if not bg:
                continue
            for y in range(iy1, iy2):
                if not _is_text(x, y):
                    continue
                ly = next((b for b in reversed(bg) if b < y), None)
                ry = next((b for b in bg           if b > y), None)
                if ly and ry:
                    px[x, y] = _interp(px[x, ly], px[x, ry], (y - ly) / (ry - ly))
                elif ly:
                    px[x, y] = (*px[x, ly][:3], 255)
                elif ry:
                    px[x, y] = (*px[x, ry][:3], 255)


def _write_centered(draw: ImageDraw.ImageDraw, box: list, text: str,
                    color=(255, 255, 255, 255)):
    """Write text centred inside box at auto-fitted font size."""
    x1, y1, x2, y2 = [int(v) for v in box]
    zone_w = x2 - x1
    zone_h = y2 - y1

    font_size = min(80, max(14, int(zone_h * 0.60)))
    font = _load_font(FONT_BOLD_EN, font_size)
    bb   = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]

    while tw > zone_w - 8 and font_size > 14:
        font_size -= 2
        font = _load_font(FONT_BOLD_EN, font_size)
        bb   = draw.textbbox((0, 0), text, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]

    tx = x1 + (zone_w - tw) // 2
    ty = y1 + (zone_h - th) // 2
    draw.text((tx, ty), text, font=font, fill=color,
              stroke_width=2, stroke_fill=(0, 0, 0, 200))
    return font_size


# ── Public API ────────────────────────────────────────────────────────────────

def get_expected_prices(poster_type: str) -> dict:
    """
    Return the exact sell/buy strings the renderer will write.
    Used by QC to know what values to look for in the output image.
    """
    key = poster_type.lower()
    cfg = POSTER_CFG.get(key)
    if not cfg:
        raise ValueError(f"Unknown poster_type: {poster_type}")

    rates    = _load_rates()
    cur_key  = cfg["currency_key"]
    cur_data = rates.get(cur_key, {})
    fmt      = cfg["format"]

    sell_raw = cur_data.get("sell")
    buy_raw  = cur_data.get("buy")

    if sell_raw is not None:
        sell_val = float(sell_raw)
        adj      = 0 if fmt == "decimal" else BUY_ADJ
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
        "poster_type":  key,
        "currency_key": cur_key,
        "sell_str":     sell_str,
        "buy_str":      buy_str,
        "sell_raw":     sell_raw,
        "buy_raw":      buy_raw,
        "buy_adjusted": buy_val,
        "format":       fmt,
    }


def render_prices(
    poster_type: str,
    design_path: Path | None = None,
    toronto_now: datetime | None = None,
) -> Path:
    """
    Detect price cells from the marker template, render live rates onto the
    clean template (or a supplied AI design), and save the result.

    Parameters
    ----------
    poster_type  : "canada" | "usa" | "usacan" | "usdt" | "eur"
    design_path  : optional AI-generated design PNG; overrides the clean template
    toronto_now  : datetime for timestamps (defaults to now in Toronto TZ)

    Returns
    -------
    Path to the saved rendered PNG in rendered_prices/
    """
    RENDERED_DIR.mkdir(parents=True, exist_ok=True)

    key = poster_type.lower()
    cfg = POSTER_CFG.get(key)
    if not cfg:
        raise ValueError(f"Unknown poster_type '{poster_type}'. Valid: {list(POSTER_CFG)}")

    # ── 1. Detect price boxes from marker template ────────────────────────────
    marker_path = MARKER_DIR / cfg["file"]
    if not marker_path.exists():
        raise FileNotFoundError(f"Marker template not found: {marker_path}")

    boxes = _detect_pink_boxes(marker_path)
    if not boxes:
        raise RuntimeError(f"No pink marker boxes detected in {marker_path}")

    sell_count = cfg["sell_count"]
    sell_boxes = boxes[:sell_count]
    buy_boxes  = boxes[sell_count:]
    log.info(f"[renderer] {key.upper()} detected {len(boxes)} box(es) — "
             f"{len(sell_boxes)} sell, {len(buy_boxes)} buy")

    # ── 2. Compute price strings ──────────────────────────────────────────────
    prices   = get_expected_prices(key)
    sell_str = prices["sell_str"]
    buy_str  = prices["buy_str"]
    log.info(f"[renderer] {key.upper()}  sell='{sell_str}'  buy='{buy_str}'")

    # ── 3. Open base image ────────────────────────────────────────────────────
    if design_path and Path(design_path).exists():
        img = Image.open(design_path).convert("RGBA")
        log.info(f"[renderer] Using AI design: {Path(design_path).name}")
    else:
        clean_path = CLEAN_DIR / cfg["file"]
        if not clean_path.exists():
            raise FileNotFoundError(f"Clean template not found: {clean_path}")
        img = Image.open(clean_path).convert("RGBA")
        log.info(f"[renderer] Using clean template: {cfg['file']}")

    draw = ImageDraw.Draw(img)

    # ── 4. Erase placeholders → write new prices ──────────────────────────────
    for box in sell_boxes:
        _smart_erase_box(img, box)
        fs = _write_centered(draw, box, sell_str)
        log.info(f"[renderer]   SELL box={box} font={fs}px")

    for box in buy_boxes:
        _smart_erase_box(img, box)
        fs = _write_centered(draw, box, buy_str)
        log.info(f"[renderer]   BUY  box={box} font={fs}px")

    # Dates are NOT rendered on currency posters — only on the logo/date card.

    # ── 5. Save ───────────────────────────────────────────────────────────────
    toronto_now = toronto_now or datetime.now(TORONTO_TZ)
    ts       = toronto_now.strftime("%Y%m%d_%H%M%S")
    out_path = RENDERED_DIR / f"{key}_{ts}.png"
    img.convert("RGB").save(str(out_path), "PNG")
    log.info(f"[renderer] Saved → {out_path}")
    return out_path
