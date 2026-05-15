#!/usr/bin/env python3
"""
calibrate_coordinates.py

Generates grid-annotated versions of each poster template so that exact
bounding boxes can be identified visually, then produces debug preview images
with red rectangles for each defined box.

Usage:
    python3 calibrate_coordinates.py --grid      # generate grid overlay images
    python3 calibrate_coordinates.py --preview   # generate debug preview with boxes
    python3 calibrate_coordinates.py --all       # both
"""

import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ASSETS_DIR  = Path("/var/www/exchange_bot/assets/posters")
DEBUG_DIR   = Path("/var/www/exchange_bot/debug_coordinates")
COORDS_FILE = Path("/var/www/exchange_bot/poster_coordinates.json")

TEMPLATES = {
    "CAD":    ASSETS_DIR / "updated_cyrus_exchange_canada_poster.png",
    "USD":    ASSETS_DIR / "updated_cyrus_exchange_usa_poster.png",
    "EUR":    ASSETS_DIR / "updated_cyrus_exchange_europe_poster.png",
    "USDT":   ASSETS_DIR / "updated_cyrus_exchange_usdt_poster.png",
    "USACAN": ASSETS_DIR / "updated_cyrus_exchange_usacan_poster.png",
}

GRID_STEP = 50   # pixel grid lines every 50px
LABEL_STEP = 100  # coordinate labels every 100px


def load_font(size=14):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        return ImageFont.load_default()


def generate_grid_image(key: str, template_path: Path) -> Path:
    """Overlay a coordinate grid on the template so boxes can be identified."""
    img  = Image.open(template_path).convert("RGBA")
    W, H = img.size

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)
    font_sm = load_font(11)
    font_lg = load_font(14)

    # Light grid lines every 50px
    for x in range(0, W, GRID_STEP):
        alpha = 140 if x % LABEL_STEP == 0 else 60
        draw.line([(x, 0), (x, H)], fill=(255, 255, 0, alpha), width=1)

    for y in range(0, H, GRID_STEP):
        alpha = 140 if y % LABEL_STEP == 0 else 60
        draw.line([(0, y), (W, y)], fill=(255, 255, 0, alpha), width=1)

    # Coordinate labels every 100px
    for x in range(0, W, LABEL_STEP):
        draw.text((x + 2, 2),  str(x), font=font_sm, fill=(255, 255, 0, 220))

    for y in range(0, H, LABEL_STEP):
        draw.text((2, y + 2), str(y), font=font_sm, fill=(255, 255, 0, 220))

    # Merge
    combined = Image.alpha_composite(img, overlay)
    out_path = DEBUG_DIR / f"grid_{key.lower()}.png"
    combined.convert("RGB").save(str(out_path))
    print(f"  Grid saved → {out_path}")
    return out_path


def generate_preview_image(key: str, template_path: Path, coords: dict) -> Path:
    """Draw red debug rectangles for every defined box in coords."""
    img  = Image.open(template_path).convert("RGBA")
    W, H = img.size

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)
    font    = load_font(18)

    box_styles = {
        "date_box":        ("Date",         (255, 80,  80,  180), (255, 80,  80,  255)),
        "sell_boxes":      ("SELL",         (80,  200, 80,  180), (80,  255, 80,  255)),
        "buy_boxes":       ("BUY",          (80,  80,  255, 180), (80,  120, 255, 255)),
        "extra_price_boxes": ("EXTRA",      (255, 165, 0,   180), (255, 165, 0,   255)),
        "footer_box":      ("Footer",       (200, 80,  200, 180), (220, 80,  220, 255)),
    }

    def draw_box(box, label, fill_color, text_color):
        if not box or len(box) != 4:
            return
        x1, y1, x2, y2 = box
        # Semi-transparent fill
        draw.rectangle([x1, y1, x2, y2], fill=fill_color, outline=text_color, width=3)
        # Label centred in box
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        bb = draw.textbbox((0, 0), label, font=font)
        tw = bb[2] - bb[0]
        th = bb[3] - bb[1]
        draw.text((cx - tw // 2, cy - th // 2), label, font=font, fill=text_color)
        # Corner coords
        small = load_font(11)
        draw.text((x1 + 3, y1 + 3), f"({x1},{y1})", font=small, fill=text_color)
        draw.text((x1 + 3, y2 - 14), f"({x2},{y2})", font=small, fill=text_color)

    for field, (label, fill, outline) in box_styles.items():
        val = coords.get(field)
        if val is None:
            continue
        if isinstance(val[0], list):      # list of boxes
            for i, box in enumerate(val):
                draw_box(box, f"{label}{i+1}", fill, outline)
        else:                              # single box
            draw_box(val, label, fill, outline)

    combined = Image.alpha_composite(img, overlay)
    out_path = DEBUG_DIR / f"preview_{key.lower()}.png"
    combined.convert("RGB").save(str(out_path))
    print(f"  Preview saved → {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--grid",    action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--all",     action="store_true")
    args = parser.parse_args()

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    if args.all or args.grid:
        print("\n=== Generating grid overlay images ===")
        for key, path in TEMPLATES.items():
            if not path.exists():
                print(f"  MISSING: {path}")
                continue
            generate_grid_image(key, path)

    if args.all or args.preview:
        if not COORDS_FILE.exists():
            print(f"\nCoordinates file not found: {COORDS_FILE}")
            print("Run --grid first, inspect images, define coordinates, then run --preview")
            return
        coords_data = json.loads(COORDS_FILE.read_text())
        print("\n=== Generating debug preview images ===")
        for key, path in TEMPLATES.items():
            if not path.exists():
                print(f"  MISSING: {path}")
                continue
            poster_coords = coords_data.get(key, {})
            if not poster_coords:
                print(f"  No coordinates defined for {key} — skipping preview")
                continue
            generate_preview_image(key, path, poster_coords)

    print("\nDone.")


if __name__ == "__main__":
    main()
