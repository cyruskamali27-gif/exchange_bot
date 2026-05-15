#!/usr/bin/env python3
"""
detect_zones.py

Uses pytesseract layout analysis (TSV output) to find exact bounding boxes
of price numbers and date text in each poster template, then generates cropped
inspection images for manual confirmation.

Outputs:
  debug_coordinates/inspect_<key>_<zone>.png  — cropped zone crops
  debug_coordinates/ocr_<key>.json            — raw OCR hits
"""

import json
import re
from pathlib import Path

import pytesseract
from PIL import Image, ImageDraw, ImageFont

ASSETS_DIR = Path("/var/www/exchange_bot/assets/posters")
DEBUG_DIR  = Path("/var/www/exchange_bot/debug_coordinates")

TEMPLATES = {
    "CAD":    ASSETS_DIR / "updated_cyrus_exchange_canada_poster.png",
    "USD":    ASSETS_DIR / "updated_cyrus_exchange_usa_poster.png",
    "EUR":    ASSETS_DIR / "updated_cyrus_exchange_europe_poster.png",
    "USDT":   ASSETS_DIR / "updated_cyrus_exchange_usdt_poster.png",
    "USACAN": ASSETS_DIR / "updated_cyrus_exchange_usacan_poster.png",
}

PRICE_RE   = re.compile(r"^\d[\d,\.]{3,}$")   # e.g. 125,400 or 181400 or 1.38
DATE_FA_RE = re.compile(r"[۰-۹]{4}|[۰-۹]{2}")  # Persian digits → likely date
YEAR_RE    = re.compile(r"\b(20\d{2}|14\d{2})\b")


def load_font(size=13):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        return ImageFont.load_default()


def run_ocr_tsv(img: Image.Image, lang: str) -> list[dict]:
    """Run pytesseract in TSV mode, return list of word dicts with bbox."""
    tsv = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
    results = []
    n = len(tsv["text"])
    for i in range(n):
        txt = (tsv["text"][i] or "").strip()
        if not txt:
            continue
        conf = int(tsv["conf"][i])
        if conf < 20:
            continue
        results.append({
            "text": txt,
            "conf": conf,
            "x":    tsv["left"][i],
            "y":    tsv["top"][i],
            "w":    tsv["width"][i],
            "h":    tsv["height"][i],
            "x2":   tsv["left"][i] + tsv["width"][i],
            "y2":   tsv["top"][i]  + tsv["height"][i],
            "lang": lang,
        })
    return results


def merge_nearby_words(words: list[dict], x_gap=30, y_gap=10) -> list[dict]:
    """Merge words that are close together into phrase bboxes."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (w["y"], w["x"]))
    groups = []
    cur = words[0].copy()
    for w in words[1:]:
        same_line  = abs(w["y"] - cur["y"]) <= y_gap
        close_x    = (w["x"] - cur["x2"]) <= x_gap
        if same_line and close_x:
            cur["text"] += " " + w["text"]
            cur["x2"]    = max(cur["x2"], w["x2"])
            cur["y2"]    = max(cur["y2"], w["y2"])
            cur["conf"]  = min(cur["conf"], w["conf"])
        else:
            groups.append(cur)
            cur = w.copy()
    groups.append(cur)
    return groups


def classify_words(words: list[dict]) -> dict[str, list[dict]]:
    """Separate words into prices, date-related, and other."""
    prices = []
    dates  = []
    others = []
    for w in words:
        t = w["text"].strip()
        if PRICE_RE.match(t):
            prices.append(w)
        elif YEAR_RE.search(t) or DATE_FA_RE.search(t):
            dates.append(w)
        else:
            others.append(w)
    return {"prices": prices, "dates": dates, "others": others}


def expand_box(box: tuple, pad: int, W: int, H: int) -> tuple:
    x1, y1, x2, y2 = box
    return (
        max(0, x1 - pad),
        max(0, y1 - pad),
        min(W, x2 + pad),
        min(H, y2 + pad),
    )


def save_crop(img: Image.Image, box: tuple, key: str, label: str) -> Path:
    x1, y1, x2, y2 = box
    crop = img.crop((x1, y1, x2, y2)).convert("RGBA")
    W, H = crop.size
    draw = ImageDraw.Draw(crop)
    font = load_font(12)
    draw.rectangle([0, 0, W - 1, H - 1], outline=(255, 0, 0, 255), width=2)
    draw.text((4, 4), f"{label}  [{x1},{y1}]→[{x2},{y2}]", font=font, fill=(255, 255, 0, 255))
    safe_label = label.replace(" ", "_")
    out = DEBUG_DIR / f"inspect_{key.lower()}_{safe_label}.png"
    crop.convert("RGB").save(str(out))
    return out


def annotate_full(img: Image.Image, all_words: list[dict], key: str) -> Path:
    """Draw all detected word bboxes on a copy of the image."""
    overlay = img.convert("RGBA").copy()
    draw    = ImageDraw.Draw(overlay)
    font    = load_font(10)

    for w in all_words:
        x1, y1, x2, y2 = w["x"], w["y"], w["x2"], w["y2"]
        t = w["text"]
        if PRICE_RE.match(t):
            colour = (0, 255, 100, 200)
        elif YEAR_RE.search(t) or DATE_FA_RE.search(t):
            colour = (255, 200, 0, 200)
        else:
            colour = (120, 120, 255, 160)
        draw.rectangle([x1, y1, x2, y2], outline=colour, width=2)
        draw.text((x1 + 1, y1 + 1), t[:12], font=font, fill=colour)

    out = DEBUG_DIR / f"annotated_{key.lower()}.png"
    overlay.convert("RGB").save(str(out))
    return out


def analyse_template(key: str, path: Path):
    print(f"\n{'='*50}")
    print(f"  Template: {key}  →  {path.name}")
    print(f"{'='*50}")

    img  = Image.open(path).convert("RGB")
    W, H = img.size
    print(f"  Size: {W}×{H}")

    # Run OCR in both languages
    words_en = run_ocr_tsv(img, "eng")
    words_fa = run_ocr_tsv(img, "fas")
    all_words = words_en + words_fa

    # Save raw OCR data
    ocr_out = DEBUG_DIR / f"ocr_{key.lower()}.json"
    ocr_out.write_text(json.dumps(all_words, ensure_ascii=False, indent=2))
    print(f"  OCR hits: {len(all_words)} words → {ocr_out.name}")

    # Classify
    cls = classify_words(all_words)
    print(f"  Prices detected: {len(cls['prices'])}")
    for p in cls["prices"]:
        print(f"    '{p['text']}'  @ x={p['x']}-{p['x2']}  y={p['y']}-{p['y2']}  conf={p['conf']}")

    print(f"  Date-related: {len(cls['dates'])}")
    for d in cls["dates"]:
        print(f"    '{d['text']}'  @ x={d['x']}-{d['x2']}  y={d['y']}-{d['y2']}  conf={d['conf']}")

    # Annotate full image
    ann = annotate_full(img.convert("RGBA"), all_words, key)
    print(f"  Annotated → {ann.name}")

    return cls, all_words, (W, H)


def main():
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    report = {}

    for key, path in TEMPLATES.items():
        if not path.exists():
            print(f"MISSING: {path}")
            continue
        cls, words, (W, H) = analyse_template(key, path)
        report[key] = {
            "size": [W, H],
            "prices": cls["prices"],
            "dates": cls["dates"],
        }

    # Save summary
    summary_path = DEBUG_DIR / "detection_summary.json"
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n\nSummary saved → {summary_path}")

    # Print coordinate recommendations
    print("\n\n=== COORDINATE RECOMMENDATIONS ===")
    for key, data in report.items():
        print(f"\n{key}  ({data['size'][0]}×{data['size'][1]}):")
        for p in data["prices"]:
            print(f"  price '{p['text']}':  [{p['x']},{p['y']},{p['x2']},{p['y2']}]")
        for d in data["dates"]:
            print(f"  date  '{d['text']}':  [{d['x']},{d['y']},{d['x2']},{d['y2']}]")


if __name__ == "__main__":
    main()
