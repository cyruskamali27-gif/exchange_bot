#!/usr/bin/env python3
"""
poster_generator.py — Deterministic PIL poster generator.
Cyrus Global Exchange  |  1080×1350 PNG

No AI. No image generation models. Pure PIL rendering.

Usage:
    python poster_generator.py config.json output.png
    python poster_generator.py --make-assets          # regenerate background + logo

All pixel positions are in LAYOUT below — change numbers there to adjust.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper

try:
    import jdatetime as _jdt
    _HAS_JDATE = True
except ImportError:
    _HAS_JDATE = False

TORONTO_TZ = ZoneInfo("America/Toronto")
from bidi.algorithm import get_display


# ═══════════════════════════════════════════════════════════════════════════════
# CANVAS
# ═══════════════════════════════════════════════════════════════════════════════
W, H = 1080, 1350


# ═══════════════════════════════════════════════════════════════════════════════
# COLORS  (R, G, B) or (R, G, B, A)
# ═══════════════════════════════════════════════════════════════════════════════
GOLD        = (214, 175,  75)
GOLD_DIM    = (160, 130,  55)
GOLD_GLOW   = ( 80,  62,  18)
WHITE       = (255, 255, 255)
CREAM       = (240, 228, 185)
BG_TOP      = (  8,  12,  24)
BG_BOT      = ( 16,  22,  42)
DECO_LINE   = ( 28,  38,  68)   # subtle background grid lines

# semi-transparent overlays (RGBA)
HEADER_BAND     = (  8,  14,  32, 220)
SELL_HDR_BAND   = ( 10,  60,  90, 200)   # teal-blue
BUY_HDR_BAND    = ( 20,  80,  50, 200)   # teal-green
ROW_LIGHT       = (255, 255, 255,  12)
ROW_DARK        = (  0,   0,   0,  22)
FOOTER_BAND     = (  6,  10,  22, 215)


# ═══════════════════════════════════════════════════════════════════════════════
# LAYOUT  ← every pixel position in one place
# ═══════════════════════════════════════════════════════════════════════════════
#
# Fixed zones:
#   Header:       0 – 185
#   Identity:   185 – 400
#   Sell hdr:   400 – 450
#   Sell rows start at 450;  each row is ROW_H px tall  (max 4)
#   BUY_GAP px gap after last sell row
#   Buy hdr:    BUY_HDR_H px
#   Buy rows:   ROW_H px each  (max 3)
#   Footer:     FOOTER_H px pinned to bottom
#
# Row positions below sell/buy are computed dynamically in generate_poster()
# so the sections sit tight against each other regardless of row count.
#
L = {
    # ── Header (0–185) ────────────────────────────────────────────────────────
    "logo_box":       ( 38,  28, 148, 138),   # (x1, y1, x2, y2)
    "company_xy":     (168,  54),
    "company_sub_xy": (168,  88),
    "company_loc_xy": (168, 116),
    "hdr_band":       (  0,   0, W, 185),
    "hdr_div_y":      185,

    # ── Currency identity (185–400) ───────────────────────────────────────────
    "cur_code_cy": 268,
    "cur_en_cy":   323,
    "cur_fa_cy":   366,
    "id_div_y":    400,

    # ── Section geometry (used to derive all row positions at runtime) ────────
    "sell_hdr_top":  400,   # y where SELL header band starts
    "sell_hdr_h":     50,   # height of SELL / BUY header bands
    "row_h":         100,   # height of each data row
    "section_gap":    30,   # vertical gap between sell block and buy block
    "footer_h":      160,   # reserved height for footer at bottom of canvas

    # ── Column x positions ────────────────────────────────────────────────────
    "cx":       540,
    "label_x":   68,
    "price_rx": 1012,
}


# ═══════════════════════════════════════════════════════════════════════════════
# FONT SIZES
# ═══════════════════════════════════════════════════════════════════════════════
FS = {
    "company":    22,
    "sm":         15,
    "cur_code":   90,
    "cur_en":     26,
    "cur_fa":     26,
    "sec_hdr":    26,
    "label":      22,
    "price":      50,
    "footer":     20,
}


# ═══════════════════════════════════════════════════════════════════════════════
# FONTS
# ═══════════════════════════════════════════════════════════════════════════════
ASSETS = Path(__file__).parent / "assets"

_SYSTEM_FA = Path("/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Bold.ttf")
_SYSTEM_EN_CANDIDATES = [
    Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
]

_font_cache: dict = {}


def _resolve(filename: str) -> Path:
    local = ASSETS / filename
    if local.exists():
        return local
    if filename == "Vazirmatn-Bold.ttf" and _SYSTEM_FA.exists():
        return _SYSTEM_FA
    if filename == "Arial-Bold.ttf":
        for p in _SYSTEM_EN_CANDIDATES:
            if p.exists():
                return p
    raise FileNotFoundError(f"Font '{filename}' not found. Place it in assets/")


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(_resolve(name)), size)
    return _font_cache[key]


def fa(size: int) -> ImageFont.FreeTypeFont:
    """Vazirmatn Bold — Persian / Arabic."""
    return _font("Vazirmatn-Bold.ttf", size)


def en(size: int) -> ImageFont.FreeTypeFont:
    """NotoSans Bold — Latin, digits."""
    return _font("Arial-Bold.ttf", size)


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def persian(text: str) -> str:
    """Reshape + BiDi-reorder Persian for left-to-right PIL rendering."""
    return get_display(arabic_reshaper.reshape(text))


def draw_lm(draw, x, cy, text, fnt, color):
    """Left-aligned, vertically centered at cy."""
    draw.text((x, cy), text, font=fnt, fill=color, anchor="lm")


def draw_rm(draw, rx, cy, text, fnt, color):
    """Right-aligned, vertically centered at cy."""
    draw.text((rx, cy), text, font=fnt, fill=color, anchor="rm")


def draw_mm(draw, cx, cy, text, fnt, color):
    """Horizontally and vertically centered at (cx, cy)."""
    draw.text((cx, cy), text, font=fnt, fill=color, anchor="mm")


def draw_bilingual(draw, cx, cy, ltr: str, rtl: str, fnt_ltr, fnt_rtl, color):
    """
    Draw  'LTR_TEXT  /  RTL_TEXT'  centered at (cx, cy).
    Each part uses its own font; the three segments are measured and
    positioned as one centered group.
    """
    sep = "  /  "
    rtl_shaped = persian(rtl)

    w_ltr = draw.textlength(ltr,        font=fnt_ltr)
    w_sep = draw.textlength(sep,        font=fnt_ltr)
    w_rtl = draw.textlength(rtl_shaped, font=fnt_rtl)

    sx = cx - (w_ltr + w_sep + w_rtl) / 2
    draw.text((sx,                cy), ltr,        font=fnt_ltr, fill=color, anchor="lm")
    draw.text((sx + w_ltr,        cy), sep,        font=fnt_ltr, fill=color, anchor="lm")
    draw.text((sx + w_ltr + w_sep, cy), rtl_shaped, font=fnt_rtl, fill=color, anchor="lm")


# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND
# ═══════════════════════════════════════════════════════════════════════════════

def _make_gradient() -> Image.Image:
    """Dark navy vertical gradient."""
    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    r1, g1, b1 = BG_TOP
    r2, g2, b2 = BG_BOT
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)],
                  fill=(int(r1 + (r2 - r1) * t),
                        int(g1 + (g2 - g1) * t),
                        int(b1 + (b2 - b1) * t)))
    return img


def _add_deco(draw: ImageDraw.ImageDraw) -> None:
    """Subtle diagonal lines — gives depth without distracting."""
    step = 130
    for x in range(-H, W + H, step):
        draw.line([(x, 0), (x + H, H)], fill=DECO_LINE, width=1)


def _build_background() -> Image.Image:
    bg   = _make_gradient()
    draw = ImageDraw.Draw(bg)
    _add_deco(draw)
    return bg


def _load_or_build_bg(currency_code: str) -> Image.Image:
    path = ASSETS / f"background_{currency_code.lower()}.png"
    if path.exists():
        img = Image.open(path).convert("RGBA")
        if img.size != (W, H):
            img = img.resize((W, H), Image.LANCZOS)
        return img
    return _build_background().convert("RGBA")


# ═══════════════════════════════════════════════════════════════════════════════
# LOGO
# ═══════════════════════════════════════════════════════════════════════════════

def _load_or_build_logo(size: int) -> Image.Image:
    path = ASSETS / "logo.png"
    if path.exists():
        img = Image.open(path).convert("RGBA")
        return img.resize((size, size), Image.LANCZOS)
    return _placeholder_logo(size)


def _placeholder_logo(size: int) -> Image.Image:
    """Gold circle monogram — used when no logo.png is present."""
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r    = 3
    draw.ellipse([r, r, size - r - 1, size - r - 1],
                 fill=(18, 26, 50, 230), outline=GOLD, width=4)
    draw.ellipse([12, 12, size - 13, size - 13],
                 fill=None, outline=GOLD_DIM, width=1)
    fnt = fa(size // 2)
    bb  = draw.textbbox((0, 0), "C", font=fnt)
    tx  = (size - (bb[2] - bb[0])) // 2 - bb[0]
    ty  = (size - (bb[3] - bb[1])) // 2 - bb[1]
    draw.text((tx, ty), "C", font=fnt, fill=GOLD)
    return img


# ═══════════════════════════════════════════════════════════════════════════════
# COMPOSITING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _band(canvas: Image.Image, box: tuple, rgba: tuple) -> None:
    """Alpha-composite a solid colored rectangle onto the canvas."""
    x1, y1, x2, y2 = box
    overlay = Image.new("RGBA", (x2 - x1, y2 - y1), rgba)
    canvas.alpha_composite(overlay, dest=(x1, y1))


def _divider(draw, y: int, x1=50, x2=W - 50,
             color=GOLD, alpha=110, width=1) -> None:
    draw.line([(x1, y), (x2, y)], fill=(*color, alpha), width=width)


def _thick_divider(draw, y: int, color=GOLD, alpha=200) -> None:
    draw.line([(40, y), (W - 40, y)], fill=(*color, alpha), width=2)


# ═══════════════════════════════════════════════════════════════════════════════
# POSTER GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

def _section_geometry(n_sell: int, n_buy: int) -> dict:
    """
    Compute all dynamic y positions from row counts.
    Fixed anchor: sell header starts at L["sell_hdr_top"].
    Rows sit tight; empty space is at the bottom before the footer.
    """
    hdr_h     = L["sell_hdr_h"]
    row_h     = L["row_h"]
    gap       = L["section_gap"]
    footer_h  = L["footer_h"]

    sell_top  = L["sell_hdr_top"]          # 400
    sell_rows_top = sell_top + hdr_h       # 450
    sell_bot  = sell_rows_top + n_sell * row_h

    buy_top   = sell_bot + gap
    buy_rows_top = buy_top + hdr_h
    buy_bot   = buy_rows_top + n_buy * row_h

    # Footer: right after buy section, but always at least 30 px of breathing room.
    # If content is dense (4 sell + 3 buy), footer stays near the bottom naturally.
    content_end = buy_bot + 30
    footer_top  = max(content_end, H - footer_h - 20)

    sell_cy   = [sell_rows_top + row_h * i + row_h // 2 for i in range(n_sell)]
    sell_bands = [(0, sell_rows_top + row_h * i, W, sell_rows_top + row_h * (i + 1))
                  for i in range(n_sell)]
    sell_divs  = [sell_rows_top + row_h * (i + 1) for i in range(n_sell - 1)]

    buy_cy    = [buy_rows_top + row_h * i + row_h // 2 for i in range(n_buy)]
    buy_bands  = [(0, buy_rows_top + row_h * i, W, buy_rows_top + row_h * (i + 1))
                  for i in range(n_buy)]
    buy_divs   = [buy_rows_top + row_h * (i + 1) for i in range(n_buy - 1)]

    footer_line = footer_top + 3
    fh          = footer_h - 6             # usable footer height
    phone_cy    = footer_top + fh * 1 // 4 + 10
    tg_cy       = footer_top + fh * 2 // 4 + 14
    loc_cy      = footer_top + fh * 3 // 4 + 18

    return dict(
        sell_top=sell_top, sell_rows_top=sell_rows_top, sell_bot=sell_bot,
        sell_cy=sell_cy, sell_bands=sell_bands, sell_divs=sell_divs,
        buy_top=buy_top, buy_rows_top=buy_rows_top, buy_bot=buy_bot,
        buy_cy=buy_cy, buy_bands=buy_bands, buy_divs=buy_divs,
        footer_top=footer_top, footer_line=footer_line,
        phone_cy=phone_cy, tg_cy=tg_cy, loc_cy=loc_cy,
    )


def generate_poster(cfg: dict, output_path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    code      = cfg.get("currency_code", "CAD").upper()
    sell_rows = cfg.get("sell_rows", [])[:4]
    buy_rows  = cfg.get("buy_rows",  [])[:3]
    g         = _section_geometry(len(sell_rows), len(buy_rows))

    # ── Canvas ────────────────────────────────────────────────────────────────
    canvas = _load_or_build_bg(code)
    draw   = ImageDraw.Draw(canvas, "RGBA")
    cx     = L["cx"]

    # ── HEADER ────────────────────────────────────────────────────────────────
    _band(canvas, L["hdr_band"], HEADER_BAND)

    lx1, ly1, lx2, _ = L["logo_box"]
    logo = _load_or_build_logo(lx2 - lx1)
    canvas.alpha_composite(logo, dest=(lx1, ly1))

    draw_lm(draw, L["company_xy"][0], L["company_xy"][1] + 11,
            cfg.get("company", "CYRUS GLOBAL EXCHANGE"), en(FS["company"]), GOLD)
    draw_lm(draw, L["company_sub_xy"][0], L["company_sub_xy"][1] + 8,
            "Currency Exchange", en(FS["sm"]), (*WHITE, 170))
    if cfg.get("location"):
        draw_lm(draw, L["company_loc_xy"][0], L["company_loc_xy"][1] + 8,
                cfg["location"], en(FS["sm"]), (*GOLD_DIM, 200))
    _thick_divider(draw, L["hdr_div_y"])

    # ── CURRENCY IDENTITY ─────────────────────────────────────────────────────
    draw_mm(draw, cx, L["cur_code_cy"], code, en(FS["cur_code"]), GOLD)
    if cfg.get("currency_title"):
        draw_mm(draw, cx, L["cur_en_cy"], cfg["currency_title"], en(FS["cur_en"]), WHITE)
    if cfg.get("currency_title_fa"):
        draw_mm(draw, cx, L["cur_fa_cy"],
                persian(cfg["currency_title_fa"]), fa(FS["cur_fa"]), CREAM)
    _thick_divider(draw, L["id_div_y"])

    # ── SELL SECTION ──────────────────────────────────────────────────────────
    _band(canvas, (0, g["sell_top"], W, g["sell_rows_top"]), SELL_HDR_BAND)
    draw_bilingual(draw, cx, g["sell_top"] + L["sell_hdr_h"] // 2,
                   "SELL", "فروش", en(FS["sec_hdr"]), fa(FS["sec_hdr"]), GOLD)

    for i, row in enumerate(sell_rows):
        _band(canvas, g["sell_bands"][i], ROW_LIGHT if i % 2 == 0 else ROW_DARK)
        draw_lm(draw, L["label_x"], g["sell_cy"][i], row.get("label", ""), en(FS["label"]), GOLD)
        draw_rm(draw, L["price_rx"], g["sell_cy"][i], row.get("price", ""), en(FS["price"]), WHITE)

    for y_div in g["sell_divs"]:
        _divider(draw, y_div)

    # ── MID DIVIDER ───────────────────────────────────────────────────────────
    _thick_divider(draw, g["buy_top"])

    # ── BUY SECTION ───────────────────────────────────────────────────────────
    _band(canvas, (0, g["buy_top"], W, g["buy_rows_top"]), BUY_HDR_BAND)
    draw_bilingual(draw, cx, g["buy_top"] + L["sell_hdr_h"] // 2,
                   "BUY", "خرید", en(FS["sec_hdr"]), fa(FS["sec_hdr"]), GOLD)

    for i, row in enumerate(buy_rows):
        _band(canvas, g["buy_bands"][i], ROW_LIGHT if i % 2 == 0 else ROW_DARK)
        draw_lm(draw, L["label_x"], g["buy_cy"][i], row.get("label", ""), en(FS["label"]), GOLD)
        draw_rm(draw, L["price_rx"], g["buy_cy"][i], row.get("price", ""), en(FS["price"]), WHITE)

    for y_div in g["buy_divs"]:
        _divider(draw, y_div)

    # ── FOOTER ────────────────────────────────────────────────────────────────
    _band(canvas, (0, g["footer_top"], W, H), FOOTER_BAND)
    _thick_divider(draw, g["footer_line"])

    if cfg.get("phone"):
        draw_mm(draw, cx, g["phone_cy"], f"☎  {cfg['phone']}", en(FS["footer"]), WHITE)
    if cfg.get("telegram"):
        draw_mm(draw, cx, g["tg_cy"], cfg["telegram"], en(FS["footer"]), GOLD)
    if cfg.get("location"):
        draw_mm(draw, cx, g["loc_cy"], cfg["location"], en(FS["footer"]), (*WHITE, 160))

    # ── SAVE ──────────────────────────────────────────────────────────────────
    canvas.convert("RGB").save(str(output_path), "PNG", optimize=True)
    print(f"  Saved → {output_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# DATE COVER POSTER
# ═══════════════════════════════════════════════════════════════════════════════

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

def _shamsi(now: datetime) -> str:
    if _HAS_JDATE:
        jd = _jdt.datetime.fromgregorian(datetime=now)
        s  = f"{jd.year}/{str(jd.month).zfill(2)}/{str(jd.day).zfill(2)}"
        return s.translate(_FA_DIGITS)
    return now.strftime("%Y/%m/%d")


def _gregorian(now: datetime) -> str:
    return now.strftime("%B %d, %Y")


def generate_date_cover(now: datetime | None = None,
                        output_path=None) -> Image.Image:
    """
    Date-only cover poster: Shamsi + Gregorian date on dark background.
    Same visual language as currency posters — no price rows.
    """
    if now is None:
        now = datetime.now(TORONTO_TZ)

    canvas = _build_background().convert("RGBA")
    draw   = ImageDraw.Draw(canvas, "RGBA")
    cx     = W // 2

    # ── Header ────────────────────────────────────────────────────────────────
    _band(canvas, L["hdr_band"], HEADER_BAND)
    lx1, ly1, lx2, _ = L["logo_box"]
    logo = _load_or_build_logo(lx2 - lx1)
    canvas.alpha_composite(logo, dest=(lx1, ly1))
    draw_lm(draw, L["company_xy"][0], L["company_xy"][1] + 11,
            "CYRUS GLOBAL EXCHANGE", en(FS["company"]), GOLD)
    draw_lm(draw, L["company_sub_xy"][0], L["company_sub_xy"][1] + 8,
            "Currency Exchange", en(FS["sm"]), (*WHITE, 170))
    _thick_divider(draw, L["hdr_div_y"])

    # ── Date block — centered in the space below the header ───────────────────
    date_cy = 700   # visual center of the date area

    draw_mm(draw, cx, date_cy - 230, "TODAY'S RATES", en(32), (*GOLD, 200))
    _thick_divider(draw, date_cy - 195)

    # Shamsi date (large, Persian numerals + Vazirmatn)
    draw_mm(draw, cx, date_cy - 90,
            persian(_shamsi(now)), fa(80), CREAM)

    # Gregorian date
    draw_mm(draw, cx, date_cy + 30, _gregorian(now), en(36), WHITE)

    _thick_divider(draw, date_cy + 80)

    # ── Footer ────────────────────────────────────────────────────────────────
    footer_y = H - 160
    _band(canvas, (0, footer_y, W, H), FOOTER_BAND)
    _thick_divider(draw, footer_y + 3)
    draw_mm(draw, cx, footer_y + 45, "☎  226-962-7729",       en(FS["footer"]), WHITE)
    draw_mm(draw, cx, footer_y + 90, "@cyrusGlobalExchange",  en(FS["footer"]), GOLD)
    draw_mm(draw, cx, footer_y + 132, "Guelph, Ontario",      en(FS["footer"]), (*WHITE, 160))

    img = canvas.convert("RGB")
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(str(output_path), "PNG", optimize=True)
    return img


# ═══════════════════════════════════════════════════════════════════════════════
# ASSET BUILDER  (python poster_generator.py --make-assets)
# ═══════════════════════════════════════════════════════════════════════════════

def make_assets() -> None:
    ASSETS.mkdir(exist_ok=True)

    for code in ("cad", "usd", "eur", "usdt", "usacan"):
        path = ASSETS / f"background_{code}.png"
        bg = _build_background()
        bg.save(str(path), "PNG")
        print(f"  Built {path.name}")

    logo_path = ASSETS / "logo.png"
    _placeholder_logo(110).save(str(logo_path), "PNG")
    print(f"  Built {logo_path.name}")

    for fname, src in [
        ("Vazirmatn-Bold.ttf", _SYSTEM_FA),
        ("Arial-Bold.ttf",     _SYSTEM_EN_CANDIDATES[0]),
    ]:
        dst = ASSETS / fname
        if not dst.exists() and src.exists():
            import shutil
            shutil.copy(src, dst)
            print(f"  Copied {fname}")

    print("\nDone. Replace assets/logo.png and assets/background_*.png with real artwork.")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--make-assets" in sys.argv:
        make_assets()
        sys.exit(0)

    if len(sys.argv) < 3:
        print("Usage: python poster_generator.py config.json output.png")
        print("       python poster_generator.py --make-assets")
        sys.exit(1)

    cfg_path = Path(sys.argv[1])
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}")
        sys.exit(1)

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    generate_poster(cfg, sys.argv[2])
