#!/usr/bin/env python3
"""
clean_templates.py — Repair and polish price-cell areas across all poster templates.

Canada:   Heavy inpainting blotch (two-tone artifact) → uniform dark glass + gold accent.
USA:      Clean; minor bright-pixel removal only.
USACAN:   Clean; minor bright-pixel removal only.
EUR/USDT: Clean; icon-bleed artifacts suppressed, right-side price zone confirmed clear.

Design goals:
  • Uniform semi-transparent glass in every price cell — no blotches, no stickers
  • Subtle gold left-separator preserved
  • Right side fully empty for Python price overlay
  • Crimson (CAD/USD/USACAN) and emerald (USDT/EUR) palettes respected
"""

from PIL import Image, ImageDraw, ImageFilter
import numpy as np
from pathlib import Path

TEMPLATES = Path("/var/www/exchange_bot/templates_clean")


# ── Colour palettes ────────────────────────────────────────────────────────────

# Canada / USA / USACAN  (deep crimson-noir)
CRIMSON_CELL  = np.array([20,  7,  5], dtype=np.float32)   # base dark fill
CRIMSON_GLASS = np.array([35, 12,  8], dtype=np.float32)   # subtle glass highlight
GOLD          = (188, 153,  62)
GOLD_DIM      = ( 60,  48,  20)

# USDT / EUR  (deep emerald-noir)
EMERALD_CELL  = np.array([ 8, 24,  8], dtype=np.float32)
EMERALD_GLASS = np.array([14, 38, 14], dtype=np.float32)


# ── Core helper ────────────────────────────────────────────────────────────────

def glass_fill(arr: np.ndarray, box: list[int],
               base_color: np.ndarray, glass_color: np.ndarray,
               blend_original: float = 0.12) -> None:
    """
    In-place premium glass-cell paint.

    1. Crush existing content:  pixel = orig * blend + base * (1-blend)
       — removes bright artifacts while letting the background breathe
    2. Add horizontal highlight band: faint lighter stripe near vertical centre
       simulating a glass edge reflection
    3. No gold border drawn here — left to the caller for per-template control.
    """
    x1, y1, x2, y2 = box
    region = arr[y1:y2, x1:x2].astype(np.float32)

    # Step 1 — blend original background down to base colour
    region = region * blend_original + base_color * (1.0 - blend_original)

    # Step 2 — horizontal glass-edge highlight (subtle lighter band, 15% of height)
    h, w = region.shape[:2]
    band_cy = h // 2
    band_half = max(2, h // 7)
    for dy in range(-band_half, band_half + 1):
        row_idx = band_cy + dy
        if 0 <= row_idx < h:
            t = 1.0 - abs(dy) / (band_half + 1)       # 0→1→0 fade
            strength = t * 0.18                         # max 18% lighter
            region[row_idx] = np.clip(
                region[row_idx] * (1.0 - strength) + glass_color * strength,
                0, 255
            )

    arr[y1:y2, x1:x2] = region.astype(np.uint8)


def draw_gold_separator(draw: ImageDraw.ImageDraw, box: list[int]) -> None:
    """Thin vertical gold bar on the left edge of the price cell."""
    x1, y1, x2, y2 = box
    draw.rectangle([x1, y1 + 6, x1 + 3, y2 - 6], fill=GOLD)


def draw_dim_border(draw: ImageDraw.ImageDraw, box: list[int], radius: int = 6) -> None:
    """Ultra-subtle dim gold rounded rect inside the cell — looks like inset recess."""
    x1, y1, x2, y2 = box
    inset = 4
    draw.rounded_rectangle(
        [x1 + inset, y1 + inset, x2 - inset, y2 - inset],
        radius=radius,
        outline=GOLD_DIM,
        width=1,
    )


# ── Per-template cleaners ──────────────────────────────────────────────────────

def clean_canada() -> None:
    path = TEMPLATES / "canada.png"
    img  = Image.open(path).convert("RGB")
    arr  = np.array(img)

    price_cells = [
        [470, 330, 810, 420],
        [470, 445, 810, 535],
        [470, 560, 810, 650],
        [470, 675, 810, 765],
        [470, 790, 810, 880],
    ]

    for box in price_cells:
        glass_fill(arr, box, CRIMSON_CELL, CRIMSON_GLASS, blend_original=0.10)

    img = Image.fromarray(arr)
    draw = ImageDraw.Draw(img)

    for box in price_cells:
        draw_gold_separator(draw, box)
        draw_dim_border(draw, box)

    img.save(str(path), "PNG")
    print("  canada.png   — price cells cleaned")


def clean_usa() -> None:
    path = TEMPLATES / "usa.png"
    img  = Image.open(path).convert("RGB")
    arr  = np.array(img)

    # USA already has beautiful gold rounded rects; just crush stray bright pixels
    price_cells = [
        [520, 310, 820, 410],
        [520, 430, 820, 530],
        [520, 550, 820, 650],
        [520, 670, 820, 770],
        [520, 790, 820, 890],
    ]

    for box in price_cells:
        x1, y1, x2, y2 = box
        region = arr[y1:y2, x1:x2].astype(np.float32)
        # Only darken isolated bright spots (>170), leave everything else intact
        bright_mask = region.max(axis=2) > 170
        # Smooth-suppress: blend bright pixels toward the local mean
        local_mean  = region.mean(axis=(0, 1))
        for c in range(3):
            region[:, :, c] = np.where(
                bright_mask,
                region[:, :, c] * 0.3 + local_mean[c] * 0.7,
                region[:, :, c],
            )
        arr[y1:y2, x1:x2] = region.astype(np.uint8)

    Image.fromarray(arr).save(str(path), "PNG")
    print("  usa.png      — bright-pixel artifacts suppressed")


def clean_usacan() -> None:
    path = TEMPLATES / "usacan.png"
    img  = Image.open(path).convert("RGB")
    arr  = np.array(img)

    price_cells = [
        [620, 430, 830, 525],
        [620, 655, 830, 750],
    ]

    for box in price_cells:
        x1, y1, x2, y2 = box
        region = arr[y1:y2, x1:x2].astype(np.float32)
        bright_mask = region.max(axis=2) > 170
        local_mean  = region.mean(axis=(0, 1))
        for c in range(3):
            region[:, :, c] = np.where(
                bright_mask,
                region[:, :, c] * 0.3 + local_mean[c] * 0.7,
                region[:, :, c],
            )
        arr[y1:y2, x1:x2] = region.astype(np.uint8)

    Image.fromarray(arr).save(str(path), "PNG")
    print("  usacan.png   — bright-pixel artifacts suppressed")


def clean_usdt() -> None:
    path = TEMPLATES / "usdt.png"
    img  = Image.open(path).convert("RGB")
    arr  = np.array(img)

    price_cells = [
        [560, 360, 780, 450],
        [560, 560, 780, 650],
    ]

    # Price area starts after icons (≈ x=560); icon bleeds are < 20px wide
    # Only clean the right portion of each cell (after the gold separator)
    for box in price_cells:
        x1, y1, x2, y2 = box
        clean_box = [x1 + 22, y1, x2, y2]    # skip gold separator + icon edge
        glass_fill(arr, clean_box, EMERALD_CELL, EMERALD_GLASS, blend_original=0.08)

    img  = Image.fromarray(arr)
    draw = ImageDraw.Draw(img)
    for box in price_cells:
        draw_dim_border(draw, box, radius=8)

    img.save(str(path), "PNG")
    print("  usdt.png     — price cells polished")


def clean_eur() -> None:
    path = TEMPLATES / "eur.png"
    img  = Image.open(path).convert("RGB")
    arr  = np.array(img)

    price_cells = [
        [560, 360, 780, 450],
        [560, 560, 780, 650],
    ]

    for box in price_cells:
        x1, y1, x2, y2 = box
        clean_box = [x1 + 22, y1, x2, y2]
        glass_fill(arr, clean_box, EMERALD_CELL, EMERALD_GLASS, blend_original=0.08)

    img  = Image.fromarray(arr)
    draw = ImageDraw.Draw(img)
    for box in price_cells:
        draw_dim_border(draw, box, radius=8)

    img.save(str(path), "PNG")
    print("  eur.png      — price cells polished")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Cleaning templates...")
    clean_canada()
    clean_usa()
    clean_usacan()
    clean_usdt()
    clean_eur()
    print("Done — all templates updated in templates_clean/")
