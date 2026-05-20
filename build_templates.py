#!/usr/bin/env python3
"""
build_templates.py — Cyrus Exchange  /  Generate all poster templates from scratch.

Outputs:
  templates_clean/{canada,usa,usdt,eur,usacan}.png
  price_zones.json   ← used by rate_graphic_publisher.py for price rendering

Design rules:
  • Dark crimson premium background
  • Gold accents, borders, separators
  • Price safe-zones: flat solid colour, zero texture, zero overlay
  • All text via Vazirmatn Bold; Persian shaped via arabic_reshaper + bidi
  • All prices rendered at (cx, cy) of their safe-zone — fully automatic
"""

from PIL import Image, ImageDraw, ImageFont
import json
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display

# ── Helpers ────────────────────────────────────────────────────────────────────

FONT_BOLD  = "/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Bold.ttf"
FONT_EXTRA = "/usr/share/fonts/truetype/vazirmatn/Vazirmatn-ExtraBold.ttf"

def F(size, extra=False):
    try:
        return ImageFont.truetype(FONT_EXTRA if extra else FONT_BOLD, size)
    except Exception:
        return ImageFont.load_default()

def fa(text):
    return get_display(arabic_reshaper.reshape(text))

def center_text(draw, cx, cy, text, font, fill):
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text((cx - tw // 2, cy - th // 2), text, font=font, fill=fill)

# ── Palette ────────────────────────────────────────────────────────────────────

BG        = ( 9,  3,  3)
HDR_BG    = (14,  5,  5)
ROW_A     = (11,  4,  4)
ROW_B     = (17,  7,  7)
SAFE      = (22,  9,  9)       # price zone — absolutely clean solid colour
GOLD      = (188, 153, 62)
GOLD2     = (220, 190, 98)
GOLD_DIM  = ( 85,  68, 28)
WHITE     = (255, 255, 255)
LABEL_FA  = (180, 147, 64)
LABEL_EN  = (128, 105, 50)
DIVIDER   = ( 32,  13, 13)

# ── Layout ─────────────────────────────────────────────────────────────────────

W          = 1200
POSTER_H   = 900
HDR_H      = 168
FOOT_H     = 78
CONTENT_H  = POSTER_H - HDR_H - FOOT_H   # 654

LABEL_W    = 700     # label column width
PRICE_X    = 724     # left edge of price safe-zone  (= LABEL_W + 24 separator gap)
PRICE_PAD  = 24      # gap from right edge

# ── Currency definitions ────────────────────────────────────────────────────────

CURRENCIES = {
    "CAD": {
        "name_en": "CANADIAN DOLLAR RATE",
        "name_fa": "نرخ دلار کانادا",
        "fname":   "canada.png",
        "sell_count": 3,
        "rows": [
            ("فروش نقدی",    "Cash Sell",          "sell"),
            ("فروش چک",      "Left Cheque",         "sell_cheque"),
            ("آی ترنسفر",    "E-Transfer",          "sell_etransfer"),
            ("خرید نقدی",    "Cash Buy",            "buy"),
            ("خرید مستقیم",  "Cash Buy Direct",     "buy_direct"),
        ],
    },
    "USD": {
        "name_en": "US DOLLAR RATE",
        "name_fa": "نرخ دلار آمریکا",
        "fname":   "usa.png",
        "sell_count": 3,
        "rows": [
            ("فروش نقدی",      "Cash Sell",          "sell"),
            ("فروش داخل USA",  "Sell Inside USA",    "sell_inside_usa"),
            ("آی ترنسفر",      "E-Transfer",         "sell_etransfer"),
            ("خرید نقدی",      "Cash Buy",           "buy"),
            ("خرید مستقیم",    "Buy Direct",         "buy_direct"),
        ],
    },
    "USDT": {
        "name_en": "USDT  ·  TETHER RATE",
        "name_fa": "نرخ تتر",
        "fname":   "usdt.png",
        "sell_count": 1,
        "rows": [
            ("فروش",  "Sell",  "sell"),
            ("خرید",  "Buy",   "buy"),
        ],
    },
    "EUR": {
        "name_en": "EURO RATE  ·  EUR / CAD",
        "name_fa": "نرخ یورو",
        "fname":   "eur.png",
        "sell_count": 1,
        "rows": [
            ("فروش EUR/CAD",  "Sell EUR / CAD",  "sell"),
            ("خرید EUR/CAD",  "Buy EUR / CAD",   "buy"),
        ],
    },
    "USACAN": {
        "name_en": "USD / CAD RATE",
        "name_fa": "نرخ USD/CAD",
        "fname":   "usacan.png",
        "sell_count": 1,
        "rows": [
            ("فروش",  "Sell USD / CAD",  "sell"),
            ("خرید",  "Buy USD / CAD",   "buy"),
        ],
    },
}


# ── Poster builder ──────────────────────────────────────────────────────────────

def build_poster(cur: str, cfg: dict, out_dir: Path, all_zones: dict) -> None:
    rows    = cfg["rows"]
    n_rows  = len(rows)
    row_h   = CONTENT_H // n_rows

    img  = Image.new("RGB", (W, POSTER_H), BG)
    draw = ImageDraw.Draw(img)

    # ── Background subtle vertical vignette ──────────────────────────────────
    for x in range(W):
        t = abs(x - W / 2) / (W / 2)
        shade = int(4 * t)
        r, g, b = max(0, BG[0] - shade), BG[1], BG[2]
        draw.line([(x, 0), (x, POSTER_H)], fill=(r, g, b))

    # ── Header ───────────────────────────────────────────────────────────────
    draw.rectangle([0, 0, W, HDR_H], fill=HDR_BG)
    draw.rectangle([0, HDR_H - 4, W, HDR_H], fill=GOLD)   # bottom gold bar

    # Logo badge (circle)
    lx, ly, lr = 86, HDR_H // 2, 56
    draw.ellipse([lx-lr, ly-lr, lx+lr, ly+lr], outline=GOLD,  width=4)
    draw.ellipse([lx-lr+8, ly-lr+8, lx+lr-8, ly+lr-8], outline=GOLD2, width=1)
    center_text(draw, lx, ly - 10, "GE",    F(34, extra=True), GOLD2)
    center_text(draw, lx, ly + 20, "CYRUS", F(13),             GOLD)

    # Currency title
    center_text(draw, W // 2, HDR_H // 2 - 30, cfg["name_en"],         F(46, extra=True), WHITE)
    center_text(draw, W // 2, HDR_H // 2 + 22, fa(cfg["name_fa"]),     F(32),             GOLD)
    center_text(draw, W // 2, HDR_H       - 18, "★  ★  ★",             F(17),             GOLD2)

    # ── Price rows ────────────────────────────────────────────────────────────
    zones = []
    for i, (label_fa, label_en, key) in enumerate(rows):
        y1  = HDR_H + i * row_h
        y2  = y1 + row_h
        row_mid = (y1 + y2) // 2

        # Row background
        draw.rectangle([0, y1, W, y2], fill=ROW_A if i % 2 == 0 else ROW_B)

        # Gold divider between sell section and buy section
        if i == cfg["sell_count"]:
            draw.rectangle([0, y1, W, y1 + 4], fill=GOLD)

        # Row bottom hairline
        draw.rectangle([0, y2 - 1, W, y2], fill=DIVIDER)

        # ── Price safe-zone (CLEAN, no texture, no gradient) ─────────────────
        pz_x1 = PRICE_X
        pz_x2 = W - PRICE_PAD
        pz_y1 = y1 + 10
        pz_y2 = y2 - 10
        draw.rectangle([pz_x1, pz_y1, pz_x2, pz_y2], fill=SAFE)

        # Vertical gold separator
        draw.rectangle([PRICE_X - 4, y1 + 10, PRICE_X - 2, y2 - 10], fill=GOLD)

        # Label column: Persian + English
        lbl_cx = LABEL_W // 2
        center_text(draw, lbl_cx, row_mid - 17, fa(label_fa), F(32),     LABEL_FA)
        center_text(draw, lbl_cx, row_mid + 18, label_en,     F(22),     LABEL_EN)

        # Record price zone
        price_cx = (pz_x1 + pz_x2) // 2
        price_cy = (pz_y1 + pz_y2) // 2
        zones.append({
            "key": key,
            "cx":  price_cx,
            "cy":  price_cy,
            "box": [pz_x1, pz_y1, pz_x2, pz_y2],
        })

    # ── Footer ────────────────────────────────────────────────────────────────
    fy = POSTER_H - FOOT_H
    draw.rectangle([0, fy, W, POSTER_H], fill=HDR_BG)
    draw.rectangle([0, fy, W, fy + 4],   fill=GOLD)

    fc = fy + FOOT_H // 2
    center_text(draw, W * 1 // 4, fc, "2269627729",      F(23), GOLD)
    center_text(draw, W * 2 // 4, fc, "@Cyrus.Exchange", F(23), GOLD2)
    center_text(draw, W * 3 // 4, fc, "Guelph, Canada",  F(23), GOLD)

    # ── Save ─────────────────────────────────────────────────────────────────
    img.save(str(out_dir / cfg["fname"]), "PNG")
    all_zones[cur] = zones
    print(f"  {cur:8s}  {cfg['fname']}  ({W}x{POSTER_H})  {n_rows} rows")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    out_dir    = Path("/var/www/exchange_bot/templates_clean")
    zones_path = Path("/var/www/exchange_bot/price_zones.json")

    all_zones: dict = {}
    print("Building templates...")
    for cur, cfg in CURRENCIES.items():
        build_poster(cur, cfg, out_dir, all_zones)

    zones_path.write_text(json.dumps(all_zones, indent=2, ensure_ascii=False))
    print(f"\nSaved price_zones.json with {sum(len(v) for v in all_zones.values())} price zones")
    print("Done.")


if __name__ == "__main__":
    main()
