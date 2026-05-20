#!/usr/bin/env python3
"""
clean_usdt_eur.py — Natural background reconstruction for USDT and EUR price cells.

Strategy:
  • x=700-780 of each row is already clean dark-green (std ≈ 8.6) — use as reference.
  • Reconstruct x=582-780 by sampling that reference texture (preserves natural look).
  • Leave x=565-582 untouched (icon-edge bleed — part of the row design).
  • Zero heavy overlays, zero flat fills, zero visible editing marks.

Price boxes (1280x960 canvas):
  SELL: x1=565  y1=370  x2=780  y2=445
  BUY:  x1=565  y1=565  x2=780  y2=640
"""

from PIL import Image, ImageFilter
import numpy as np
from pathlib import Path

TEMPLATES = Path("/var/www/exchange_bot/templates_clean")

SELL_BOX = (565, 370, 780, 445)
BUY_BOX  = (565, 565, 780, 640)

ICON_BLEED_X = 582   # keep pixels left of this untouched (icon anti-aliasing)
REF_X1       = 700   # reference strip start (confirmed clean)
REF_X2       = 780   # reference strip end


def reconstruct_cell(arr: np.ndarray, box: tuple) -> None:
    """
    Replace the price area with a smooth reconstruction of the natural background.

    1. Sample reference texture from the clean right portion of the same cell (x=700-780).
    2. Tile that reference patch across the full reconstruction zone (x=582-780).
    3. Apply a micro-blur to eliminate any tiling seams.
    4. Blend smoothly into surrounding pixels at the left boundary.
    """
    x1, y1, x2, y2 = box
    h = y2 - y1
    rw = REF_X2 - REF_X1          # reference width = 80px
    tw = x2 - ICON_BLEED_X        # reconstruction zone width = 198px

    # ── Step 1: capture clean reference patch ─────────────────────────────────
    ref = arr[y1:y2, REF_X1:REF_X2].astype(np.float32)

    # ── Step 2: tile reference across reconstruction zone ────────────────────
    repeats = -(-tw // rw)         # ceiling division
    tiled   = np.tile(ref, (1, repeats, 1))[:, :tw, :]

    # ── Step 3: add subtle natural noise (σ = 2) so it never looks stamped ───
    rng   = np.random.default_rng(seed=42)
    noise = rng.normal(0, 2.0, tiled.shape)
    patch = np.clip(tiled + noise, 0, 255).astype(np.uint8)

    # ── Step 4: write patch into the reconstruction zone ─────────────────────
    arr[y1:y2, ICON_BLEED_X:x2] = patch

    # ── Step 5: feather the left boundary (5px fade into icon bleed) ─────────
    fade_w = 5
    for dx in range(fade_w):
        px  = ICON_BLEED_X + dx
        t   = dx / fade_w                              # 0 → 1
        orig = arr[y1:y2, px].astype(np.float32)
        repl = patch[:, dx].astype(np.float32)
        arr[y1:y2, px] = np.clip(orig * (1 - t) + repl * t, 0, 255).astype(np.uint8)


def clean_template(path: Path) -> None:
    img = Image.open(path).convert("RGB")
    arr = np.array(img)

    for box in (SELL_BOX, BUY_BOX):
        reconstruct_cell(arr, box)

    # Micro-blur only the reconstructed zones to erase any remaining seam
    result = Image.fromarray(arr)
    for box in (SELL_BOX, BUY_BOX):
        x1, y1, x2, y2 = box
        zone = result.crop((ICON_BLEED_X, y1, x2, y2))
        zone = zone.filter(ImageFilter.GaussianBlur(radius=0.6))
        result.paste(zone, (ICON_BLEED_X, y1))

    result.save(str(path), "PNG")
    print(f"  {path.name}  — price cells reconstructed")


if __name__ == "__main__":
    print("Reconstructing USDT and EUR price cells...")
    for fname in ("usdt.png", "eur.png"):
        clean_template(TEMPLATES / fname)
    print("Done.")
