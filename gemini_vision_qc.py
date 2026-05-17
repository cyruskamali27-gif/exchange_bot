#!/usr/bin/env python3
"""
gemini_vision_qc.py
Gemini Vision QC layer — validates rendered posters before publishing.

Checks:
  - Prices match latest_rates.json formula
  - Prices are inside correct boxes (no floating)
  - Persian text is intact
  - No sticker/patch background effect
  - No AI-invented numbers
  - Date is correct
  - Overall professional quality

Public API
----------
run_qc(poster_type, rendered_path) -> dict
    Returns QC report. report["passed"] True/False.
    Saves JSON report to vision_qc_reports/.

summarize_report(report) -> str
    Human-readable one-paragraph summary.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv("/var/www/exchange_bot/.env")

from google import genai
from google.genai import types as genai_types
from PIL import Image

log = logging.getLogger("gemini_vision_qc")

QC_DIR     = Path("/var/www/exchange_bot/vision_qc_reports")
TORONTO_TZ = ZoneInfo("America/Toronto")
RATES_FILE = Path("/var/www/exchange_bot/latest_rates.json")

CURRENCY_MAP = {
    "canada": "CAD",
    "usa":    "USD",
    "usacan": "USACAN",
    "usdt":   "USDT",
    "eur":    "EUR",
    "logo":   None,
}


def _get_client():
    import os
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set in environment")
    return genai.Client(api_key=key)


def _load_rates() -> dict:
    try:
        return json.loads(RATES_FILE.read_text(encoding="utf-8")).get("rates", {})
    except Exception:
        return {}


def _build_prompt(poster_type: str, expected: dict) -> str:
    today_str = datetime.now(TORONTO_TZ).strftime("%-d %B %Y")

    if poster_type == "logo":
        return (
            "You are a financial poster QC inspector for Cyrus Global Exchange.\n"
            "This is a LOGO/DATE poster (no prices).\n\n"
            "CHECK:\n"
            "1. logo_ok          — Cyrus Global Exchange logo is fully visible and NOT cropped at any edge\n"
            "2. date_boxes_ok    — Date boxes are visible inside the logo panel area\n"
            "3. date_inside_logo — Date text appears ONLY inside the logo/date panel, NOT floating elsewhere\n"
            "4. no_black_date_box — No separate black or dark rectangle box exists around the date\n"
            "5. professional     — Poster looks professional and design is not damaged\n\n"
            f"Today's date for reference: {today_str}\n\n"
            'Respond ONLY in JSON:\n'
            '{"passed": true/false, "logo_ok": true/false, "date_boxes_ok": true/false, '
            '"date_inside_logo": true/false, "no_black_date_box": true/false, '
            '"professional": true/false, "issues": [], "score": 0-100, '
            '"recommendation": "APPROVE or REJECT", "notes": "brief"}'
        )

    sell_str = expected.get("sell_str", "N/A")
    buy_str  = expected.get("buy_str",  "N/A")

    return f"""You are a financial poster QC inspector for Cyrus Global Exchange.

Analyze this exchange rate poster carefully.

POSTER TYPE: {poster_type.upper()}
EXPECTED VALUES (calculated by our Python system from live rates):
  Sell price: {sell_str}
  Buy price:  {buy_str}
  Today's date (Gregorian): {today_str}

POSTER LAYOUT RULES YOU MUST ENFORCE:
- The poster has a HEADER/LOGO BAND at the top (~top 30% of the image). Date text belongs ONLY there.
- The MAIN ARTWORK area below the header contains price boxes with exchange rate numbers.
- NO date box, sticker, rectangle, or overlay should appear over the main artwork.
- Price numbers should appear clean inside their designated boxes with NO colored background patch behind them.

INSPECT each of the following and answer true/false:

REGULAR CHECKS:
1. sell_price_ok      — Sell price number is visible and matches "{sell_str}"
2. buy_price_ok       — Buy price number is visible and matches "{buy_str}"
3. prices_in_boxes    — Both prices appear inside proper rectangular price boxes
4. no_floating        — No price text is floating outside a box
5. date_ok            — A date is visible and appears to be today or recent
6. persian_ok         — Persian/Arabic text is visible and not broken/garbled
7. professional       — Poster looks professional and design is not damaged
9. no_invented_nums   — No unexpected numbers other than the expected prices

HARD FAIL CHECKS (false = automatic REJECT regardless of score):
8.  no_sticker        — No patch, rectangle, or sticker-effect background behind price numbers
10. no_black_date_box — No separate black or dark-colored rectangle/box exists in the date area
11. date_inside_logo  — Date text is ONLY in the header/logo band (top area), NOT over price boxes or main artwork
12. no_overlapping_text — No text elements overlap each other (prices, labels, dates all separate)
13. logo_not_cropped  — The Cyrus Global Exchange logo/branding is fully visible and NOT cut off at any edge
14. branding_ok       — Cyrus Global Exchange name or logo is visible

Respond ONLY in this exact JSON format (no extra text):
{{
  "passed": true or false,
  "sell_price_ok": true or false,
  "buy_price_ok": true or false,
  "prices_in_boxes": true or false,
  "no_floating": true or false,
  "date_ok": true or false,
  "persian_ok": true or false,
  "professional": true or false,
  "no_sticker": true or false,
  "no_invented_nums": true or false,
  "no_black_date_box": true or false,
  "date_inside_logo": true or false,
  "no_overlapping_text": true or false,
  "logo_not_cropped": true or false,
  "branding_ok": true or false,
  "issues": ["list any specific issues found, empty array if none"],
  "score": 0,
  "recommendation": "APPROVE or REJECT",
  "notes": "one sentence summary"
}}"""


def run_qc(poster_type: str, rendered_path: Path) -> dict:
    """
    Run Gemini Vision QC on a rendered poster image.

    Parameters
    ----------
    poster_type   : "canada" | "usa" | "usacan" | "usdt" | "eur" | "logo"
    rendered_path : path to the rendered PNG

    Returns
    -------
    dict with QC results. report["passed"] is the key field.
    """
    QC_DIR.mkdir(parents=True, exist_ok=True)

    rendered_path = Path(rendered_path)
    if not rendered_path.exists():
        raise FileNotFoundError(f"Rendered poster not found: {rendered_path}")

    key     = poster_type.lower()
    cur_key = CURRENCY_MAP.get(key, key.upper())

    # Build expected values for comparison
    expected = {}
    if cur_key:
        try:
            from python_price_renderer import get_expected_prices
            expected = get_expected_prices(key)
        except Exception as exc:
            log.warning(f"[qc] Could not load expected prices: {exc}")

    prompt = _build_prompt(key, expected)
    img    = Image.open(rendered_path)

    log.info(f"[qc] Running Gemini Vision on {rendered_path.name}")
    client   = _get_client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt, img],
    )

    raw = response.text.strip()
    # Strip markdown fences if present
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break

    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"[qc] JSON parse failed, raw response: {raw[:200]}")
        report = {
            "passed":         False,
            "score":          0,
            "recommendation": "REJECT",
            "issues":         ["QC response could not be parsed"],
            "raw_response":   response.text[:500],
        }

    # Compute score from individual checks (Gemini's "score" field is unreliable)
    check_keys = [
        "sell_price_ok", "buy_price_ok", "prices_in_boxes", "no_floating",
        "date_ok", "persian_ok", "professional", "no_sticker",
        "no_invented_nums", "branding_ok", "no_black_date_box",
        "date_inside_logo", "no_overlapping_text", "logo_not_cropped",
    ]
    checks_present = [k for k in check_keys if k in report]
    if checks_present:
        passed_count = sum(1 for k in checks_present if report.get(k))
        report["score"] = round(passed_count / len(checks_present) * 100)

    # Derive "passed" from checks + recommendation
    if "passed" not in report:
        report["passed"] = (
            report.get("score", 0) >= 75
            and report.get("recommendation", "REJECT") == "APPROVE"
        )

    # Hard fail conditions — instant REJECT regardless of overall score
    HARD_FAIL_KEYS = ["no_black_date_box", "date_inside_logo", "no_sticker", "logo_not_cropped"]
    hard_failed = [k for k in HARD_FAIL_KEYS if k in report and not report.get(k)]
    if hard_failed:
        report["passed"] = False
        report["recommendation"] = "REJECT"
        if "issues" not in report:
            report["issues"] = []
        for k in hard_failed:
            report["issues"].insert(0, f"HARD FAIL: {k}")

    # Attach metadata
    report["poster_type"]   = key
    report["rendered_path"] = str(rendered_path)
    report["expected"]      = expected
    report["qc_timestamp"]  = datetime.now(TORONTO_TZ).isoformat()

    # Save report
    ts          = datetime.now(TORONTO_TZ).strftime("%Y%m%d_%H%M%S")
    report_path = QC_DIR / f"qc_{key}_{ts}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    status = "✅ PASSED" if report.get("passed") else "❌ FAILED"
    log.info(
        f"[qc] {status}  score={report.get('score')}  "
        f"rec={report.get('recommendation')}  report={report_path.name}"
    )
    if report.get("issues"):
        for issue in report["issues"]:
            log.warning(f"[qc]   issue: {issue}")

    return report


def summarize_report(report: dict) -> str:
    """Return a concise human-readable summary of a QC report."""
    passed = report.get("passed", False)
    score  = report.get("score", 0)
    rec    = report.get("recommendation", "REJECT")
    notes  = report.get("notes", "")
    issues = report.get("issues", [])
    pt     = report.get("poster_type", "?").upper()

    header = f"{'✅ QC PASSED' if passed else '❌ QC FAILED'}  [{pt}]  score={score}/100  → {rec}"
    body   = f"  {notes}" if notes else ""
    if issues:
        body += "\n  Issues:\n" + "\n".join(f"    • {i}" for i in issues)

    exp = report.get("expected", {})
    if exp:
        body += f"\n  Expected: sell={exp.get('sell_str')} buy={exp.get('buy_str')}"

    return f"{header}\n{body}".strip()
