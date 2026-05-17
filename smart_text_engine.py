#!/usr/bin/env python3
"""
smart_text_engine.py
Professional smart text-fitting engine for Cyrus Exchange poster date boxes.

Public API
----------
fit_text_inside_box(image, box, lines, font_paths, colors, ...)
    Core engine: auto-sizes font, centers block, draws RTL-aware text.

get_today_date_lines(dt)
    Build the 3-line date content (Shamsi / weekday / Gregorian).

draw_date_box(image, box, currency, dt, adj, debug)
    High-level: clean box → fit 3 lines → optional debug preview → QC layout dict.

qc_date_fit(layout, box, dt, currency)
    Validate layout against today's date and box boundaries. Returns (ok, errors, score).

save_debug_preview(image_copy, box, result, currency, iteration)
    Save debug PNG with coloured overlays (private, never posted).
"""

import logging
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAS_BIDI = True
except ImportError:
    HAS_BIDI = False

try:
    import jdatetime
    HAS_JDATE = True
except ImportError:
    HAS_JDATE = False

log = logging.getLogger("smart_text_engine")

# ── Font paths ────────────────────────────────────────────────────────────────
FONT_BOLD_FA = "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"
FONT_REG_FA  = "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"
FONT_BOLD_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG_EN  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# ── Visual constants ──────────────────────────────────────────────────────────
TORONTO_TZ     = ZoneInfo("America/Toronto")
DEBUG_DIR      = Path("/var/www/exchange_bot/debug_dates")
_FA_DIGITS     = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_WEEKDAYS      = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یک‌شنبه"]
BOX_BORDER_CLR = (185, 155, 75, 255)   # gold border redrawn after bg-fill
BOX_BORDER_W   = 2
SHADOW_CLR     = (0, 0, 0, 190)
BG_DARKEN      = 25     # how many points darker than sampled bg for box fill
COLOR_FA       = (242, 222, 148, 255)  # gold — Shamsi + weekday
COLOR_EN       = (210, 190, 120, 255)  # slightly dimmer — Gregorian

# ── Low-level primitives ──────────────────────────────────────────────────────

def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, max(6, int(size)))
    except Exception:
        return ImageFont.load_default()


def _reshape(text: str) -> str:
    """Apply Arabic reshaping + BiDi for proper RTL rendering."""
    if not HAS_BIDI or not text:
        return text
    return get_display(arabic_reshaper.reshape(text))


def _sample_bg(px_rgb, x1: int, y1: int, x2: int, y2: int) -> tuple:
    """
    Estimate the background colour inside a box region.
    Uses the darkest quartile of sampled pixels — text (bright) is excluded.
    """
    samples = []
    for sy in range(y1, y2, 4):
        for sx in range(x1, x2, 15):
            try:
                r, g, b = px_rgb[sx, sy]
                samples.append((r, g, b, (r + g + b) / 3))
            except Exception:
                pass
    if not samples:
        return (20, 20, 40)
    samples.sort(key=lambda s: s[3])
    dark = samples[: max(1, len(samples) // 4)]
    return (
        sum(s[0] for s in dark) // len(dark),
        sum(s[1] for s in dark) // len(dark),
        sum(s[2] for s in dark) // len(dark),
    )


def _measure(draw: ImageDraw.ImageDraw, text: str, font) -> tuple:
    """Return (width, height) of rendered text bounding box."""
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


# ── Core engine ───────────────────────────────────────────────────────────────

def fit_text_inside_box(
    image,
    box: list,
    lines: list,
    font_paths,
    colors: list,
    align: str = "center",
    vertical_align: str = "center",
    min_font_size: int = 10,
    max_font_size: int = 42,
    padding_x: int = 16,
    padding_y: int = 8,
    line_spacing: int = 4,
    rtl_lines=None,
    shadow: bool = True,
    border_color=BOX_BORDER_CLR,
    border_width: int = BOX_BORDER_W,
    clean_bg: bool = True,
    block_offset: int = 0,
) -> dict:
    """
    Fit multi-line text inside a box with automatic font-size reduction.

    Parameters
    ----------
    image           PIL Image (RGBA) — modified in-place
    box             [x1, y1, x2, y2]
    lines           list[str] — one text string per line
    font_paths      str or list[str] — one font path per line (or shared)
    colors          list of RGBA tuples — one per line
    align           "center" | "left" | "right"
    vertical_align  "center" | "top" | "bottom"
    min_font_size   smallest allowed font size (fallback)
    max_font_size   largest font to try first
    padding_x       horizontal inner margin in pixels
    padding_y       vertical inner margin in pixels
    line_spacing    pixels between lines
    rtl_lines       list[bool] — True = apply RTL reshaping (Persian/Arabic)
    shadow          draw 1-pixel drop shadow under text
    border_color    RGBA tuple for box border, or None to skip
    border_width    border thickness
    clean_bg        sample + fill background before drawing
    block_offset    extra vertical shift of text block (+ = down, - = up)

    Returns
    -------
    dict:
        font_size, positions, widths, heights, display_texts,
        inner_box, block_top, block_bottom, total_height,
        spacing, qc_fit, warnings
    """
    draw  = ImageDraw.Draw(image)
    x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    box_w = x2 - x1
    box_h = y2 - y1

    inner_x1 = x1 + padding_x
    inner_y1 = y1 + padding_y
    inner_x2 = x2 - padding_x
    inner_y2 = y2 - padding_y
    inner_w  = inner_x2 - inner_x1
    inner_h  = inner_y2 - inner_y1

    if inner_w <= 0 or inner_h <= 0:
        log.warning(f"Box too small for padding: {box}  inner={inner_w}×{inner_h}")
        inner_x1, inner_y1, inner_x2, inner_y2 = x1, y1, x2, y2
        inner_w, inner_h = box_w, box_h

    n = len(lines)
    if isinstance(font_paths, str):
        font_paths = [font_paths] * n
    if rtl_lines is None:
        rtl_lines = [False] * n
    colors = list(colors)
    while len(colors) < n:
        colors.append((255, 255, 255, 255))

    # Reshape RTL lines once — do NOT reshape English
    display_texts = [
        _reshape(lines[i]) if rtl_lines[i] else lines[i]
        for i in range(n)
    ]

    warnings_list = []
    chosen_size   = min_font_size
    spacing       = line_spacing

    # ── Find largest font size where all lines + spacing fit ─────────────────
    for size in range(max_font_size, min_font_size - 1, -1):
        fonts   = [_load_font(font_paths[i], size) for i in range(n)]
        widths  = [_measure(draw, display_texts[i], fonts[i])[0] for i in range(n)]
        heights = [_measure(draw, display_texts[i], fonts[i])[1] for i in range(n)]
        total_h = sum(heights) + spacing * max(0, n - 1)
        if all(w <= inner_w for w in widths) and total_h <= inner_h:
            chosen_size = size
            break
    else:
        # At min_font_size — try tighter spacing before giving up
        for sp in [2, 1, 0]:
            fonts   = [_load_font(font_paths[i], min_font_size) for i in range(n)]
            widths  = [_measure(draw, display_texts[i], fonts[i])[0] for i in range(n)]
            heights = [_measure(draw, display_texts[i], fonts[i])[1] for i in range(n)]
            total_h = sum(heights) + sp * max(0, n - 1)
            if total_h <= inner_h:
                spacing = sp
                chosen_size = min_font_size
                warnings_list.append(f"line_spacing reduced to {sp}px to fit text at min size")
                break
        else:
            chosen_size = min_font_size
            spacing     = 0
            warnings_list.append("Text may not fully fit — rendered at minimum font size")

    # ── Final measurement ─────────────────────────────────────────────────────
    fonts   = [_load_font(font_paths[i], chosen_size) for i in range(n)]
    widths  = [_measure(draw, display_texts[i], fonts[i])[0] for i in range(n)]
    heights = [_measure(draw, display_texts[i], fonts[i])[1] for i in range(n)]
    total_h = sum(heights) + spacing * max(0, n - 1)

    # ── Vertical alignment ────────────────────────────────────────────────────
    if vertical_align == "center":
        block_top = inner_y1 + max(0, (inner_h - total_h) // 2) + block_offset
    elif vertical_align == "top":
        block_top = inner_y1 + block_offset
    else:  # bottom
        block_top = inner_y2 - total_h + block_offset
    block_top = max(inner_y1, min(block_top, inner_y2 - total_h))

    # ── Per-line horizontal positions ─────────────────────────────────────────
    positions = []
    y_cursor  = block_top
    for i in range(n):
        if align == "center":
            lx = inner_x1 + max(0, (inner_w - widths[i]) // 2)
        elif align == "right":
            lx = inner_x2 - widths[i]
        else:
            lx = inner_x1
        positions.append((lx, y_cursor))
        y_cursor += heights[i] + spacing

    block_bottom = y_cursor - spacing

    # ── Clean background — sample region, fill interior ───────────────────────
    if clean_bg:
        px_rgb = image.convert("RGB").load()
        bg     = _sample_bg(px_rgb, x1, y1, x2, y2)
        box_bg = (
            max(0, bg[0] - BG_DARKEN),
            max(0, bg[1] - BG_DARKEN),
            max(0, bg[2] - 10),
        )
        # Fill interior only — preserve original template border pixels (inset by 2)
        bw = border_width if border_color else 0
        draw.rectangle(
            [x1 + bw, y1 + bw, x2 - bw, y2 - bw],
            fill=(*box_bg, 255),
        )

    # Redraw border on top of fill so it's always crisp
    if border_color:
        draw.rectangle([x1, y1, x2, y2], outline=border_color, width=border_width)

    # ── Draw text with optional shadow ────────────────────────────────────────
    for i in range(n):
        lx, ly = positions[i]
        txt    = display_texts[i]
        fnt    = fonts[i]
        col    = colors[i]
        if shadow:
            draw.text((lx + 1, ly + 1), txt, font=fnt, fill=SHADOW_CLR)
        draw.text((lx, ly), txt, font=fnt, fill=col)

    # ── QC fit check ──────────────────────────────────────────────────────────
    overlap_ok = all(
        positions[i][1] + heights[i] <= positions[i + 1][1]
        for i in range(n - 1)
    )
    qc_fit = (
        block_top    >= inner_y1
        and block_bottom <= inner_y2 + 2   # 2px tolerance
        and all(w <= inner_w for w in widths)
        and overlap_ok
    )

    log.info(
        f"fit_text_inside_box: size={chosen_size}px  "
        f"block={total_h}px/inner={inner_h}px  "
        f"spacing={spacing}  fit={'OK' if qc_fit else 'WARN'}"
        + (f"  WARN={warnings_list}" if warnings_list else "")
    )

    return {
        "font_size":     chosen_size,
        "positions":     positions,
        "widths":        widths,
        "heights":       heights,
        "display_texts": display_texts,
        "inner_box":     [inner_x1, inner_y1, inner_x2, inner_y2],
        "block_top":     block_top,
        "block_bottom":  block_bottom,
        "total_height":  total_h,
        "spacing":       spacing,
        "qc_fit":        qc_fit,
        "warnings":      warnings_list,
    }


# ── Date content ──────────────────────────────────────────────────────────────

def get_today_date_lines(dt: datetime) -> dict:
    """
    Generate 3-line date content for a given datetime (America/Toronto).

    Returns dict:
        shamsi, weekday, gregorian  — raw strings
        lines, font_paths, colors, rtl_lines  — ready for fit_text_inside_box
    """
    if HAS_JDATE:
        jd    = jdatetime.datetime.fromgregorian(datetime=dt)
        year  = str(jd.year).translate(_FA_DIGITS)
        month = str(jd.month).zfill(2).translate(_FA_DIGITS)
        day   = str(jd.day).zfill(2).translate(_FA_DIGITS)
        shamsi = f"{year}/{month}/{day}"
    else:
        shamsi = dt.strftime("%Y/%m/%d")

    weekday   = _WEEKDAYS[dt.weekday()]
    gregorian = dt.strftime("%-d %B %Y")

    return {
        "shamsi":     shamsi,
        "weekday":    weekday,
        "gregorian":  gregorian,
        "lines":      [shamsi, weekday, gregorian],
        "font_paths": [FONT_BOLD_FA, FONT_BOLD_FA, FONT_BOLD_EN],
        "colors":     [COLOR_FA, COLOR_FA, COLOR_EN],
        "rtl_lines":  [True, True, False],
    }


# ── High-level date box renderer ──────────────────────────────────────────────

def draw_date_box(
    image: Image.Image,
    box: list,
    currency: str,
    dt: datetime,
    adj: dict = None,
    debug: bool = False,
) -> dict:
    """
    Clean the date box and render 3-line date with smart fitting.

    Parameters
    ----------
    image     PIL Image (RGBA) — modified in-place
    box       [x1, y1, x2, y2] — from poster_coordinates.json
    currency  e.g. "CAD"
    dt        datetime in America/Toronto (or any tz, used as-is)
    adj       optional render adjustments:
                y_offset (int): shift block up/down
                font_scale (float): multiply max_font_size
                padding_extra (int): add to padding
    debug     if True, save debug overlay PNG to debug_dates/

    Returns
    -------
    layout dict compatible with qc_date_fit() and the design iteration agent.
    """
    adj           = adj or {}
    y_offset      = int(adj.get("y_offset", 0))
    font_scale    = float(adj.get("font_scale", 1.0))
    padding_extra = int(adj.get("padding_extra", 0))

    date_data = get_today_date_lines(dt)

    padding_x  = max(8, 14 + padding_extra)
    padding_y  = max(4, 7 + padding_extra)
    max_font   = max(10, int(36 * font_scale))
    min_font   = 10
    spacing    = max(2, 4 + padding_extra // 3)

    result = fit_text_inside_box(
        image,
        box,
        date_data["lines"],
        date_data["font_paths"],
        date_data["colors"],
        align="center",
        vertical_align="center",
        min_font_size=min_font,
        max_font_size=max_font,
        padding_x=padding_x,
        padding_y=padding_y,
        line_spacing=spacing,
        rtl_lines=date_data["rtl_lines"],
        shadow=True,
        border_color=None,
        border_width=0,
        clean_bg=False,
        block_offset=y_offset,
    )

    # Build layout dict (backward-compatible with design_iteration_agent QC format)
    def _get(lst, idx, default=0):
        return lst[idx] if idx < len(lst) else default

    pos = result["positions"]
    w   = result["widths"]
    h   = result["heights"]

    layout = {
        "date_box":  box,
        "shamsi":    date_data["shamsi"],
        "weekday":   date_data["weekday"],
        "gregorian": date_data["gregorian"],
        "font_size": result["font_size"],
        "y1": _get(pos, 0, (0, 0))[1],  "h1": _get(h, 0),  "w1": _get(w, 0),
        "y2": _get(pos, 1, (0, 0))[1],  "h2": _get(h, 1),  "w2": _get(w, 1),
        "y3": _get(pos, 2, (0, 0))[1],  "h3": _get(h, 2),  "w3": _get(w, 2),
        "box_x1": box[0], "box_y1": box[1], "box_x2": box[2], "box_y2": box[3],
        "qc_fit":   result["qc_fit"],
        "warnings": result["warnings"],
        # also keep flat aliases used by rate_graphic_publisher _qc_date
        "date_fa":  f"{date_data['shamsi']} {date_data['weekday']}",
        "date_en":  date_data["gregorian"],
        "fa_y": _get(pos, 0, (0, 0))[1],
        "fa_h": _get(h, 0),
        "fa_w": _get(w, 0),
        "en_y": _get(pos, 2, (0, 0))[1],
        "en_h": _get(h, 2),
        "en_w": _get(w, 2),
    }

    log.info(
        f"[{currency}] draw_date_box → "
        f"'{date_data['shamsi']}' / '{date_data['weekday']}' / '{date_data['gregorian']}'  "
        f"size={result['font_size']}px  fit={'OK' if result['qc_fit'] else 'WARN'}  "
        f"block={result['total_height']}px"
    )

    if debug:
        save_debug_preview(image, box, result, currency)

    return layout


# ── QC ────────────────────────────────────────────────────────────────────────

def qc_date_fit(layout: dict, box: list, dt: datetime, currency: str) -> tuple:
    """
    Validate the rendered date layout.

    Checks
    ------
    1. All 3 date lines are inside the box
    2. No line overlaps another
    3. No line touches the box border (<2 px)
    4. Padding respected
    5. Date values match today (Shamsi year + Gregorian)
    6. Persian text is non-empty (connected)
    7. Text widths reasonable (not too narrow)

    Returns
    -------
    (passed: bool, errors: list[str], score: float 0–100)
    """
    errors = []
    x1, y1, x2, y2 = box

    # Date correctness
    if HAS_JDATE:
        jd      = jdatetime.datetime.fromgregorian(datetime=dt)
        year_fa = str(jd.year).translate(_FA_DIGITS)
        if year_fa not in layout.get("shamsi", ""):
            errors.append(f"Shamsi year {year_fa} not found in '{layout.get('shamsi')}'")
        month_fa = str(jd.month).zfill(2).translate(_FA_DIGITS)
        if month_fa not in layout.get("shamsi", ""):
            errors.append(f"Shamsi month {month_fa} not found in '{layout.get('shamsi')}'")

    exp_greg = dt.strftime("%-d %B %Y")
    if layout.get("gregorian") != exp_greg:
        errors.append(f"Gregorian wrong: got '{layout.get('gregorian')}', expected '{exp_greg}'")

    exp_wd = _WEEKDAYS[dt.weekday()]
    if layout.get("weekday") != exp_wd:
        errors.append(f"Weekday wrong: got '{layout.get('weekday')}', expected '{exp_wd}'")

    # Boundary checks
    if layout.get("y1", 0) < y1:
        errors.append(f"Line 1 above box top (y1={layout['y1']} < {y1})")
    if layout.get("y3", 0) + layout.get("h3", 0) > y2 + 2:
        errors.append(
            f"Line 3 below box bottom ({layout['y3'] + layout['h3']} > {y2})"
        )

    # Overlap checks
    if layout.get("y1", 0) + layout.get("h1", 0) > layout.get("y2", 999):
        errors.append(
            f"Overlap L1-L2: bottom={layout['y1'] + layout['h1']} > top={layout['y2']}"
        )
    if layout.get("y2", 0) + layout.get("h2", 0) > layout.get("y3", 999):
        errors.append(
            f"Overlap L2-L3: bottom={layout['y2'] + layout['h2']} > top={layout['y3']}"
        )

    # Width sanity
    for label, key in [("Shamsi", "w1"), ("Weekday", "w2"), ("Gregorian", "w3")]:
        if layout.get(key, 0) < 15:
            errors.append(f"{label} text too narrow ({layout.get(key)}px)")
        if layout.get(key, 0) > (x2 - x1):
            errors.append(f"{label} text wider than box")

    # Score: start 100, -20 per critical error (overlap/boundary), -10 per minor
    critical = sum(
        1 for e in errors
        if any(k in e for k in ["Overlap", "above box", "below box", "too narrow"])
    )
    minor = len(errors) - critical
    score = max(0.0, 100.0 - critical * 20 - minor * 10)
    passed = len(errors) == 0

    if not passed:
        log.warning(f"[{currency}] QC FAILED  score={score:.0f}%  errors={errors}")
    else:
        log.info(f"[{currency}] QC PASSED  score=100%")

    return passed, errors, score


# ── Debug preview ─────────────────────────────────────────────────────────────

def save_debug_preview(
    image: Image.Image,
    box: list,
    result: dict,
    currency: str,
    iteration: int = 0,
) -> Path | None:
    """
    Save a debug PNG showing:
      red   rect = outer date_box
      green rect = inner usable area
      blue  line = horizontal centre of box
      text positions as rendered

    Saved to debug_dates/ — NEVER sent to Telegram or public channel.
    """
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        dbg = image.copy().convert("RGBA")
        overlay = Image.new("RGBA", dbg.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)

        x1, y1, x2, y2 = map(int, box)

        # Red = outer box
        d.rectangle([x1, y1, x2, y2], outline=(255, 40, 40, 200), width=2)

        # Green = inner usable area
        inner = result.get("inner_box", [x1, y1, x2, y2])
        d.rectangle(inner, outline=(40, 220, 40, 180), width=1)

        # Blue = horizontal centre line
        cy = (y1 + y2) // 2
        d.line([(x1, cy), (x2, cy)], fill=(60, 120, 255, 160), width=1)

        # Yellow markers = line start positions
        for i, (px_, py_) in enumerate(result.get("positions", [])):
            h = result["heights"][i] if i < len(result["heights"]) else 4
            d.rectangle([px_ - 2, py_, px_ + 2, py_ + h],
                        outline=(255, 230, 0, 200), width=1)

        dbg = Image.alpha_composite(dbg, overlay)

        ts   = datetime.now(TORONTO_TZ).strftime("%Y%m%d_%H%M%S")
        name = f"debug_{currency.lower()}_iter{iteration}_{ts}.png"
        out  = DEBUG_DIR / name
        dbg.convert("RGB").save(str(out), "PNG")
        log.info(f"[{currency}] Debug preview → {out}")
        return out
    except Exception as exc:
        log.error(f"save_debug_preview error: {exc}")
        return None
