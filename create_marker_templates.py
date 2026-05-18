#!/usr/bin/env python3
"""
create_marker_templates.py
==========================
Produces two output directories:

  templates_clean/   — pixel-perfect copies of the 6 master posters
  templates_marker/  — same posters with PURE PINK (#FF00FF) thin outline
                       rectangles drawn around every changing price / date field.

The marker rectangles are the detection targets for the Python renderer:
  1. detect pink box
  2. erase old value inside the box
  3. write new value centred inside the same box
  4. preserve all surrounding design
"""

import shutil
from pathlib import Path
from PIL import Image, ImageDraw

BASE        = Path("/var/www/exchange_bot")
SRC_DIR     = BASE / "templates"
CLEAN_DIR   = BASE / "templates_clean"
MARKER_DIR  = BASE / "templates_marker"

PINK        = (255, 0, 255)   # pure #FF00FF
THICKNESS   = 4               # 4 px outline — inside the 3-5 px spec

# ── Marker box definitions ─────────────────────────────────────────────────────
# Each list entry: [x1, y1, x2, y2]
# Boxes cover the FULL price / date cell so the renderer can erase → rewrite.
#
# canada / usa  : table layout, 5 rows (3 sell + 2 buy), numbers right of divider
# usdt / usacan : 2-row layout (1 sell + 1 buy), numbers right of icons
# eur           : same 2-row layout
# logo          : Shamsi + Gregorian date cells (right column of each date row)

MARKERS: dict[str, list[list[int]]] = {

    # ── CANADA ────────────────────────────────────────────────────────────────
    # Coordinates from poster_coordinates.json (authoritative, measured on 1448×1086 template)
    # 3 sell rows + 2 buy rows
    "canada.png": [
        [545, 342, 878, 422],    # SELL row 1  (Cash Sell)
        [545, 464, 878, 545],    # SELL row 2  (Left Cheque)
        [545, 585, 878, 666],    # SELL row 3  (E-Transfer)
        [545, 706, 878, 787],    # BUY  row 4  (Cash Buy)
        [545, 827, 878, 908],    # BUY  row 5  (Cash Buy – Direct)
    ],

    # ── USA ───────────────────────────────────────────────────────────────────
    # Coordinates from poster_coordinates.json
    "usa.png": [
        [503, 342, 852, 426],    # SELL row 1  (Cash Sell)
        [503, 464, 852, 549],    # SELL row 2  (Inside USA)
        [503, 587, 852, 671],    # SELL row 3  (Personal Transfer)
        [503, 706, 852, 790],    # BUY  row 4  (Cash Buy)
        [503, 826, 852, 910],    # BUY  row 5  (Cash Buy – Direct)
    ],

    # ── USDT ──────────────────────────────────────────────────────────────────
    # Coordinates from poster_coordinates.json
    "usdt.png": [
        [684, 375, 972, 595],    # SELL
        [684, 645, 972, 870],    # BUY
    ],

    # ── USACAN ────────────────────────────────────────────────────────────────
    # Coordinates from poster_coordinates.json
    "usacan.png": [
        [728, 385, 1012, 532],   # SELL
        [728, 700, 1012, 862],   # BUY
    ],

    # ── EUR ───────────────────────────────────────────────────────────────────
    # Coordinates from poster_coordinates.json
    "eur.png": [
        [684, 375, 972, 600],    # SELL
        [684, 638, 972, 862],    # BUY
    ],

    # ── LOGO ──────────────────────────────────────────────────────────────────
    # templates/logo.png is byte-identical to assets/posters/logo.png (1254×1254)
    # Cell positions from rate_graphic_publisher.py (measured via OCR on the actual file)
    "logo.png": [
        [637, 948,  960, 1058],  # Shamsi date right cell   (YYYY/MM/DD placeholder)
        [637, 1058, 960, 1168],  # Gregorian date right cell (DD/MM/YYYY placeholder)
    ],
}


def _draw_marker(draw: ImageDraw.ImageDraw, box: list[int]) -> None:
    """Draw a PINK outline rectangle (no fill)."""
    x1, y1, x2, y2 = box
    for t in range(THICKNESS):
        draw.rectangle([x1 + t, y1 + t, x2 - t, y2 - t], outline=PINK)


def main() -> None:
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    MARKER_DIR.mkdir(parents=True, exist_ok=True)

    for filename, boxes in MARKERS.items():
        src = SRC_DIR / filename
        if not src.exists():
            print(f"  [SKIP] {filename} — not found in {SRC_DIR}")
            continue

        # ── templates_clean: exact copy ──────────────────────────────────────
        clean_dst = CLEAN_DIR / filename
        shutil.copy2(str(src), str(clean_dst))

        # ── templates_marker: copy + pink outline rectangles ─────────────────
        img  = Image.open(src).convert("RGB")
        draw = ImageDraw.Draw(img)
        for box in boxes:
            _draw_marker(draw, box)

        marker_dst = MARKER_DIR / filename
        img.save(str(marker_dst), "PNG")

        print(f"  [OK] {filename} — {len(boxes)} marker(s)")

    print(f"\nDone.\n  Clean  → {CLEAN_DIR}\n  Marker → {MARKER_DIR}")


if __name__ == "__main__":
    main()
