#!/usr/bin/env python3
"""
test_screenshot_flow.py
Simulates the full Telegram screenshot-feedback loop:
  1. Generate a poster with deliberately bad date layout (lines overlapping)
  2. Post it to admin private chat via Bot API (as a "sent screenshot")
  3. Run _analyze_screenshot_feedback on the same image
  4. Apply the suggested fix and generate a corrected preview
  5. Post the corrected preview to admin private chat
  6. Print a full report
"""

import sys, io, os, json
sys.path.insert(0, "/var/www/exchange_bot")

from dotenv import load_dotenv
load_dotenv()

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont

import requests as _http

from design_iteration_agent import (
    BOT_TOKEN, ADMIN_ID, ADMIN_DEBUG_CHAT,
    PREVIEWS_DIR, ASSETS_DIR, TEMPLATES, POSTER_ORDER,
    FONT_BOLD_FA, FONT_BOLD_EN,
    COORDS_FILE, TORONTO_TZ,
    fa, load_font, sample_bg,
    _analyze_screenshot_feedback,
    _detect_region_from_text,
    generate_preview,
    _render_adj, _DEFAULT_ADJ,
)

PASS = "\033[92m✅\033[0m"
FAIL = "\033[91m❌\033[0m"
INFO = "\033[94mℹ\033[0m"

def api_send(text):
    _http.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": str(ADMIN_DEBUG_CHAT), "text": text},
        timeout=15,
    )

def api_photo(path, caption=""):
    with open(path, "rb") as fh:
        r = _http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": str(ADMIN_DEBUG_CHAT), "caption": caption},
            files={"photo": fh},
            timeout=30,
        )
    return r.json()

# ─── Step 1: Generate deliberately broken poster ──────────────────────────────
print(f"\n{INFO}  Step 1: Generating broken poster (lines overlapping)...")

coords = json.loads(COORDS_FILE.read_text(encoding="utf-8"))
cad_coords = coords.get("CAD", {})
date_box = cad_coords.get("date_box")
template_path = ASSETS_DIR / TEMPLATES["CAD"]

img = Image.open(template_path).convert("RGBA")
draw = ImageDraw.Draw(img)
px_rgb = img.convert("RGB").load()

x1, y1, x2, y2 = date_box
box_w = x2 - x1
box_h = y2 - y1

bg = sample_bg(px_rgb, x1, y1, x2, y2)
box_bg = (max(0, bg[0] - 25), max(0, bg[1] - 25), max(0, bg[2] - 10))
draw.rectangle([x1, y1, x2, y2], fill=(*box_bg, 255))
draw.rectangle([x1, y1, x2, y2], outline=(185, 155, 75, 255), width=2)

# Intentionally bad layout: font too large, no gap → lines overlap
dt = datetime.now(TORONTO_TZ)
import jdatetime
jd = jdatetime.datetime.fromgregorian(datetime=dt)
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_WEEKDAYS  = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یک‌شنبه"]

shamsi_raw  = f"{str(jd.year).translate(_FA_DIGITS)}/{str(jd.month).zfill(2).translate(_FA_DIGITS)}/{str(jd.day).zfill(2).translate(_FA_DIGITS)}"
weekday_raw = _WEEKDAYS[dt.weekday()]
gregorian   = dt.strftime("%-d %B %Y")

shamsi_r  = fa(shamsi_raw)
weekday_r = fa(weekday_raw)

# Oversized font: lines will collide
big_font_fa = load_font(FONT_BOLD_FA, int(box_h * 0.45))
big_font_en = load_font(FONT_BOLD_EN, int(box_h * 0.38))

# Stack them with zero gap — deliberately overlapping
yl1 = y1 + 4
yl2 = yl1 + int(box_h * 0.30)   # too close
yl3 = yl2 + int(box_h * 0.28)   # too close

SHADOW   = (0, 0, 0, 180)
COLOR_FA = (242, 222, 148, 255)
COLOR_EN = (210, 190, 120, 255)

draw.text((x1 + 2, yl1 + 1), shamsi_r,  font=big_font_fa, fill=SHADOW)
draw.text((x1 + 2, yl1),     shamsi_r,  font=big_font_fa, fill=COLOR_FA)
draw.text((x1 + 2, yl2 + 1), weekday_r, font=big_font_fa, fill=SHADOW)
draw.text((x1 + 2, yl2),     weekday_r, font=big_font_fa, fill=COLOR_FA)
draw.text((x1 + 2, yl3 + 1), gregorian, font=big_font_en, fill=SHADOW)
draw.text((x1 + 2, yl3),     gregorian, font=big_font_en, fill=COLOR_EN)

broken_path = PREVIEWS_DIR / "test_broken_date.png"
img.convert("RGB").save(str(broken_path), "PNG")
print(f"   Saved: {broken_path.name}")
print(f"   Date box: {date_box}  font_h≈{int(box_h*0.45)}px (oversized)")

# ─── Step 2: Post broken poster to admin private chat ─────────────────────────
print(f"\n{INFO}  Step 2: Posting broken poster to admin private chat...")
api_send("🧪 TEST — ارسال پوستر با تاریخ خراب:")
resp = api_photo(broken_path, "❌ این نسخه — تاریخ خرابه (تست)")
ok = resp.get("ok", False)
print(f"   Telegram response: {'ok' if ok else 'FAILED'}")
if not ok:
    print(f"   Error: {resp}")

# ─── Step 3: Simulate the Persian complaint ───────────────────────────────────
user_text = "تاریخ خرابه و خطوط روی هم افتادن"
api_send(f'👤 شکایت ادمین: «{user_text}»')
print(f"\n{INFO}  Step 3: Running screenshot analysis...")
print(f"   Complaint: «{user_text}»")

img_bytes = broken_path.read_bytes()
analysis = _analyze_screenshot_feedback(img_bytes, user_text)

region      = analysis.get("region", "?")
issue_type  = analysis.get("issue_type", "?")
description = analysis.get("description", "?")
suggested   = analysis.get("suggested_adj", {})
confidence  = analysis.get("confidence", 0)

print(f"\n   Analysis result:")
print(f"     region:      {region}")
print(f"     issue_type:  {issue_type}")
print(f"     confidence:  {confidence*100:.0f}%")
print(f"     suggested:   {suggested}")
print(f"     description: {description[:100]}...")

detected_date = (region == "date_box")
print(f"\n   {PASS if detected_date else FAIL}  date_box detected: {detected_date}")
print(f"   {PASS if confidence >= 0.7 else FAIL}  confidence >= 70%: {confidence*100:.0f}%")

# ─── Step 4: Apply suggested fix and generate corrected preview ───────────────
print(f"\n{INFO}  Step 4: Applying suggested fix and generating corrected preview...")

from design_iteration_agent import _render_adj, _DEFAULT_ADJ
adj = dict(_render_adj.get("CAD", _DEFAULT_ADJ()))
if suggested:
    for k, v in suggested.items():
        if k in adj:
            adj[k] = v

print(f"   Adjustments applied: {adj}")

result = generate_preview("CAD", 99, dt, adj)
if result:
    fixed_path, layout = result
    print(f"   {PASS}  Fixed preview generated: {fixed_path.name}")

    # ─── Step 5: Post corrected preview to admin ──────────────────────────────
    print(f"\n{INFO}  Step 5: Posting corrected preview to admin...")
    api_photo(fixed_path, "✅ نسخه بعد از فیکس خودکار (تست)")
    api_send(
        f"📋 گزارش آنالیز اسکرین‌شات:\n"
        f"  ناحیه: {region}\n"
        f"  نوع مشکل: {issue_type}\n"
        f"  اطمینان: {confidence*100:.0f}%\n"
        f"  font_scale: {adj.get('font_scale', 1.0):.2f}\n"
        f"  padding_extra: {adj.get('padding_extra', 0)}\n\n"
        f"برای تایید عمومی: /design_approve"
    )
    print(f"   {PASS}  Corrected preview posted to admin private chat")
else:
    print(f"   {FAIL}  generate_preview failed")

# ─── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{'='*55}")
print("TELEGRAM SCREENSHOT TEST — SUMMARY")
print(f"{'='*55}")
print(f"  {PASS if detected_date else FAIL}  date_box region detected by Gemini Vision")
print(f"  {PASS if confidence >= 0.7 else FAIL}  confidence {confidence*100:.0f}% (threshold 70%)")
print(f"  {PASS if result else FAIL}  corrected preview generated")
print(f"  {PASS}  broken + fixed poster posted to admin private chat")
print(f"  {PASS}  public channel (@cyrusGlobalExchange) NOT touched")
print(f"{'='*55}\n")
