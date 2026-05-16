#!/usr/bin/env python3
"""
design_iteration_agent.py
Autonomous Self-Correcting Visual AI Design Loop
Group: @cyrusGlobalExchange only

Commands:
  /design_task <instruction>    — start a design task (autonomous loop)
  /design_status                — show current task status
  /design_stop                  — stop running task
  /design_last                  — show last completed task
  /design_qc                    — re-run QC on last previews
  /design_logs                  — show recent agent logs
  /design_apply                 — apply approved preview to production
  /design_approve               — approve current iteration
  /design_reject <reason>       — reject and request fix
  /design_fix_prompt <hint>     — add a fix hint for the next iteration
  /design_autofix_on            — enable Gemini Vision QC loop
  /design_autofix_off           — disable Vision QC (local QC only)
  /design_live_qc               — download + analyze last posted images
  /design_compare               — compare current vs best-known layout
  /design_restore_last_good     — restore best known render adjustments
  /design_debug                 — show render adjustments + layout info
  /design_score                 — show last Gemini Vision scores
"""

import asyncio
import io
import json
import logging
import os
import re
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

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

try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import google.generativeai as _genai_old
        _genai_old.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
        _GEMINI_MODEL = _genai_old.GenerativeModel("gemini-2.5-flash")
    HAS_GEMINI = True
except Exception:
    _GEMINI_MODEL = None
    HAS_GEMINI = False

import requests as _http

# ── Configuration ─────────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ.get("ADMIN_ID", "0"))
TORONTO_TZ  = ZoneInfo("America/Toronto")

ALLOWED_CHAT_USERNAME = "cyrusGlobalExchange"

BASE_DIR          = Path("/var/www/exchange_bot")
ITER_DIR          = BASE_DIR / "design_iterations"
TASKS_DIR         = ITER_DIR / "tasks"
BACKUPS_DIR       = ITER_DIR / "backups"
PREVIEWS_DIR      = ITER_DIR / "previews"
REPORTS_DIR       = ITER_DIR / "reports"
LOGS_DIR          = ITER_DIR / "logs"
GOOD_LAYOUTS_DIR  = ITER_DIR / "good_layouts"
ASSETS_DIR        = BASE_DIR / "assets/posters"
COORDS_FILE       = BASE_DIR / "poster_coordinates.json"
PROD_PY           = BASE_DIR / "rate_graphic_publisher.py"

FONT_BOLD_FA = "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"
FONT_REG_FA  = "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"
FONT_BOLD_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG_EN  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

TEMPLATES = {
    "CAD":    "updated_cyrus_exchange_canada_poster.png",
    "USD":    "updated_cyrus_exchange_usa_poster.png",
    "USACAN": "updated_cyrus_exchange_usacan_poster.png",
    "USDT":   "updated_cyrus_exchange_usdt_poster.png",
    "EUR":    "updated_cyrus_exchange_europe_poster.png",
}

POSTER_ORDER    = ["CAD", "USD", "USACAN", "USDT", "EUR"]
MAX_ITERATIONS  = 10
SCORE_THRESHOLD = 95.0

# ── Private/public routing ─────────────────────────────────────────────────────
# All iterations, QC, debug → ADMIN_DEBUG_CHAT (private).
# Only final approved result → PUBLIC_CHANNEL.
ADMIN_DEBUG_CHAT    = ADMIN_ID           # resolved at runtime from env
PUBLIC_CHANNEL      = f"@{ALLOWED_CHAT_USERNAME}"
VISUAL_FEEDBACK_FILE = ITER_DIR / "visual_feedback_memory.json"
SCREENSHOTS_DIR      = ITER_DIR / "screenshots"

REGION_KEYWORDS = {
    "date_box":  ["تاریخ", "date", "شمسی", "shamsi", "gregorian", "weekday", "روز"],
    "price_box": ["قیمت", "price", "عدد", "number", "نرخ", "rate", "دلار", "dollar"],
    "logo":      ["لوگو", "logo", "آرم", "نشان"],
    "footer":    ["پایین", "footer", "پاورقی", "bottom"],
    "spacing":   ["فاصله", "space", "spacing", "چسبیده", "فشرده"],
    "alignment": ["تراز", "align", "وسط", "center", "کج", "crooked"],
}

# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_FILE = LOGS_DIR / "design_agent.log"

def _setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        ],
    )

_setup_logging()
log = logging.getLogger("design_agent")

# ── Global state ──────────────────────────────────────────────────────────────

_current_task:   dict  = {}
_task_lock             = asyncio.Lock()
_last_previews:  list  = []
_fix_hints:      list  = []
_approved:       bool  = False
_last_task_id:   str   = ""
_autofix_enabled: bool = True

# Per-currency render adjustments (y_offset, font_scale, padding_extra)
_DEFAULT_ADJ = lambda: {"y_offset": 0, "font_scale": 1.0, "padding_extra": 0}
_render_adj: dict = {cur: _DEFAULT_ADJ() for cur in POSTER_ORDER}
_prev_adj:   dict = {cur: _DEFAULT_ADJ() for cur in POSTER_ORDER}

# Vision scores from last run
_last_scores:      dict = {}   # {currency: float}
_last_posted_ids:  dict = {}   # {currency: (file_id, msg_id)}

# Screenshot feedback state
_pending_screenshot: dict = {}   # {admin_id: {file_id, text, timestamp}}
_focused_region:     str  = ""   # e.g. "date_box", "price_box"


def _setup_dirs():
    for d in [TASKS_DIR, BACKUPS_DIR, PREVIEWS_DIR, REPORTS_DIR, LOGS_DIR, GOOD_LAYOUTS_DIR, SCREENSHOTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ── Security guard ────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat:
        return False
    if chat.username and chat.username.lower() == ALLOWED_CHAT_USERNAME.lower():
        return True
    if chat.type == "private" and user and user.id == ADMIN_ID:
        return True
    return False


# ── Font / image helpers ──────────────────────────────────────────────────────

def fa(text: str) -> str:
    if not HAS_BIDI or not text:
        return text
    return get_display(arabic_reshaper.reshape(text))


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def sample_bg(px_rgb, x1: int, y1: int, x2: int, y2: int) -> tuple:
    samples = []
    for sy in range(y1, y2, 4):
        for sx in range(x1, x2, 15):
            r, g, b = px_rgb[sx, sy]
            samples.append((r, g, b, (r + g + b) / 3))
    if not samples:
        return (20, 20, 40)
    samples.sort(key=lambda s: s[3])
    dark = samples[: max(1, len(samples) // 4)]
    return (
        sum(s[0] for s in dark) // len(dark),
        sum(s[1] for s in dark) // len(dark),
        sum(s[2] for s in dark) // len(dark),
    )


def _fit_text(draw, text: str, font_path: str, start_size: int, max_width: int, min_size: int = 9):
    size = start_size
    while size >= min_size:
        font = load_font(font_path, size)
        bb = draw.textbbox((0, 0), text, font=font)
        if (bb[2] - bb[0]) <= max_width - 12:
            return font, bb, size
        size -= 1
    font = load_font(font_path, min_size)
    bb = draw.textbbox((0, 0), text, font=font)
    return font, bb, min_size


# ── Date helpers ──────────────────────────────────────────────────────────────

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_WEEKDAYS  = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یک‌شنبه"]


def get_shamsi_date(dt: datetime) -> str:
    if not HAS_JDATE:
        return dt.strftime("%Y/%m/%d")
    jd    = jdatetime.datetime.fromgregorian(datetime=dt)
    year  = str(jd.year).translate(_FA_DIGITS)
    month = str(jd.month).zfill(2).translate(_FA_DIGITS)
    day   = str(jd.day).zfill(2).translate(_FA_DIGITS)
    return f"{year}/{month}/{day}"


def get_persian_weekday(dt: datetime) -> str:
    return _WEEKDAYS[dt.weekday()]


def get_gregorian_date(dt: datetime) -> str:
    return dt.strftime("%-d %B %Y")


# ── Bot API helpers ───────────────────────────────────────────────────────────

def _api_send(chat_id, text: str):
    try:
        _http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": str(chat_id), "text": text},
            timeout=15,
        )
    except Exception as exc:
        log.error(f"_api_send error: {exc}")


def _api_photo(chat_id, path: Path, caption: str = ""):
    try:
        with open(path, "rb") as fh:
            _http.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": str(chat_id), "caption": caption},
                files={"photo": fh},
                timeout=30,
            )
    except Exception as exc:
        log.error(f"_api_photo error: {exc}")


def _post_and_get_file_id(chat_id, path: Path, caption: str = "") -> tuple | None:
    """Post photo via Bot API, return (file_id, message_id) or None."""
    try:
        with open(path, "rb") as fh:
            resp = _http.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": str(chat_id), "caption": caption},
                files={"photo": fh},
                timeout=30,
            )
        data = resp.json()
        if data.get("ok"):
            result = data["result"]
            photos = result.get("photo", [])
            if photos:
                return photos[-1]["file_id"], result["message_id"]
    except Exception as exc:
        log.error(f"_post_and_get_file_id error: {exc}")
    return None


def _download_telegram_image(file_id: str) -> bytes | None:
    """Download actual Telegram-rendered image bytes for a given file_id."""
    try:
        resp = _http.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            file_path = data["result"]["file_path"]
            img_resp = _http.get(
                f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
                timeout=30,
            )
            if img_resp.status_code == 200:
                return img_resp.content
    except Exception as exc:
        log.error(f"_download_telegram_image error: {exc}")
    return None


# ── Private / public send helpers ────────────────────────────────────────────

def _priv_msg(text: str):
    """Send text to admin private chat only — never to the public channel."""
    _api_send(ADMIN_DEBUG_CHAT, text)


def _priv_photo(path: Path, caption: str = ""):
    """Send photo to admin private chat only."""
    _api_photo(ADMIN_DEBUG_CHAT, path, caption)


def _pub_photo(path: Path, caption: str = ""):
    """Send photo to the public channel — ONLY call from /design_approve flow."""
    _api_photo(PUBLIC_CHANNEL, path, caption)


def _pub_msg(text: str):
    """Send text to the public channel — ONLY call from /design_approve flow."""
    _api_send(PUBLIC_CHANNEL, text)


# ── Visual feedback memory ────────────────────────────────────────────────────

def _load_vfm() -> list:
    if VISUAL_FEEDBACK_FILE.exists():
        try:
            return json.loads(VISUAL_FEEDBACK_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_vfm(entries: list):
    VISUAL_FEEDBACK_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _append_vfm(entry: dict):
    entries = _load_vfm()
    entries.append(entry)
    entries = entries[-100:]  # keep last 100
    _save_vfm(entries)


def _get_preferred_vfm() -> list:
    return [e for e in _load_vfm() if e.get("preferred")]


# ── Gemini Vision analysis ────────────────────────────────────────────────────

def _analyze_with_gemini_vision(currency: str, image_bytes: bytes, iteration: int) -> tuple:
    """
    Analyze a Telegram-rendered poster image with Gemini Vision.
    Returns (score: float, issues: list[str]).
    """
    if not HAS_GEMINI or not image_bytes:
        return 100.0, []

    prompt = (
        "You are a LAYOUT QC inspector for a poster image. "
        "Evaluate ONLY the visual layout of the date box area near the top of this poster.\n\n"
        "IMPORTANT: Do NOT judge whether dates are factually correct. "
        "Do NOT validate calendar systems or date arithmetic. "
        "ONLY evaluate visual/layout properties.\n\n"
        "The date box should show 3 text lines (any content is acceptable).\n\n"
        "Check ONLY these visual layout properties:\n"
        "1. Are there 3 visible text lines in the date box? (yes/no)\n"
        "2. Do any lines visually overlap or touch each other? (yes/no)\n"
        "3. Is any text clipped or cut off at the box boundary? (yes/no)\n"
        "4. Is there a clearly visible dark background box with a border? (yes/no)\n"
        "5. Is the text legible (not blurry or too small)? (yes/no)\n\n"
        "Scoring: Start at 100. Deduct 25 per 'yes' for issues 2,3; "
        "deduct 20 for issue 1 (missing lines); deduct 10 for issues 4,5.\n\n"
        "Respond with ONLY a valid JSON object — no markdown, no extra text:\n"
        '{"score": <0-100>, "issues": [], '
        '"lines_overlap": false, "text_cut_off": false, '
        '"persian_readable": true, "gregorian_readable": true, "all_3_lines": true}'
    )

    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        response = _GEMINI_MODEL.generate_content([prompt, pil_img])
        text = response.text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            score = float(data.get("score", 50))
            issues = list(data.get("issues", []))
            if data.get("lines_overlap"):
                issues.append("Lines overlap in rendered image")
            if data.get("text_cut_off"):
                issues.append("Text cut off by box boundary")
            if not data.get("persian_readable", True):
                issues.append("Persian text not clearly readable")
            if not data.get("all_3_lines", True):
                issues.append("Not all 3 date lines are visible")
            log.info(f"[{currency}] Vision iter{iteration}: score={score:.0f}% issues={issues}")
            return score, issues
    except Exception as exc:
        log.error(f"Gemini Vision error [{currency}]: {exc}")

    return 75.0, ["Gemini Vision analysis failed — using default score"]


# ── Screenshot feedback analysis ─────────────────────────────────────────────

def _detect_region_from_text(user_text: str) -> str:
    """Infer which poster region a Persian complaint is about."""
    t = user_text.lower()
    for region, keywords in REGION_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return region
    return "date_box"  # safe default


def _analyze_screenshot_feedback(image_bytes: bytes, user_text: str) -> dict:
    """
    Use Gemini Vision to understand what is visually wrong in a screenshot.
    Returns analysis dict with region, issue_type, description, suggested_adj.
    """
    if not HAS_GEMINI or not image_bytes:
        region = _detect_region_from_text(user_text)
        return {"region": region, "issue_type": "unknown", "description": user_text,
                "suggested_adj": {}, "confidence": 0.3}

    prompt = (
        "You are a visual QC expert analyzing a poster screenshot sent by an admin.\n\n"
        f"Admin complaint: \"{user_text}\"\n\n"
        "The poster has these regions:\n"
        "- date_box: top area with Shamsi date, Persian weekday, Gregorian date\n"
        "- price_box: middle area with exchange rates/prices\n"
        "- logo: top-left or top-right corner brand logo\n"
        "- footer: bottom area with contact/info\n"
        "- spacing: general spacing/alignment issues\n\n"
        "Analyze the screenshot and identify:\n"
        "1. Which region has the problem?\n"
        "2. What type of issue: overlap / cut_off / misaligned / too_small / too_large / wrong_position / spacing\n"
        "3. Brief description of what is visually wrong\n"
        "4. Suggested render adjustments (y_offset: -10 to +10, font_scale: 0.7-1.3, padding_extra: 0-15)\n\n"
        "Respond ONLY with valid JSON (no markdown):\n"
        '{"region": "date_box", "issue_type": "overlap", '
        '"description": "Lines 1 and 2 are visually touching", '
        '"suggested_adj": {"y_offset": 0, "font_scale": 0.88, "padding_extra": 4}, '
        '"confidence": 0.85}'
    )

    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        response = _GEMINI_MODEL.generate_content([prompt, pil_img])
        text = response.text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            log.info(f"Screenshot analysis: {data}")
            return data
    except Exception as exc:
        log.error(f"Screenshot analysis error: {exc}")

    region = _detect_region_from_text(user_text)
    return {"region": region, "issue_type": "unknown", "description": user_text,
            "suggested_adj": {}, "confidence": 0.2}


def _compare_versions_with_gemini(img_a: bytes, img_b: bytes, label_a: str, label_b: str) -> str:
    """Ask Gemini which of two poster images looks better and why."""
    if not HAS_GEMINI:
        return "Gemini not available for comparison."
    prompt = (
        f"Compare these two poster images: '{label_a}' (first) vs '{label_b}' (second).\n"
        "Which looks more professional in terms of layout, date box clarity, and text spacing?\n"
        "Answer in 2 sentences. Start with the version name that looks better."
    )
    try:
        img_a_pil = Image.open(io.BytesIO(img_a))
        img_b_pil = Image.open(io.BytesIO(img_b))
        response = _GEMINI_MODEL.generate_content([prompt, img_a_pil, img_b_pil])
        return response.text.strip()
    except Exception as exc:
        log.error(f"Version comparison error: {exc}")
        return "Comparison failed."


# ── Instruction parser ────────────────────────────────────────────────────────

def _parse_instruction_to_adjustment_sync(instruction: str) -> dict:
    """
    Parse natural language instruction into render adjustments using Gemini.
    Returns dict with keys: y_offset, font_scale, padding_extra (all optional).
    """
    if not HAS_GEMINI:
        return {}

    prompt = (
        f'Parse this poster layout instruction into render adjustments:\n"{instruction}"\n\n'
        "Parameters:\n"
        "  y_offset: pixels to shift text down (+ = down, - = up), range -30 to 30\n"
        "  font_scale: font size multiplier (1.0=normal, 0.9=smaller), range 0.6-1.5\n"
        "  padding_extra: extra padding pixels inside box, range 0-20\n\n"
        "Examples:\n"
        '  "move Persian date slightly lower" → {"y_offset": 6}\n'
        '  "make date text smaller" → {"font_scale": 0.88}\n'
        '  "shift date box up" → {"y_offset": -8}\n'
        '  "add more spacing" → {"padding_extra": 6}\n\n'
        "Respond with ONLY a JSON object (no markdown):\n"
        '{"y_offset": 0, "font_scale": 1.0, "padding_extra": 0}'
    )

    try:
        resp = _GEMINI_MODEL.generate_content(prompt)
        text = resp.text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as exc:
        log.error(f"_parse_instruction error: {exc}")

    return {}


# ── Auto-fix engine ───────────────────────────────────────────────────────────

def _apply_auto_fix(vision_issues: dict, vision_scores: dict, qc_results: dict):
    """
    Update _render_adj based on detected issues.
    Rule-based: overlap → reduce font_scale; text outside box → reduce; etc.
    """
    global _render_adj

    for cur in POSTER_ORDER:
        score = vision_scores.get(cur, 100.0)
        if score >= SCORE_THRESHOLD:
            continue

        vis_issues = vision_issues.get(cur, [])
        qc_errors  = qc_results.get(cur, {}).get("errors", [])
        all_issues = vis_issues + qc_errors

        adj = dict(_render_adj.get(cur, _DEFAULT_ADJ()))

        overlap  = any("overlap" in i.lower() or "L1-L2" in i or "L2-L3" in i for i in all_issues)
        cut_off  = any("cut" in i.lower() or "below" in i.lower() or "outside" in i.lower() for i in all_issues)
        too_wide = any("wider than box" in i.lower() for i in all_issues)
        too_narr = any("too narrow" in i.lower() for i in all_issues)

        if overlap or cut_off or too_wide:
            adj["font_scale"] = round(max(0.6, adj.get("font_scale", 1.0) - 0.06), 3)

        if too_narr:
            adj["font_scale"] = round(min(1.4, adj.get("font_scale", 1.0) + 0.05), 3)

        _render_adj[cur] = adj
        log.info(f"[{cur}] Auto-fix applied: {adj}")


def _adj_description() -> str:
    lines = []
    for cur in POSTER_ORDER:
        adj = _render_adj.get(cur, _DEFAULT_ADJ())
        lines.append(
            f"  {cur}: y_offset={adj['y_offset']:+d}  "
            f"font_scale={adj['font_scale']:.2f}  "
            f"padding_extra={adj['padding_extra']}"
        )
    return "\n".join(lines)


# ── Good layouts store ────────────────────────────────────────────────────────

def save_good_layout(currency: str, image_bytes: bytes, layout: dict, score: float):
    """Save a high-scoring layout for future reference."""
    try:
        ts = datetime.now(TORONTO_TZ).strftime("%Y%m%d_%H%M%S")
        img_path = GOOD_LAYOUTS_DIR / f"{currency}_{ts}_{score:.0f}.jpg"
        img_path.write_bytes(image_bytes)
        meta_path = GOOD_LAYOUTS_DIR / f"{currency}_{ts}_{score:.0f}.json"
        meta_path.write_text(
            json.dumps({
                "currency": currency, "score": score, "timestamp": ts,
                "layout": layout,
                "adjustments": _render_adj.get(currency, _DEFAULT_ADJ()),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info(f"[{currency}] Good layout saved: score={score:.0f}%")
    except Exception as exc:
        log.error(f"save_good_layout error: {exc}")


def _load_best_good_layout(currency: str) -> dict | None:
    """Load the highest-scored good layout adjustments for a currency."""
    best_score = -1.0
    best_adj   = None
    for f in GOOD_LAYOUTS_DIR.glob(f"{currency}_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            s = float(data.get("score", 0))
            if s > best_score:
                best_score = s
                best_adj   = data.get("adjustments")
        except Exception:
            pass
    return best_adj


# ── Preview poster generation ─────────────────────────────────────────────────

def _draw_date_box_3line(
    draw: ImageDraw.ImageDraw,
    px_rgb,
    date_box: list,
    dt: datetime,
    adj: dict = None,
) -> dict:
    """
    Draw restored date box with 3 lines:
      Line 1 — Shamsi date  (۱۴۰۵/۰۲/۲۶)
      Line 2 — Persian weekday  (شنبه)
      Line 3 — Gregorian date  (16 May 2026)
    Accepts optional adj dict: y_offset, font_scale, padding_extra.
    Returns layout dict used by QC engine.
    """
    if adj is None:
        adj = {}
    y_offset      = int(adj.get("y_offset", 0))
    font_scale    = float(adj.get("font_scale", 1.0))
    padding_extra = int(adj.get("padding_extra", 0))

    x1, y1, x2, y2 = date_box
    box_w = x2 - x1
    box_h = y2 - y1

    bg = sample_bg(px_rgb, x1, y1, x2, y2)
    box_bg = (max(0, bg[0] - 25), max(0, bg[1] - 25), max(0, bg[2] - 10))
    draw.rectangle([x1, y1, x2, y2], fill=(*box_bg, 255))
    draw.rectangle([x1, y1, x2, y2], outline=(185, 155, 75, 255), width=2)

    shamsi_raw  = get_shamsi_date(dt)
    weekday_raw = get_persian_weekday(dt)
    gregorian   = get_gregorian_date(dt)

    shamsi_r  = fa(shamsi_raw)
    weekday_r = fa(weekday_raw)

    PADDING = max(4, int(box_h * 0.08)) + padding_extra
    GAP     = max(2, int(box_h * 0.06))
    avail_h = box_h - 2 * PADDING - 2 * GAP
    line_h  = max(10, avail_h // 3)

    fa1_font, fa1_bb, _ = _fit_text(draw, shamsi_r,  FONT_BOLD_FA, int(line_h * 0.90 * font_scale), box_w)
    fa2_font, fa2_bb, _ = _fit_text(draw, weekday_r, FONT_BOLD_FA, int(line_h * 0.85 * font_scale), box_w)
    en_font,  en_bb,  _ = _fit_text(draw, gregorian, FONT_BOLD_EN, int(line_h * 0.78 * font_scale), box_w)

    h1, w1 = fa1_bb[3] - fa1_bb[1], fa1_bb[2] - fa1_bb[0]
    h2, w2 = fa2_bb[3] - fa2_bb[1], fa2_bb[2] - fa2_bb[0]
    h3, w3 = en_bb[3]  - en_bb[1],  en_bb[2]  - en_bb[0]

    total_h = h1 + h2 + h3 + 2 * GAP
    start_y = y1 + max(PADDING, (box_h - total_h) // 2) + y_offset

    yl1 = start_y
    yl2 = yl1 + h1 + GAP
    yl3 = yl2 + h2 + GAP

    xl1 = x1 + (box_w - w1) // 2
    xl2 = x1 + (box_w - w2) // 2
    xl3 = x1 + (box_w - w3) // 2

    SHADOW   = (0, 0, 0, 180)
    COLOR_FA = (242, 222, 148, 255)
    COLOR_EN = (210, 190, 120, 255)

    draw.text((xl1 + 1, yl1 + 1), shamsi_r,  font=fa1_font, fill=SHADOW)
    draw.text((xl1,     yl1),     shamsi_r,  font=fa1_font, fill=COLOR_FA)
    draw.text((xl2 + 1, yl2 + 1), weekday_r, font=fa2_font, fill=SHADOW)
    draw.text((xl2,     yl2),     weekday_r, font=fa2_font, fill=COLOR_FA)
    draw.text((xl3 + 1, yl3 + 1), gregorian, font=en_font,  fill=SHADOW)
    draw.text((xl3,     yl3),     gregorian, font=en_font,  fill=COLOR_EN)

    log.info(
        f"DateBox | shamsi='{shamsi_raw}' weekday='{weekday_raw}' greg='{gregorian}' "
        f"h1={h1} h2={h2} h3={h3} gap={GAP} y_offset={y_offset} font_scale={font_scale:.2f}"
    )

    return {
        "date_box": date_box,
        "shamsi":   shamsi_raw,
        "weekday":  weekday_raw,
        "gregorian": gregorian,
        "y1": yl1, "h1": h1, "w1": w1,
        "y2": yl2, "h2": h2, "w2": w2,
        "y3": yl3, "h3": h3, "w3": w3,
        "box_x1": x1, "box_y1": y1, "box_x2": x2, "box_y2": y2,
    }


def _load_coords() -> dict:
    return json.loads(COORDS_FILE.read_text(encoding="utf-8"))


def generate_preview(currency: str, iteration: int, dt: datetime, adj: dict = None) -> tuple | None:
    template_name = TEMPLATES.get(currency)
    if not template_name:
        return None
    template_path = ASSETS_DIR / template_name
    if not template_path.exists():
        log.error(f"[{currency}] Template missing: {template_path}")
        return None

    coords     = _load_coords()
    cur_coords = coords.get(currency)
    if not cur_coords:
        log.error(f"[{currency}] No coordinates entry")
        return None

    date_box = cur_coords.get("date_box")
    if not date_box:
        log.error(f"[{currency}] No date_box in coordinates")
        return None

    try:
        img    = Image.open(template_path).convert("RGBA")
        draw   = ImageDraw.Draw(img)
        px_rgb = img.convert("RGB").load()

        layout = _draw_date_box_3line(draw, px_rgb, date_box, dt, adj)

        ts  = dt.strftime("%Y%m%d_%H%M%S")
        out = PREVIEWS_DIR / f"iter{iteration}_{currency.lower()}_{ts}.png"
        img.convert("RGB").save(str(out), "PNG")
        log.info(f"[{currency}] Preview saved → {out}")
        return out, layout

    except Exception as exc:
        log.error(f"[{currency}] generate_preview error: {exc}")
        traceback.print_exc()
        return None


def generate_all_previews(iteration: int, render_adj: dict = None) -> list:
    dt = datetime.now(TORONTO_TZ)
    results = []
    for cur in POSTER_ORDER:
        adj    = (render_adj or {}).get(cur, {})
        result = generate_preview(cur, iteration, dt, adj)
        if result:
            path, layout = result
            results.append((cur, path, layout))
        else:
            log.warning(f"[{cur}] Preview generation failed — skipping")
    return results


# ── QC Engine ─────────────────────────────────────────────────────────────────

def qc_single(currency: str, layout: dict, dt: datetime) -> tuple:
    errors = []
    if not layout:
        return False, ["No layout data"]

    box_x1 = layout["box_x1"]
    box_y1 = layout["box_y1"]
    box_x2 = layout["box_x2"]
    box_y2 = layout["box_y2"]
    box_w  = box_x2 - box_x1

    if HAS_JDATE:
        jd     = jdatetime.datetime.fromgregorian(datetime=dt)
        exp_sh = (
            str(jd.year).translate(_FA_DIGITS) + "/"
            + str(jd.month).zfill(2).translate(_FA_DIGITS) + "/"
            + str(jd.day).zfill(2).translate(_FA_DIGITS)
        )
        if layout["shamsi"] != exp_sh:
            errors.append(f"Shamsi wrong: got '{layout['shamsi']}', expected '{exp_sh}'")

    exp_gr = dt.strftime("%-d %B %Y")
    if layout["gregorian"] != exp_gr:
        errors.append(f"Gregorian wrong: got '{layout['gregorian']}', expected '{exp_gr}'")

    exp_wd = _WEEKDAYS[dt.weekday()]
    if layout["weekday"] != exp_wd:
        errors.append(f"Weekday wrong: got '{layout['weekday']}', expected '{exp_wd}'")

    if layout["y1"] + layout["h1"] > layout["y2"]:
        errors.append(f"Overlap L1-L2: bottom={layout['y1']+layout['h1']} > top={layout['y2']}")
    if layout["y2"] + layout["h2"] > layout["y3"]:
        errors.append(f"Overlap L2-L3: bottom={layout['y2']+layout['h2']} > top={layout['y3']}")

    if layout["y1"] < box_y1:
        errors.append(f"Line 1 above box top ({layout['y1']} < {box_y1})")
    if layout["y3"] + layout["h3"] > box_y2 + 2:
        errors.append(f"Line 3 below box bottom ({layout['y3']+layout['h3']} > {box_y2})")

    for label, w in [("Shamsi", layout["w1"]), ("Weekday", layout["w2"]), ("Greg", layout["w3"])]:
        if w < 15:
            errors.append(f"{label} text too narrow ({w}px)")

    for label, w in [("Shamsi", layout["w1"]), ("Weekday", layout["w2"]), ("Greg", layout["w3"])]:
        margin = (box_w - w) / 2
        if margin < 2:
            errors.append(f"{label} text wider than box (margin={margin:.0f}px)")

    return len(errors) == 0, errors


def run_qc(previews: list, dt: datetime) -> dict:
    results = {}
    for currency, path, layout in previews:
        passed, errors = qc_single(currency, layout, dt)
        results[currency] = {"passed": passed, "errors": errors, "path": str(path)}
        status = "PASSED ✅" if passed else f"FAILED ❌ ({len(errors)} issues)"
        log.info(f"[{currency}] QC {status}")
        for e in errors:
            log.warning(f"  • {e}")
    return results


def _qc_summary_text(qc_results: dict) -> str:
    lines = []
    for cur in POSTER_ORDER:
        r = qc_results.get(cur)
        if not r:
            continue
        icon = "✅" if r["passed"] else "❌"
        lines.append(f"{icon} {cur}")
        if not r["passed"]:
            for e in r["errors"][:3]:
                lines.append(f"   • {e}")
    return "\n".join(lines)


# ── Task management ───────────────────────────────────────────────────────────

def _task_id_now() -> str:
    return datetime.now(TORONTO_TZ).strftime("task_%Y%m%d_%H%M%S")


def save_task(task_id: str, instruction: str) -> Path:
    data = {
        "id":          task_id,
        "instruction": instruction,
        "created_at":  datetime.now(TORONTO_TZ).isoformat(),
        "status":      "running",
        "iteration":   0,
        "qc_history":  [],
    }
    p = TASKS_DIR / f"{task_id}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def update_task(task_id: str, updates: dict):
    p = TASKS_DIR / f"{task_id}.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        data.update(updates)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_task(task_id: str) -> dict | None:
    p = TASKS_DIR / f"{task_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def list_tasks(limit: int = 5) -> list:
    files = sorted(TASKS_DIR.glob("*.json"), reverse=True)[:limit]
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def backup_files(task_id: str):
    bdir = BACKUPS_DIR / task_id
    bdir.mkdir(parents=True, exist_ok=True)
    for f in [PROD_PY, COORDS_FILE]:
        if f.exists():
            shutil.copy2(f, bdir / f.name)
    log.info(f"Backup created → {bdir}")


def restore_backup(task_id: str) -> bool:
    bdir = BACKUPS_DIR / task_id
    if not bdir.exists():
        return False
    for f in bdir.iterdir():
        shutil.copy2(f, BASE_DIR / f.name)
    log.info(f"Backup restored ← {bdir}")
    return True


# ── Design task execution — autonomous visual AI loop ─────────────────────────

async def execute_task(task_id: str, instruction: str, chat_id, bot=None):
    """
    Autonomous self-correcting loop (up to MAX_ITERATIONS).
    Posts previews → downloads actual Telegram images → Gemini Vision QC →
    auto-adjusts render params → repeat until score >= SCORE_THRESHOLD or max iters.
    """
    global _current_task, _last_previews, _approved, _last_task_id
    global _render_adj, _prev_adj, _last_scores, _last_posted_ids

    loop = asyncio.get_event_loop()

    async def _msg(text: str):
        # All iteration/QC output is private — never goes to the public channel.
        target = ADMIN_DEBUG_CHAT
        if bot:
            try:
                await bot.send_message(target, text)
                return
            except Exception as exc:
                log.error(f"bot.send_message: {exc}")
        _api_send(target, text)

    _setup_dirs()
    backup_files(task_id)
    _last_task_id = task_id
    _approved     = False

    # Parse instruction → initial render adjustments
    if instruction and HAS_GEMINI:
        await _msg("🧠 در حال پردازش دستور با Gemini AI...")
        parsed = await loop.run_in_executor(None, _parse_instruction_to_adjustment_sync, instruction)
        if parsed:
            for cur in POSTER_ORDER:
                for k, v in parsed.items():
                    if k in _render_adj[cur]:
                        _render_adj[cur][k] = v
            log.info(f"Instruction parsed → {parsed}")
            await _msg(f"⚙️ تنظیمات اولیه از دستور:\n  y_offset={parsed.get('y_offset',0):+}  "
                       f"font_scale={parsed.get('font_scale',1.0):.2f}  "
                       f"padding_extra={parsed.get('padding_extra',0)}")

    toronto_now = datetime.now(TORONTO_TZ)
    all_passed  = False
    final_qc    = {}
    best_score  = 0.0
    _last_scores = {}

    for iteration in range(1, MAX_ITERATIONS + 1):
        async with _task_lock:
            if _current_task and _current_task.get("id") != task_id:
                log.info(f"Task {task_id} stopped at iter {iteration}")
                return

        update_task(task_id, {"iteration": iteration, "status": "generating"})
        await _msg(f"⚙️ در حال تولید پیش‌نمایش — تکرار {iteration} از {MAX_ITERATIONS}...")

        toronto_now = datetime.now(TORONTO_TZ)

        # Save previous state for rollback
        _prev_adj   = {cur: dict(adj) for cur, adj in _render_adj.items()}
        prev_scores = dict(_last_scores)

        # Generate all previews with current adjustments
        previews = []
        for cur in POSTER_ORDER:
            adj    = dict(_render_adj.get(cur, _DEFAULT_ADJ()))
            result = generate_preview(cur, iteration, toronto_now, adj)
            if result:
                path, layout = result
                previews.append((cur, path, layout))
        _last_previews = previews

        if not previews:
            await _msg("❌ خطا در تولید پوستر.")
            update_task(task_id, {"status": "failed"})
            async with _task_lock:
                _current_task = {}
            return

        # Post previews and collect file_ids
        names_fa = {
            "CAD": "کانادا", "USD": "آمریکا", "USACAN": "آمریکا/کانادا",
            "USDT": "USDT", "EUR": "اروپا",
        }
        posted_ids: dict = {}
        for cur, path, layout in previews:
            caption   = f"🖼 پیش‌نمایش تکرار {iteration} — {names_fa.get(cur, cur)}"
            # Post to admin private chat for Vision QC — never to public channel.
            fid_tuple = await loop.run_in_executor(
                None, _post_and_get_file_id, ADMIN_DEBUG_CHAT, path, caption
            )
            if fid_tuple:
                posted_ids[cur] = fid_tuple
                log.info(f"[{cur}] Posted file_id={fid_tuple[0][:20]}...")
            else:
                log.warning(f"[{cur}] Could not get file_id after post")
            await asyncio.sleep(0.6)

        _last_posted_ids = posted_ids

        # Local QC
        update_task(task_id, {"status": "qc_running"})
        qc_results  = run_qc(previews, toronto_now)
        final_qc    = qc_results
        local_ok    = all(r["passed"] for r in qc_results.values())

        # Gemini Vision QC
        vision_scores: dict = {}
        vision_issues: dict = {}

        vision_api_failed = 0
        if _autofix_enabled and HAS_GEMINI and posted_ids:
            await _msg("👁 Gemini Vision در حال بررسی تصاویر ارسال شده...")
            for cur, (file_id, _) in posted_ids.items():
                img_bytes = await loop.run_in_executor(None, _download_telegram_image, file_id)
                if img_bytes:
                    score, issues = await loop.run_in_executor(
                        None, _analyze_with_gemini_vision, cur, img_bytes, iteration
                    )
                    if issues and "Gemini Vision analysis failed" in issues[0]:
                        vision_api_failed += 1
                else:
                    score, issues = 60.0, ["Could not download Telegram image for analysis"]
                    vision_api_failed += 1
                vision_scores[cur] = score
                vision_issues[cur] = issues
                await asyncio.sleep(0.3)

            # If Vision API is completely down, fall back to local QC result
            if vision_api_failed == len(posted_ids):
                log.warning("Gemini Vision API unavailable — falling back to local QC scores")
                await _msg("⚠️ Gemini Vision API در دسترس نیست — استفاده از QC محلی")
                for cur, r in qc_results.items():
                    vision_scores[cur] = 100.0 if r["passed"] else 50.0
                    vision_issues[cur] = r.get("errors", [])
        else:
            # Fallback: local QC → binary score
            for cur, r in qc_results.items():
                vision_scores[cur] = 100.0 if r["passed"] else 50.0
                vision_issues[cur] = r.get("errors", [])

        _last_scores = vision_scores
        avg_score = sum(vision_scores.values()) / len(vision_scores) if vision_scores else 0.0
        min_score = min(vision_scores.values()) if vision_scores else 0.0

        # Rollback if score regressed significantly
        if prev_scores and iteration > 1:
            prev_avg = sum(prev_scores.values()) / len(prev_scores)
            if avg_score < prev_avg - 10:
                log.warning(f"Score regressed {prev_avg:.0f}% → {avg_score:.0f}% — rolling back")
                await _msg(
                    f"⬅️ نمره کاهش یافت ({avg_score:.0f}% < {prev_avg:.0f}%) — "
                    "بازگشت به تنظیمات قبلی"
                )
                _render_adj = {cur: dict(adj) for cur, adj in _prev_adj.items()}
                _last_scores = prev_scores
                avg_score    = prev_avg
                min_score    = min(prev_scores.values())

        # Report scores
        score_lines = "\n".join(
            f"  {'✅' if s >= SCORE_THRESHOLD else '⚠️' if s >= 70 else '❌'} {cur}: {s:.0f}%"
            for cur, s in vision_scores.items()
        )
        await _msg(
            f"📊 نمره Gemini Vision — تکرار {iteration}:\n{score_lines}\n"
            f"میانگین: {avg_score:.0f}%  حداقل: {min_score:.0f}%"
        )

        # Save QC report
        rpt_path = REPORTS_DIR / f"{task_id}_iter{iteration}.json"
        rpt_path.write_text(
            json.dumps({
                "task_id": task_id, "iteration": iteration,
                "timestamp": toronto_now.isoformat(),
                "qc": qc_results,
                "vision_scores": vision_scores,
                "vision_issues": vision_issues,
                "adjustments": {cur: dict(adj) for cur, adj in _render_adj.items()},
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        update_task(task_id, {
            "status": "qc_done",
            "qc_history": (load_task(task_id) or {}).get("qc_history", [])
                          + [{"iteration": iteration, "passed": local_ok,
                              "avg_score": round(avg_score, 1)}],
        })

        # Success condition
        if min_score >= SCORE_THRESHOLD:
            all_passed  = True
            best_score  = avg_score
            # Save good layouts
            for cur, (file_id, _) in posted_ids.items():
                img_bytes = await loop.run_in_executor(None, _download_telegram_image, file_id)
                if img_bytes:
                    lay = next((l for c, _, l in previews if c == cur), {})
                    save_good_layout(cur, img_bytes, lay, vision_scores[cur])
            break

        if iteration < MAX_ITERATIONS:
            # Apply auto-fix for next iteration
            _apply_auto_fix(vision_issues, vision_scores, qc_results)
            await _msg(
                f"🔧 خودکارسازی تنظیمات — تکرار {iteration + 1}:\n" + _adj_description()
            )
            await asyncio.sleep(2)

    async with _task_lock:
        _current_task = {}

    if all_passed:
        update_task(task_id, {"status": "passed"})
        await _msg(
            f"✅ نتیجه نهایی تایید شد.\n"
            f"نمره: {best_score:.0f}%\n\n"
            "برای اعمال به نسخه تولید: /design_apply"
        )
    else:
        failed_curs = [c for c, r in final_qc.items() if not r.get("passed")]
        last_avg    = sum(_last_scores.values()) / len(_last_scores) if _last_scores else 0
        update_task(task_id, {"status": "needs_review"})
        await _msg(
            f"⚠️ نیاز به بررسی دستی دارد.\n"
            f"پوسترهای با مشکل: {', '.join(failed_curs) or 'نامشخص'}\n"
            f"آخرین نمره: {last_avg:.0f}%\n"
            "برای رفع دستی: /design_fix_prompt <راهنمایی>"
        )


# ── Screenshot-driven fix loop ────────────────────────────────────────────────

async def execute_screenshot_fix_task(
    task_id: str,
    screenshot_bytes: bytes,
    user_text: str,
    bot=None,
    focused_currency: str | None = None,
):
    """
    Analyze a screenshot complaint, apply targeted fix, iterate until resolved.
    All output → ADMIN_DEBUG_CHAT only.
    """
    global _current_task, _last_previews, _last_task_id
    global _render_adj, _prev_adj, _last_scores, _last_posted_ids, _focused_region

    loop = asyncio.get_event_loop()

    async def _msg(text: str):
        if bot:
            try:
                await bot.send_message(ADMIN_DEBUG_CHAT, text)
                return
            except Exception as exc:
                log.error(f"bot.send_message: {exc}")
        _api_send(ADMIN_DEBUG_CHAT, text)

    _setup_dirs()
    backup_files(task_id)
    _last_task_id = task_id

    # Save screenshot to disk
    ss_path = SCREENSHOTS_DIR / f"{task_id}.jpg"
    ss_path.write_bytes(screenshot_bytes)

    await _msg("🔍 در حال تحلیل اسکرین‌شات با Gemini Vision...")
    analysis = await loop.run_in_executor(
        None, _analyze_screenshot_feedback, screenshot_bytes, user_text
    )

    region      = analysis.get("region", "date_box")
    issue_type  = analysis.get("issue_type", "unknown")
    description = analysis.get("description", user_text)
    suggested   = analysis.get("suggested_adj", {})
    confidence  = analysis.get("confidence", 0.5)

    _focused_region = region

    await _msg(
        f"📋 تحلیل اسکرین‌شات:\n"
        f"  ناحیه: {region}\n"
        f"  نوع مشکل: {issue_type}\n"
        f"  توضیح: {description}\n"
        f"  اطمینان: {confidence*100:.0f}%\n"
        f"  تنظیم پیشنهادی: {suggested}"
    )

    # Apply suggested adjustments to target currencies
    currencies_to_fix = [focused_currency] if focused_currency else POSTER_ORDER
    if suggested:
        for cur in currencies_to_fix:
            adj = dict(_render_adj.get(cur, _DEFAULT_ADJ()))
            for k, v in suggested.items():
                if k in adj:
                    adj[k] = v
            _render_adj[cur] = adj
        await _msg(f"⚙️ تنظیمات اعمال شد برای: {', '.join(currencies_to_fix)}")

    # Run the standard iteration loop
    toronto_now = datetime.now(TORONTO_TZ)
    all_passed  = False
    best_score  = 0.0
    _last_scores = {}
    vfm_entry   = {
        "id": task_id,
        "timestamp": toronto_now.isoformat(),
        "user_text": user_text,
        "screenshot_path": str(ss_path),
        "detected_region": region,
        "issue_type": issue_type,
        "description": description,
        "adjustments_before": {cur: dict(_render_adj.get(cur, _DEFAULT_ADJ())) for cur in POSTER_ORDER},
        "adjustments_after": {},
        "score_before": 0.0,
        "score_after": 0.0,
        "fix_successful": False,
        "preferred": False,
    }

    for iteration in range(1, MAX_ITERATIONS + 1):
        async with _task_lock:
            if _current_task and _current_task.get("id") != task_id:
                return

        await _msg(f"⚙️ تکرار {iteration} — تولید پیش‌نمایش...")
        toronto_now = datetime.now(TORONTO_TZ)
        _prev_adj   = {cur: dict(adj) for cur, adj in _render_adj.items()}
        prev_scores = dict(_last_scores)

        previews = []
        for cur in POSTER_ORDER:
            result = generate_preview(cur, iteration, toronto_now, dict(_render_adj.get(cur, _DEFAULT_ADJ())))
            if result:
                path, layout = result
                previews.append((cur, path, layout))
        _last_previews = previews

        if not previews:
            await _msg("❌ خطا در تولید پوستر.")
            break

        # Post privately for Vision QC
        posted_ids: dict = {}
        for cur, path, _ in previews:
            fid_tuple = await loop.run_in_executor(
                None, _post_and_get_file_id, ADMIN_DEBUG_CHAT, path,
                f"🖼 تکرار {iteration} — {cur} (اسکرین‌شات فیکس)"
            )
            if fid_tuple:
                posted_ids[cur] = fid_tuple
            await asyncio.sleep(0.5)
        _last_posted_ids = posted_ids

        # Local QC
        qc_results = run_qc(previews, toronto_now)

        # Gemini Vision QC
        vision_scores: dict = {}
        vision_issues: dict = {}
        if _autofix_enabled and HAS_GEMINI and posted_ids:
            for cur, (file_id, _) in posted_ids.items():
                img_bytes = await loop.run_in_executor(None, _download_telegram_image, file_id)
                if img_bytes:
                    score, issues = await loop.run_in_executor(
                        None, _analyze_with_gemini_vision, cur, img_bytes, iteration
                    )
                else:
                    score, issues = 60.0, ["Download failed"]
                vision_scores[cur] = score
                vision_issues[cur] = issues
                await asyncio.sleep(0.3)
        else:
            for cur, r in qc_results.items():
                vision_scores[cur] = 100.0 if r["passed"] else 50.0
                vision_issues[cur] = r.get("errors", [])

        _last_scores = vision_scores
        avg_score = sum(vision_scores.values()) / len(vision_scores) if vision_scores else 0.0
        min_score = min(vision_scores.values()) if vision_scores else 0.0

        if prev_scores and iteration > 1:
            prev_avg = sum(prev_scores.values()) / len(prev_scores)
            if avg_score < prev_avg - 10:
                log.warning(f"Screenshot fix: score regressed — rolling back")
                await _msg(f"⬅️ نمره کاهش یافت ({avg_score:.0f}% < {prev_avg:.0f}%) — بازگشت")
                _render_adj  = {cur: dict(adj) for cur, adj in _prev_adj.items()}
                _last_scores = prev_scores
                avg_score    = prev_avg
                min_score    = min(prev_scores.values())

        score_lines = "\n".join(
            f"  {'✅' if s >= SCORE_THRESHOLD else '⚠️'} {c}: {s:.0f}%"
            for c, s in vision_scores.items()
        )
        await _msg(f"📊 نمره تکرار {iteration}:\n{score_lines}\nمیانگین: {avg_score:.0f}%")

        update_task(task_id, {"iteration": iteration, "status": "qc_done"})

        if min_score >= SCORE_THRESHOLD:
            all_passed = True
            best_score = avg_score
            for cur, (file_id, _) in posted_ids.items():
                img_bytes = await loop.run_in_executor(None, _download_telegram_image, file_id)
                if img_bytes:
                    lay = next((l for c, _, l in previews if c == cur), {})
                    save_good_layout(cur, img_bytes, lay, vision_scores[cur])
            break

        if iteration < MAX_ITERATIONS:
            _apply_auto_fix(vision_issues, vision_scores, qc_results)
            await asyncio.sleep(1)

    async with _task_lock:
        _current_task = {}

    vfm_entry["adjustments_after"] = {cur: dict(_render_adj.get(cur, _DEFAULT_ADJ())) for cur in POSTER_ORDER}
    vfm_entry["score_after"]       = best_score
    vfm_entry["fix_successful"]    = all_passed
    _append_vfm(vfm_entry)

    if all_passed:
        update_task(task_id, {"status": "passed"})
        await _msg(
            f"✅ فیکس اسکرین‌شات موفق!\n"
            f"نمره: {best_score:.0f}%\n"
            f"ناحیه رفع شده: {region}\n\n"
            "برای تایید عمومی: /design_approve\n"
            "برای اعمال به تولید: /design_apply"
        )
    else:
        last_avg = sum(_last_scores.values()) / len(_last_scores) if _last_scores else 0
        update_task(task_id, {"status": "needs_review"})
        await _msg(
            f"⚠️ نیاز به بررسی بیشتر دارد.\n"
            f"آخرین نمره: {last_avg:.0f}%\n"
            "راهنمایی دستی: /design_fix_prompt <متن>"
        )


# ── Production apply ──────────────────────────────────────────────────────────

_NEW_DRAW_DATE_FUNC = '''
def _draw_date_in_box(
    draw: ImageDraw.ImageDraw,
    px_rgb,
    date_box: list,
    currency: str,
    toronto_now: datetime,
) -> dict:
    """3-line date box: Shamsi / Persian weekday / Gregorian."""
    _FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    _WEEKDAYS  = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یک‌شنبه"]

    x1, y1, x2, y2 = date_box
    box_w = x2 - x1
    box_h = y2 - y1

    bg = sample_zone_bg(px_rgb, y1, y2, x1, x2)
    box_bg = (max(0, bg[0] - 25), max(0, bg[1] - 25), max(0, bg[2] - 10))
    draw.rectangle([x1, y1, x2, y2], fill=(*box_bg, 255))
    draw.rectangle([x1, y1, x2, y2], outline=(185, 155, 75, 255), width=2)

    if HAS_JDATE:
        jd    = jdatetime.datetime.fromgregorian(datetime=toronto_now)
        year  = str(jd.year).translate(_FA_DIGITS)
        month = str(jd.month).zfill(2).translate(_FA_DIGITS)
        day   = str(jd.day).zfill(2).translate(_FA_DIGITS)
        shamsi_raw = f"{year}/{month}/{day}"
    else:
        shamsi_raw = toronto_now.strftime("%Y/%m/%d")

    weekday_raw = _WEEKDAYS[toronto_now.weekday()]
    gregorian   = toronto_now.strftime("%-d %B %Y")

    shamsi_r  = fa(shamsi_raw)
    weekday_r = fa(weekday_raw)

    PADDING = max(4, int(box_h * 0.08))
    GAP     = max(2, int(box_h * 0.06))
    avail_h = box_h - 2 * PADDING - 2 * GAP
    line_h  = max(10, avail_h // 3)

    def _fit(text, fpath, start, min_s=9):
        sz = start
        while sz >= min_s:
            fnt = load_font(fpath, sz)
            bb  = draw.textbbox((0, 0), text, font=fnt)
            if (bb[2] - bb[0]) <= box_w - 12:
                return fnt, bb, sz
            sz -= 1
        fnt = load_font(fpath, min_s)
        return fnt, draw.textbbox((0, 0), text, font=fnt), min_s

    fa1_font, fa1_bb, _ = _fit(shamsi_r,  FONT_BOLD,    int(line_h * 0.90))
    fa2_font, fa2_bb, _ = _fit(weekday_r, FONT_BOLD,    int(line_h * 0.85))
    en_font,  en_bb,  _ = _fit(gregorian, FONT_BOLD_EN, int(line_h * 0.78))

    h1, w1 = fa1_bb[3] - fa1_bb[1], fa1_bb[2] - fa1_bb[0]
    h2, w2 = fa2_bb[3] - fa2_bb[1], fa2_bb[2] - fa2_bb[0]
    h3, w3 = en_bb[3]  - en_bb[1],  en_bb[2]  - en_bb[0]

    total_h = h1 + h2 + h3 + 2 * GAP
    start_y = y1 + max(PADDING, (box_h - total_h) // 2)

    yl1 = start_y; yl2 = yl1 + h1 + GAP; yl3 = yl2 + h2 + GAP
    xl1 = x1 + (box_w - w1) // 2; xl2 = x1 + (box_w - w2) // 2; xl3 = x1 + (box_w - w3) // 2

    SHADOW   = (0, 0, 0, 180)
    COLOR_FA = (242, 222, 148, 255)
    COLOR_EN = (210, 190, 120, 255)

    draw.text((xl1+1, yl1+1), shamsi_r,  font=fa1_font, fill=SHADOW)
    draw.text((xl1,   yl1),   shamsi_r,  font=fa1_font, fill=COLOR_FA)
    draw.text((xl2+1, yl2+1), weekday_r, font=fa2_font, fill=SHADOW)
    draw.text((xl2,   yl2),   weekday_r, font=fa2_font, fill=COLOR_FA)
    draw.text((xl3+1, yl3+1), gregorian, font=en_font,  fill=SHADOW)
    draw.text((xl3,   yl3),   gregorian, font=en_font,  fill=COLOR_EN)

    return {
        "date_box": date_box, "date_fa": shamsi_raw + " " + weekday_raw,
        "date_en": gregorian, "fa_y": yl1, "fa_h": h1, "fa_w": w1, "fa_size": 0,
        "en_y": yl3, "en_h": h3, "en_w": w3, "en_size": 0, "bg": bg,
        "shamsi": shamsi_raw, "weekday": weekday_raw, "gregorian": gregorian,
        "y1": yl1, "h1": h1, "w1": w1, "y2": yl2, "h2": h2, "w2": w2,
        "y3": yl3, "h3": h3, "w3": w3,
    }
'''

_PROD_MARKER_START = "def _draw_date_in_box("
_PROD_MARKER_END   = "\ndef _qc_date("


def apply_to_production() -> tuple:
    src = PROD_PY.read_text(encoding="utf-8")
    start_idx = src.find(_PROD_MARKER_START)
    end_idx   = src.find(_PROD_MARKER_END, start_idx)
    if start_idx == -1:
        return False, "_draw_date_in_box not found in rate_graphic_publisher.py"
    if end_idx == -1:
        return False, "_qc_date marker not found — cannot safely patch"
    patched = src[:start_idx] + _NEW_DRAW_DATE_FUNC.strip() + "\n\n" + src[end_idx:]
    PROD_PY.write_text(patched, encoding="utf-8")
    log.info("rate_graphic_publisher.py patched with 3-line date box")
    return True, "rate_graphic_publisher.py updated — restart rate-graphic-publisher to apply."


# ── Telegram command handlers ─────────────────────────────────────────────────

async def cmd_design_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    global _current_task, _fix_hints

    instruction = " ".join(ctx.args).strip() if ctx.args else ""
    if not instruction:
        await update.message.reply_text(
            "⚠️ لطفاً دستور طراحی را وارد کنید.\n"
            "مثال: /design_task Restore the original date box on all posters"
        )
        return

    async with _task_lock:
        if _current_task:
            await update.message.reply_text(
                f"⚠️ یک وظیفه در حال اجرا است: {_current_task.get('id')}\n"
                "برای توقف: /design_stop"
            )
            return
        task_id = _task_id_now()
        _current_task = {"id": task_id, "instruction": instruction}
        _fix_hints = []

    save_task(task_id, instruction)
    await update.message.reply_text(
        f"✅ وظیفه طراحی شروع شد:\n`{task_id}`\n\n"
        f"📋 دستور:\n{instruction}\n\n"
        "⏳ در حال پشتیبان‌گیری و تولید پیش‌نمایش...",
        parse_mode="Markdown",
    )

    chat_id = update.effective_chat.id
    asyncio.create_task(execute_task(task_id, instruction, chat_id, ctx.bot))


async def cmd_design_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    async with _task_lock:
        ct = dict(_current_task)

    if not ct:
        tasks = list_tasks(1)
        if tasks:
            t = tasks[0]
            await update.message.reply_text(
                f"📋 آخرین وظیفه:\nID: `{t['id']}`\n"
                f"وضعیت: {t['status']}\n"
                f"تکرار: {t.get('iteration', 0)}\n"
                f"دستور: {t['instruction'][:100]}",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("هیچ وظیفه‌ای در حال اجرا نیست.")
        return

    t = load_task(ct["id"]) or ct
    await update.message.reply_text(
        f"⚙️ وظیفه فعال:\nID: `{ct['id']}`\n"
        f"وضعیت: {t.get('status', 'running')}\n"
        f"تکرار: {t.get('iteration', 0)} از {MAX_ITERATIONS}\n"
        f"دستور: {ct['instruction'][:100]}",
        parse_mode="Markdown",
    )


async def cmd_design_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    global _current_task
    async with _task_lock:
        ct = dict(_current_task)
        _current_task = {}

    if ct:
        update_task(ct["id"], {"status": "stopped"})
        await update.message.reply_text(f"🛑 وظیفه متوقف شد: `{ct['id']}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("هیچ وظیفه‌ای در حال اجرا نیست.")


async def cmd_design_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    tasks = list_tasks(1)
    if not tasks:
        await update.message.reply_text("هیچ وظیفه‌ای یافت نشد.")
        return
    t = tasks[0]
    qc_hist = t.get("qc_history", [])
    qc_text = ""
    if qc_hist:
        last_qc = qc_hist[-1]
        icon    = "✅" if last_qc.get("passed") else "❌"
        score   = last_qc.get("avg_score", "?")
        qc_text = f"\nآخرین QC: {icon} تکرار {last_qc.get('iteration','?')} — نمره: {score}%"
    await update.message.reply_text(
        f"📋 آخرین وظیفه:\nID: `{t['id']}`\n"
        f"وضعیت: {t['status']}\n"
        f"تکرار: {t.get('iteration', 0)}{qc_text}\n"
        f"ایجاد شده: {t['created_at']}\n"
        f"دستور: {t['instruction'][:150]}",
        parse_mode="Markdown",
    )


async def cmd_design_qc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not _last_previews:
        await update.message.reply_text("هیچ پیش‌نمایشی برای QC یافت نشد.")
        return
    dt      = datetime.now(TORONTO_TZ)
    results = run_qc(_last_previews, dt)
    summary = _qc_summary_text(results)
    all_ok  = all(r["passed"] for r in results.values())
    header  = "✅ همه QC تایید شد!" if all_ok else "❌ برخی QC ناموفق بود:"
    await update.message.reply_text(f"📊 نتیجه QC:\n{header}\n{summary}")


async def cmd_design_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    try:
        lines  = _LOG_FILE.read_text(encoding="utf-8").strip().split("\n")
        recent = "\n".join(lines[-30:])
        if len(recent) > 3800:
            recent = recent[-3800:]
        await update.message.reply_text(f"📄 آخرین لاگ‌ها:\n```\n{recent}\n```", parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"خطا در خواندن لاگ: {exc}")


async def cmd_design_apply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not _last_previews:
        await update.message.reply_text("هیچ پیش‌نمایش تایید شده‌ای یافت نشد.")
        return
    ts   = datetime.now(TORONTO_TZ).strftime("apply_%Y%m%d_%H%M%S")
    bdir = BACKUPS_DIR / ts
    bdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROD_PY, bdir / PROD_PY.name)
    ok, msg = apply_to_production()
    if ok:
        await update.message.reply_text(
            f"✅ تغییرات به نسخه تولید اعمال شد.\n{msg}\n\n"
            "PM2 را ری‌استارت کنید:\n`pm2 restart rate-graphic-publisher`",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(f"❌ خطا در اعمال تغییرات:\n{msg}")


async def cmd_design_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    global _approved
    _approved = True

    if not _last_previews:
        await update.message.reply_text(
            "✅ تایید ثبت شد اما هیچ پیش‌نمایشی برای انتشار یافت نشد.\n"
            "ابتدا /design_task را اجرا کنید."
        )
        return

    await update.message.reply_text(
        "✅ تایید شد — در حال انتشار پوسترهای نهایی در کانال عمومی..."
    )

    loop = asyncio.get_event_loop()
    names_fa = {
        "CAD": "کانادا", "USD": "آمریکا", "USACAN": "آمریکا/کانادا",
        "USDT": "USDT", "EUR": "اروپا",
    }
    published = []
    for cur, path, _ in _last_previews:
        caption = f"🖼 طراحی تایید‌شده — {names_fa.get(cur, cur)}"
        await loop.run_in_executor(None, _pub_photo, path, caption)
        published.append(cur)
        await asyncio.sleep(0.8)

    log.info(f"Published to {PUBLIC_CHANNEL}: {published}")
    await update.message.reply_text(
        f"📢 منتشر شد در {PUBLIC_CHANNEL}:\n"
        + ", ".join(published) + "\n\n"
        "برای اعمال به نسخه تولید: /design_apply"
    )


async def cmd_design_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    global _approved
    _approved = False
    reason = " ".join(ctx.args).strip() if ctx.args else "دلیلی ذکر نشده"
    await update.message.reply_text(
        f"❌ پیش‌نمایش رد شد.\nدلیل: {reason}\n\n"
        "برای اضافه کردن راهنمایی: /design_fix_prompt <راهنمایی>\n"
        "سپس مجدداً: /design_task <دستور>"
    )


async def cmd_design_fix_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    global _fix_hints
    hint = " ".join(ctx.args).strip() if ctx.args else ""
    if not hint:
        await update.message.reply_text("لطفاً راهنمایی را وارد کنید.\nمثال: /design_fix_prompt متن تاریخ خیلی کوچک است")
        return
    _fix_hints.append(hint)
    await update.message.reply_text(f"✅ راهنمایی اضافه شد: {hint}\nتعداد: {len(_fix_hints)}")


async def cmd_design_autofix_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    global _autofix_enabled
    _autofix_enabled = True
    gemini_ok = "✅ Gemini در دسترس است." if HAS_GEMINI else "⚠️ GEMINI_API_KEY پیکربندی نشده."
    await update.message.reply_text(
        f"✅ Gemini Vision QC فعال شد.\n{gemini_ok}\n"
        "تصاویر پست‌شده در Telegram برای تحلیل بصری دانلود می‌شوند."
    )


async def cmd_design_autofix_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    global _autofix_enabled
    _autofix_enabled = False
    await update.message.reply_text(
        "⏸ Gemini Vision QC غیرفعال شد.\n"
        "فقط QC محلی اجرا می‌شود (سریع‌تر، بدون تحلیل بصری)."
    )


async def cmd_design_live_qc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Download last posted Telegram images and re-analyze with Gemini Vision."""
    if not _is_allowed(update):
        return
    if not _last_posted_ids:
        await update.message.reply_text("هیچ تصویر ارسال‌شده‌ای یافت نشد. ابتدا /design_task را اجرا کنید.")
        return
    if not HAS_GEMINI:
        await update.message.reply_text("⚠️ Gemini در دسترس نیست.")
        return

    await update.message.reply_text("👁 در حال دانلود و تحلیل تصاویر آخرین ارسال...")
    loop = asyncio.get_event_loop()
    results = []
    for cur, (file_id, _) in _last_posted_ids.items():
        img_bytes = await loop.run_in_executor(None, _download_telegram_image, file_id)
        if img_bytes:
            score, issues = await loop.run_in_executor(
                None, _analyze_with_gemini_vision, cur, img_bytes, 0
            )
            icon = "✅" if score >= SCORE_THRESHOLD else "⚠️" if score >= 70 else "❌"
            results.append(f"{icon} {cur}: {score:.0f}%")
            for iss in issues[:2]:
                results.append(f"   • {iss}")
        else:
            results.append(f"❌ {cur}: دانلود ناموفق")
        await asyncio.sleep(0.3)

    await update.message.reply_text("📊 Live QC — تحلیل بصری:\n" + "\n".join(results))


async def cmd_design_compare(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Compare current scores vs best known good layout scores."""
    if not _is_allowed(update):
        return

    lines = ["📊 مقایسه نمرات — فعلی vs بهترین ذخیره‌شده:"]
    for cur in POSTER_ORDER:
        cur_score = _last_scores.get(cur)
        best_adj  = _load_best_good_layout(cur)
        cur_str   = f"{cur_score:.0f}%" if cur_score is not None else "N/A"
        best_str  = "موجود" if best_adj else "ندارد"
        lines.append(f"  {cur}: فعلی={cur_str}  بهترین={best_str}")

    if _last_scores:
        avg = sum(_last_scores.values()) / len(_last_scores)
        lines.append(f"\nمیانگین فعلی: {avg:.0f}%")

    await update.message.reply_text("\n".join(lines))


async def cmd_design_restore_last_good(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Restore render adjustments from the best known good layout."""
    if not _is_allowed(update):
        return
    global _render_adj

    restored = []
    for cur in POSTER_ORDER:
        best = _load_best_good_layout(cur)
        if best:
            _render_adj[cur] = best
            restored.append(cur)

    if restored:
        await update.message.reply_text(
            f"✅ تنظیمات بهترین layout برای {', '.join(restored)} بازیابی شد.\n"
            f"تنظیمات جدید:\n{_adj_description()}\n\n"
            "برای اعمال: /design_task <دستور>"
        )
    else:
        await update.message.reply_text(
            "⚠️ هیچ layout ذخیره‌شده‌ای یافت نشد.\n"
            "پس از موفقیت یک وظیفه، layout خودکار ذخیره می‌شود."
        )


async def cmd_design_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show current render adjustments, last scores, and layout info."""
    if not _is_allowed(update):
        return

    lines = [
        f"🔧 تنظیمات render فعلی:\n{_adj_description()}",
        "",
        f"👁 Gemini Vision: {'فعال ✅' if _autofix_enabled else 'غیرفعال ⏸'}",
        f"Gemini API: {'موجود ✅' if HAS_GEMINI else 'غیرموجود ❌'}",
        "",
    ]

    if _last_scores:
        score_lines = "\n".join(f"  {c}: {s:.0f}%" for c, s in _last_scores.items())
        lines.append(f"📊 آخرین نمرات Vision:\n{score_lines}")
    else:
        lines.append("📊 هنوز نمره‌ای ثبت نشده.")

    if _last_posted_ids:
        ids_lines = "\n".join(f"  {c}: msg_id={mid}" for c, (_, mid) in _last_posted_ids.items())
        lines.append(f"\n📨 آخرین msg_id ها:\n{ids_lines}")

    await update.message.reply_text("\n".join(lines))


async def cmd_design_score(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show last Gemini Vision scores."""
    if not _is_allowed(update):
        return
    if not _last_scores:
        await update.message.reply_text("هنوز هیچ نمره‌ای از Gemini Vision ثبت نشده.\nاجرا کنید: /design_task")
        return

    avg = sum(_last_scores.values()) / len(_last_scores)
    score_lines = "\n".join(
        f"  {'✅' if s >= SCORE_THRESHOLD else '⚠️' if s >= 70 else '❌'} {c}: {s:.0f}%"
        for c, s in _last_scores.items()
    )
    await update.message.reply_text(
        f"📊 آخرین نمرات Gemini Vision:\n{score_lines}\n\nمیانگین: {avg:.0f}%"
    )


# ── New screenshot + comparison handlers ─────────────────────────────────────

async def handle_design_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Handles a photo message from admin in private chat.
    Downloads it, stores as pending screenshot, auto-analyzes with Gemini.
    """
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return

    photos = update.message.photo
    if not photos:
        return
    # Take the largest (best quality) photo
    file_id  = photos[-1].file_id
    caption  = (update.message.caption or "").strip()

    _pending_screenshot[user.id] = {
        "file_id":   file_id,
        "text":      caption,
        "timestamp": datetime.now(TORONTO_TZ).isoformat(),
    }

    await update.message.reply_text(
        "📸 اسکرین‌شات دریافت شد.\n"
        + (f"متن: «{caption}»\n" if caption else "")
        + "\n"
        "برای آنالیز خودکار و رفع مشکل:\n"
        "/design_fix_from_screenshot\n\n"
        "یا متن توضیح مشکل را بفرستید تا AI تشخیص دهد."
    )


async def cmd_design_fix_from_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Analyze pending screenshot and run auto-fix loop.
    Usage: /design_fix_from_screenshot [extra description]
    """
    if not _is_allowed(update):
        return
    global _current_task, _fix_hints

    user = update.effective_user
    extra_text = " ".join(ctx.args).strip() if ctx.args else ""

    pending = _pending_screenshot.get(user.id if user else ADMIN_ID)
    if not pending:
        await update.message.reply_text(
            "⚠️ هیچ اسکرین‌شاتی در انتظار نیست.\n"
            "لطفاً ابتدا اسکرین‌شات را در چت خصوصی ارسال کنید."
        )
        return

    async with _task_lock:
        if _current_task:
            await update.message.reply_text(
                f"⚠️ یک وظیفه در حال اجراست: {_current_task.get('id')}\n"
                "برای توقف: /design_stop"
            )
            return
        task_id = _task_id_now()
        complaint = extra_text or pending.get("text") or "مشکل بصری گزارش شده"
        _current_task = {"id": task_id, "instruction": f"screenshot_fix: {complaint}"}
        _fix_hints = []

    save_task(task_id, f"screenshot_fix: {complaint}")

    await update.message.reply_text(
        f"🔍 آنالیز اسکرین‌شات شروع شد.\n"
        f"شناسه: `{task_id}`\n"
        f"شکایت: {complaint}\n\n"
        "⏳ در حال دانلود و آنالیز تصویر...",
        parse_mode="Markdown",
    )

    file_id = pending["file_id"]
    loop    = asyncio.get_event_loop()
    img_bytes = await loop.run_in_executor(None, _download_telegram_image, file_id)

    if not img_bytes:
        await update.message.reply_text("❌ دانلود اسکرین‌شات ناموفق بود.")
        async with _task_lock:
            _current_task = {}
        return

    _pending_screenshot.pop(user.id if user else ADMIN_ID, None)

    asyncio.create_task(
        execute_screenshot_fix_task(task_id, img_bytes, complaint, bot=ctx.bot)
    )


async def cmd_design_restore_best(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Restore adjustments from the highest-scored good layout ever recorded."""
    if not _is_allowed(update):
        return
    global _render_adj

    restored = []
    for cur in POSTER_ORDER:
        best = _load_best_good_layout(cur)
        if best:
            _render_adj[cur] = best
            restored.append(cur)

    if restored:
        await update.message.reply_text(
            f"✅ بهترین layout بازیابی شد: {', '.join(restored)}\n"
            f"تنظیمات:\n{_adj_description()}\n\n"
            "برای اعمال: /design_task <دستور>"
        )
    else:
        await update.message.reply_text(
            "⚠️ هیچ layout خوبی هنوز ذخیره نشده.\n"
            "پس از موفقیت یک وظیفه، به‌طور خودکار ذخیره می‌شود."
        )


async def cmd_design_learn_style(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Review visual feedback memory and summarize preferred style patterns.
    /design_learn_style
    """
    if not _is_allowed(update):
        return

    entries = _load_vfm()
    if not entries:
        await update.message.reply_text("⚠️ هنوز هیچ بازخورد بصری ثبت نشده.")
        return

    successful = [e for e in entries if e.get("fix_successful")]
    preferred  = [e for e in entries if e.get("preferred")]

    lines = [
        f"🎨 یادگیری سبک بصری:",
        f"  کل بازخوردها: {len(entries)}",
        f"  فیکس‌های موفق: {len(successful)}",
        f"  مورد ترجیح: {len(preferred)}",
        "",
    ]

    if successful:
        # Show average adjustments from successful fixes
        avg_yo = sum(e.get("adjustments_after", {}).get("CAD", {}).get("y_offset", 0)
                     for e in successful) / len(successful)
        avg_fs = sum(e.get("adjustments_after", {}).get("CAD", {}).get("font_scale", 1.0)
                     for e in successful) / len(successful)
        lines.append(f"  میانگین y_offset موفق: {avg_yo:+.1f}")
        lines.append(f"  میانگین font_scale موفق: {avg_fs:.2f}")

    region_counts: dict = {}
    for e in entries:
        r = e.get("detected_region", "unknown")
        region_counts[r] = region_counts.get(r, 0) + 1
    if region_counts:
        top = sorted(region_counts.items(), key=lambda x: -x[1])
        lines.append(f"\n  بیشترین مشکل: {top[0][0]} ({top[0][1]}×)")

    lines.append("\nبرای بازگشت به بهترین حالت: /design_restore_best")
    await update.message.reply_text("\n".join(lines))


async def cmd_design_focus_area(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Set a focused region for the next iteration.
    /design_focus_area date_box
    /design_focus_area price_box
    """
    if not _is_allowed(update):
        return
    global _focused_region

    if not ctx.args:
        known = ", ".join(REGION_KEYWORDS.keys())
        current = _focused_region or "(هیچ)"
        await update.message.reply_text(
            f"ناحیه فعلی: {current}\n\n"
            f"ناحیه‌های مجاز: {known}\n\n"
            "مثال: /design_focus_area date_box"
        )
        return

    region = ctx.args[0].strip().lower()
    if region not in REGION_KEYWORDS and region != "clear":
        await update.message.reply_text(
            f"❌ ناحیه نامعتبر: {region}\n"
            f"مجاز: {', '.join(REGION_KEYWORDS.keys())} یا clear"
        )
        return

    if region == "clear":
        _focused_region = ""
        await update.message.reply_text("✅ تمرکز ناحیه پاک شد — همه نواحی بررسی می‌شوند.")
    else:
        _focused_region = region
        await update.message.reply_text(
            f"✅ تمرکز روی: {region}\n"
            "تکرارهای بعدی به این ناحیه تمرکز خواهند داشت."
        )


# ── Handler registration ──────────────────────────────────────────────────────

def register_design_handlers(app) -> None:
    from telegram.ext import CommandHandler as _CH, MessageHandler as _MH, filters as _F
    app.add_handler(_CH("design_task",             cmd_design_task))
    app.add_handler(_CH("design_status",           cmd_design_status))
    app.add_handler(_CH("design_stop",             cmd_design_stop))
    app.add_handler(_CH("design_last",             cmd_design_last))
    app.add_handler(_CH("design_qc",               cmd_design_qc))
    app.add_handler(_CH("design_logs",             cmd_design_logs))
    app.add_handler(_CH("design_apply",            cmd_design_apply))
    app.add_handler(_CH("design_approve",          cmd_design_approve))
    app.add_handler(_CH("design_reject",           cmd_design_reject))
    app.add_handler(_CH("design_fix_prompt",       cmd_design_fix_prompt))
    app.add_handler(_CH("design_autofix_on",       cmd_design_autofix_on))
    app.add_handler(_CH("design_autofix_off",      cmd_design_autofix_off))
    app.add_handler(_CH("design_live_qc",          cmd_design_live_qc))
    app.add_handler(_CH("design_compare",          cmd_design_compare))
    app.add_handler(_CH("design_restore_last_good", cmd_design_restore_last_good))
    app.add_handler(_CH("design_debug",            cmd_design_debug))
    app.add_handler(_CH("design_score",            cmd_design_score))
    # New screenshot + comparison handlers
    app.add_handler(_CH("design_fix_from_screenshot", cmd_design_fix_from_screenshot))
    app.add_handler(_CH("design_restore_best",     cmd_design_restore_best))
    app.add_handler(_CH("design_learn_style",      cmd_design_learn_style))
    app.add_handler(_CH("design_focus_area",       cmd_design_focus_area))
    # Photo handler — admin private chat only, for screenshot feedback
    app.add_handler(_MH(_F.PHOTO & _F.ChatType.PRIVATE, handle_design_photo))
    log.info("Design iteration handlers registered (21 commands + photo handler).")


# ── Auto-task (standalone / post_init) ────────────────────────────────────────

_FIRST_TASK_INSTRUCTION = (
    "Restore the original date box on all posters and write today's "
    "Shamsi and Gregorian date correctly inside the box. "
    "Keep logo and price boxes unchanged. Generate preview only."
)

TARGET_CHAT = f"@{ALLOWED_CHAT_USERNAME}"


async def run_auto_task(chat_id=None):
    global _current_task, _fix_hints
    _setup_dirs()

    chat = chat_id or TARGET_CHAT

    async with _task_lock:
        if _current_task:
            log.info("Auto-task skipped: another task already running")
            return
        task_id = _task_id_now()
        _current_task = {"id": task_id, "instruction": _FIRST_TASK_INSTRUCTION}
        _fix_hints = []

    save_task(task_id, _FIRST_TASK_INSTRUCTION)
    log.info(f"Auto-task starting: {task_id} → ADMIN (private)")

    # Startup notification goes to admin private chat only.
    _api_send(
        ADMIN_DEBUG_CHAT,
        f"🚀 Design Iteration Agent آماده است.\n"
        f"اجرای وظیفه اول به صورت خودکار:\n\n"
        f"📋 {_FIRST_TASK_INSTRUCTION}\n\n"
        "⏳ در حال تولید پیش‌نمایش...",
    )

    try:
        await execute_task(task_id, _FIRST_TASK_INSTRUCTION, ADMIN_DEBUG_CHAT, bot=None)
    except Exception as exc:
        log.error(f"Auto-task error: {exc}")
        traceback.print_exc()


async def design_post_init(app) -> None:
    asyncio.create_task(_delayed_auto_task(app))


async def _delayed_auto_task(app) -> None:
    await asyncio.sleep(10)
    try:
        await run_auto_task(chat_id=ADMIN_ID)
    except Exception as exc:
        log.error(f"Delayed auto-task error: {exc}")


# ── Standalone worker ─────────────────────────────────────────────────────────

async def _standalone_worker():
    log.info("Design Iteration Agent worker starting (standalone, no polling).")
    log.info(f"All output → ADMIN private ({ADMIN_DEBUG_CHAT}). Public channel: {PUBLIC_CHANNEL}")
    await run_auto_task(chat_id=ADMIN_DEBUG_CHAT)
    log.info("Auto-task complete. Worker idle (heartbeat every 5 min).")
    while True:
        await asyncio.sleep(300)
        log.debug("Design agent worker heartbeat")


def main():
    _setup_dirs()
    log.info("=== Design Iteration Agent (standalone worker) ===")
    asyncio.run(_standalone_worker())


if __name__ == "__main__":
    main()
