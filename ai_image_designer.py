#!/usr/bin/env python3
"""
ai_image_designer.py
GPT Image API layer for Cyrus Exchange poster design.

Responsibilities:
  - Design professional poster templates with EMPTY price boxes
  - Never generate actual exchange rates or financial numbers
  - Save designs to /var/www/exchange_bot/ai_designs/

Public API
----------
generate_poster_design(poster_type, custom_instruction) -> Path
    Call GPT Image, save PNG, return path.

check_api_key() -> (ok: bool, message: str)
    Verify OPENAI_API_KEY is set and valid.
"""

import base64
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("ai_image_designer")

DESIGNS_DIR = Path("/var/www/exchange_bot/ai_designs")
TORONTO_TZ  = ZoneInfo("America/Toronto")

# ── Per-poster design briefs sent to GPT Image ────────────────────────────────
# Each brief describes layout, branding, and explicitly states empty price boxes.
POSTER_BRIEFS: dict[str, dict] = {
    "logo": {
        "size":   "1024x1024",
        "prompt": (
            "Professional luxury financial brand poster for Cyrus Global Exchange. "
            "Square format 1024x1024. Dark black background with warm gold accents. "
            "Large 'CGE' monogram logo centered with gold circle ring. "
            "'CYRUS GLOBAL EXCHANGE' text below logo. 'CONNECTING VALUE, BUILDING TRUST' tagline. "
            "Bottom section: two side-by-side dark gold-bordered rectangular date display boxes "
            "labeled 'تاریخ شمسی' and 'تاریخ میلادی' — COMPLETELY EMPTY inside. "
            "No numbers, no dates, no prices anywhere. Ultra-premium design."
        ),
    },
    "canada": {
        "size":   "1792x1024",
        "prompt": (
            "Professional Canadian Dollar exchange rate poster for Cyrus Global Exchange. "
            "Landscape 1792x1024. Dark crimson red and black gradient background. "
            "Toronto CN Tower silhouette right side. Canadian maple leaf motif. "
            "Top-left: Cyrus Global Exchange logo (CGE monogram gold circle). "
            "Top-center Persian title 'نرخ دلار کانادا' in gold Arabic font. "
            "Below title: 'CANADIAN DOLLAR RATE' in elegant English. "
            "Left column: 5 labeled rows with icons — 'فروش نقدی / Cash Sell' x3, "
            "'خرید نقدی / Cash Buy' x2, each with a small row icon. "
            "Right column: 5 COMPLETELY EMPTY dark rectangular price boxes aligned with each label row. "
            "Price boxes must be empty — no numbers, no placeholder text. "
            "Bottom footer bar: phone number area, @Cyrus.Exchange, location 'Guelph, Canada'. "
            "Luxury dark red financial poster aesthetic."
        ),
    },
    "usa": {
        "size":   "1792x1024",
        "prompt": (
            "Professional US Dollar exchange rate poster for Cyrus Global Exchange. "
            "Landscape 1792x1024. Dark navy blue and black gradient background. "
            "Statue of Liberty silhouette right side. American flag circle emblem. "
            "Top-left: Cyrus Global Exchange logo (CGE monogram gold circle). "
            "Top-center Persian title 'نرخ دلار آمریکا' in gold Arabic font. "
            "Below title: '— US DOLLAR RATE —' in elegant English with decorative lines. "
            "Left column: 5 labeled rows with icons — 'نقدی / فروش', 'داخل آمریکا', "
            "'ای ترنسفر آمریکا شخصی', 'خرید نقدی', 'خرید نقدی - ترانسفر'. "
            "Right column: 5 COMPLETELY EMPTY dark rectangular price boxes aligned with each row. "
            "Price boxes must be empty — no numbers, no placeholder text. "
            "Bottom footer: phone 2261672800, Cyrus Global Exchange, CyrusFx_Concordia. "
            "Luxury dark navy financial poster aesthetic."
        ),
    },
    "usacan": {
        "size":   "1792x1024",
        "prompt": (
            "Professional USD to CAD exchange rate poster for Cyrus Global Exchange. "
            "Landscape 1792x1024. Dark navy blue and black gradient background. "
            "Statue of Liberty silhouette right side. Both American and Canadian flag circles with arrow between them. "
            "Top-left: Cyrus Global Exchange logo (CGE monogram gold circle). "
            "Top Persian title 'فروش دلار آمریکا به دلار کانادا' in gold Arabic font. "
            "Below: '★ SELL US DOLLAR TO CANADIAN DOLLAR ★' in English. "
            "Two large COMPLETELY EMPTY dark price boxes — one labeled 'فروش / Sell' with CAD indicator, "
            "one labeled 'خرید / Buy'. Both boxes empty — no numbers whatsoever. "
            "Bottom footer: phone 2269627729, @Cyrus.Exchange, Guelph Canada. "
            "Luxury dark blue financial poster aesthetic."
        ),
    },
    "usdt": {
        "size":   "1792x1024",
        "prompt": (
            "Professional USDT Tether exchange rate poster for Cyrus Global Exchange. "
            "Landscape 1792x1024. Dark emerald green and black gradient background. "
            "Toronto CN Tower silhouette right side. Large Tether coin (green T logo) emblem right. "
            "Top-left: Cyrus Global Exchange logo (CGE monogram gold circle). "
            "Top Persian title 'نرخ تتر به دلار کانادا' in gold Arabic font. "
            "Below: '★ USDT (TRC20) TO CANADIAN DOLLAR ★' in English. "
            "Two large COMPLETELY EMPTY dark price boxes — one labeled 'فروش / Sell' with USDT→CAD arrows, "
            "one labeled 'خرید / Buy'. Both boxes empty — no numbers whatsoever. "
            "Bottom footer: phone 2269627729, @Cyrus.Exchange, Guelph Canada. "
            "Luxury dark green crypto financial poster aesthetic."
        ),
    },
    "eur": {
        "size":   "1792x1024",
        "prompt": (
            "Professional Euro exchange rate poster for Cyrus Global Exchange. "
            "Landscape 1792x1024. Dark emerald green and gold gradient background. "
            "Eiffel Tower silhouette right side. Large Euro coin emblem with EU stars. "
            "Top-left: Cyrus Global Exchange logo (CGE monogram gold circle). "
            "Top Persian title 'نرخ یورو اروپا به دلار کانادا' in gold Arabic font. "
            "Below: '★ EUROPE EURO TO CANADIAN DOLLAR ★' in English. "
            "Two large COMPLETELY EMPTY dark price boxes — one labeled 'فروش / Sell', "
            "one labeled 'خرید / Buy'. Both boxes empty — no numbers whatsoever. "
            "Bottom footer: phone 2269627729, @Cyrus.Exchange, Guelph Canada. "
            "Luxury dark green European financial poster aesthetic."
        ),
    },
}


def check_api_key() -> tuple[bool, str]:
    """Return (ok, message). Does not reveal the key value."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return False, "OPENAI_API_KEY is not set in .env"
    if len(key) < 20:
        return False, "OPENAI_API_KEY appears too short — check .env"
    return True, "OPENAI_API_KEY is present"


def _get_client():
    ok, msg = check_api_key()
    if not ok:
        raise RuntimeError(msg)
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai package missing — run: pip install openai --break-system-packages")
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def generate_poster_design(
    poster_type: str,
    custom_instruction: str = "",
) -> Path:
    """
    Call GPT Image API to generate a poster template design.
    Price boxes will be empty — Python writes actual numbers later.

    Parameters
    ----------
    poster_type         : "logo" | "canada" | "usa" | "usacan" | "usdt" | "eur"
    custom_instruction  : optional extra style/layout instruction

    Returns
    -------
    Path to saved PNG in ai_designs/
    """
    DESIGNS_DIR.mkdir(parents=True, exist_ok=True)

    key = poster_type.lower()
    brief = POSTER_BRIEFS.get(key)
    if not brief:
        raise ValueError(f"Unknown poster_type '{poster_type}'. Valid: {list(POSTER_BRIEFS)}")

    prompt = brief["prompt"]
    if custom_instruction:
        prompt += f" Additional design instruction: {custom_instruction}"
    # Safety: explicitly forbid numbers
    prompt += (
        " CRITICAL: Do NOT write any numbers, prices, exchange rates, or financial values "
        "anywhere in the image. All price boxes must be visually empty."
    )

    size = brief["size"]
    log.info(f"[ai_designer] Requesting GPT Image for '{poster_type}'  size={size}")
    log.info(f"[ai_designer] Prompt: {prompt[:120]}…")

    client = _get_client()

    try:
        response = client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size=size,
            quality="high",
            n=1,
            response_format="b64_json",
        )
        img_b64 = response.data[0].b64_json
        img_bytes = base64.b64decode(img_b64)
    except Exception as exc:
        # gpt-image-1 may not support 1792x1024 — fall back to dall-e-3
        log.warning(f"[ai_designer] gpt-image-1 failed ({exc}), trying dall-e-3")
        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1792x1024" if size == "1792x1024" else "1024x1024",
            quality="hd",
            n=1,
            response_format="b64_json",
        )
        img_b64 = response.data[0].b64_json
        img_bytes = base64.b64decode(img_b64)

    ts       = datetime.now(TORONTO_TZ).strftime("%Y%m%d_%H%M%S")
    out_path = DESIGNS_DIR / f"{key}_{ts}.png"
    out_path.write_bytes(img_bytes)

    log.info(f"[ai_designer] Design saved → {out_path}  size={len(img_bytes)//1024}KB")
    return out_path


def list_designs(poster_type: str = None) -> list[Path]:
    """Return sorted list of saved design files, newest first."""
    DESIGNS_DIR.mkdir(parents=True, exist_ok=True)
    pattern = f"{poster_type.lower()}_*.png" if poster_type else "*.png"
    return sorted(DESIGNS_DIR.glob(pattern), reverse=True)


def latest_design(poster_type: str) -> Path | None:
    """Return most recent design for a poster type, or None."""
    results = list_designs(poster_type)
    return results[0] if results else None
