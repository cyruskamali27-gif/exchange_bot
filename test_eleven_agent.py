#!/usr/bin/env python3
"""
test_eleven_agent.py
End-to-end test: screenshot feedback detection pipeline.
Simulates admin sending a poster screenshot with a Persian date complaint.
"""

import io, json, sys, os
sys.path.insert(0, "/var/www/exchange_bot")

from pathlib import Path
from PIL import Image, ImageDraw

# ── Load env ──────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── Import the functions under test ──────────────────────────────────────────
from design_iteration_agent import (
    _detect_region_from_text,
    _analyze_screenshot_feedback,
    _append_vfm, _load_vfm,
    REGION_KEYWORDS,
    PREVIEWS_DIR,
)

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
results = []

def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    print(f"  {status}  {label}")
    if detail:
        print(f"          → {detail}")
    results.append(condition)

print("\n" + "="*60)
print("SCREENSHOT FEEDBACK DETECTION — TEST SUITE")
print("="*60)

# ─── TEST 1: Text-only region detection ──────────────────────────────────────
print("\n[1] Text-only region detection (Persian keywords)\n")

complaints = [
    ("تاریخ خرابه",       "date_box"),
    ("عدد چسبیده",         "price_box"),
    ("لوگو خراب شده",     "logo"),
    ("فاصله کمه",          "spacing"),
    ("تراز نیست",          "alignment"),
    ("پایین مشکل داره",   "footer"),
    ("date is broken",    "date_box"),
]

for text, expected in complaints:
    detected = _detect_region_from_text(text)
    check(
        f'"{text}" -> {detected}',
        detected == expected,
        f"expected={expected}"
    )

# ─── TEST 2: Gemini Vision screenshot analysis ────────────────────────────────
print("\n[2] Gemini Vision screenshot analysis\n")

# Load a real poster preview as the test "screenshot"
previews = sorted(PREVIEWS_DIR.glob("iter*_cad_*.png"))
if not previews:
    print("  WARNING: No preview images found — skipping Gemini Vision test")
else:
    test_img_path = previews[-1]
    print(f"  Using image: {test_img_path.name}")
    img_bytes = test_img_path.read_bytes()

    # Simulate admin complaint about date
    user_text = "تاریخ خرابه و خطوط روی هم افتادن"
    print(f"  Complaint: <<{user_text}>>")

    analysis = _analyze_screenshot_feedback(img_bytes, user_text)
    print(f"\n  Raw analysis result:")
    print(f"    region:      {analysis.get('region')}")
    print(f"    issue_type:  {analysis.get('issue_type')}")
    print(f"    description: {analysis.get('description')}")
    print(f"    suggested:   {analysis.get('suggested_adj')}")
    print(f"    confidence:  {analysis.get('confidence', 0)*100:.0f}%")

    check(
        "Region detected as 'date_box'",
        analysis.get("region") == "date_box",
        f"got: {analysis.get('region')}"
    )
    check(
        "Confidence >= 50%",
        analysis.get("confidence", 0) >= 0.5,
        f"got: {analysis.get('confidence', 0)*100:.0f}%"
    )
    check(
        "suggested_adj is a dict",
        isinstance(analysis.get("suggested_adj"), dict),
        f"got: {type(analysis.get('suggested_adj'))}"
    )
    check(
        "Issue type is present",
        bool(analysis.get("issue_type")),
        f"got: {analysis.get('issue_type')}"
    )

# ─── TEST 3: Second complaint — spacing ──────────────────────────────────────
print("\n[3] Second complaint — spacing / spacing keywords\n")

if previews:
    img_bytes2 = test_img_path.read_bytes()
    text2 = "عدد قیمت خیلی چسبیده به هم"
    print(f"  Complaint: <<{text2}>>")

    analysis2 = _analyze_screenshot_feedback(img_bytes2, text2)
    print(f"  Detected region: {analysis2.get('region')}")
    print(f"  Issue type:      {analysis2.get('issue_type')}")

    check(
        "A valid region is returned",
        analysis2.get("region") in REGION_KEYWORDS,
        f"got: {analysis2.get('region')}"
    )
    check(
        "Description is non-empty",
        bool(analysis2.get("description")),
        f"got: {repr(analysis2.get('description'))}"
    )

# ─── TEST 4: visual_feedback_memory.json write / read ────────────────────────
print("\n[4] Visual feedback memory persistence\n")

from datetime import datetime
from zoneinfo import ZoneInfo
TORONTO_TZ = ZoneInfo("America/Toronto")

test_entry = {
    "id": "test_vfm_001",
    "timestamp": datetime.now(TORONTO_TZ).isoformat(),
    "user_text": "تاریخ خرابه",
    "screenshot_path": "design_iterations/screenshots/test.jpg",
    "detected_region": "date_box",
    "issue_type": "overlap",
    "description": "Lines 1 and 2 are overlapping",
    "adjustments_before": {"CAD": {"y_offset": 0, "font_scale": 1.0, "padding_extra": 0}},
    "adjustments_after":  {"CAD": {"y_offset": 0, "font_scale": 0.88, "padding_extra": 4}},
    "score_before": 65.0,
    "score_after": 97.0,
    "fix_successful": True,
    "preferred": False,
}

_append_vfm(test_entry)
loaded = _load_vfm()
found = any(e.get("id") == "test_vfm_001" for e in loaded)
check("VFM entry written and read back", found, f"entries in file: {len(loaded)}")

last = [e for e in loaded if e.get("id") == "test_vfm_001"][-1]
check("Detected region persisted correctly",  last.get("detected_region") == "date_box")
check("score_after persisted correctly",      last.get("score_after") == 97.0)
check("fix_successful persisted correctly",   last.get("fix_successful") == True)

# ─── TEST 5: ADMIN_DEBUG_CHAT routing constant ───────────────────────────────
print("\n[5] Routing constants\n")

from design_iteration_agent import ADMIN_DEBUG_CHAT, PUBLIC_CHANNEL, ADMIN_ID
check(
    "ADMIN_DEBUG_CHAT == ADMIN_ID (private, not public channel)",
    ADMIN_DEBUG_CHAT == ADMIN_ID,
    f"ADMIN_DEBUG_CHAT={ADMIN_DEBUG_CHAT}"
)
check(
    "PUBLIC_CHANNEL is @cyrusGlobalExchange",
    PUBLIC_CHANNEL == "@cyrusGlobalExchange",
    f"got: {PUBLIC_CHANNEL}"
)
check(
    "ADMIN_DEBUG_CHAT is an int (not a string channel name)",
    isinstance(ADMIN_DEBUG_CHAT, int),
    f"type: {type(ADMIN_DEBUG_CHAT)}"
)

# ─── SUMMARY ─────────────────────────────────────────────────────────────────
print("\n" + "="*60)
passed = sum(results)
total  = len(results)
print(f"RESULT: {passed}/{total} tests passed")
if passed == total:
    print("\033[92mAll tests PASSED\033[0m")
else:
    print(f"\033[91m{total - passed} test(s) FAILED\033[0m")
print("="*60 + "\n")

sys.exit(0 if passed == total else 1)
