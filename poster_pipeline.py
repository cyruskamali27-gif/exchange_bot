#!/usr/bin/env python3
"""
poster_pipeline.py
Full AI poster pipeline: GPT Image → Python Prices → Gemini Vision QC → Telegram Publish

Architecture:
  1. ai_image_designer   — GPT Image generates poster design (empty boxes)
  2. python_price_renderer — Python writes exact rates from latest_rates.json
  3. gemini_vision_qc    — Gemini Vision validates poster before publishing
  4. Telegram publish    — Admin private channel for previews, public only after /publish_approved

Telegram Commands (admin only):
  /poster_pipeline <type> <instruction>   Full pipeline → private preview
  /ai_design_poster <type> <instruction>  Design only
  /render_prices <type>                   Render prices onto latest design
  /vision_qc <type>                       QC latest rendered poster
  /publish_approved                       Publish all QC-approved posters publicly
  /pipeline_status                        Show pipeline state

PM2 process: poster-pipeline-agent
"""

import asyncio
import io
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)

from config import BOT_TOKEN, ADMIN_ID
from ai_image_designer import (
    generate_poster_design, check_api_key, latest_design, list_designs,
)
from python_price_renderer import render_prices, get_expected_prices, COORDS
from gemini_vision_qc import run_qc, summarize_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("poster_pipeline")

TORONTO_TZ      = ZoneInfo("America/Toronto")
APPROVED_DIR    = Path("/var/www/exchange_bot/approved_posters")
PREVIEWS_DIR    = Path("/var/www/exchange_bot/private_previews")
QC_DIR          = Path("/var/www/exchange_bot/vision_qc_reports")
PUBLIC_CHANNEL  = "@cyrusGlobalExchange"

VALID_TYPES = list(COORDS.keys())  # canada, usa, usacan, usdt, eur

# Publish order for full cycle
PUBLISH_ORDER = ["canada", "usa", "usacan", "usdt", "eur"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def _parse_poster_type(args: list[str]) -> tuple[str, str]:
    """Extract poster_type and instruction from command args."""
    if not args:
        return "", ""
    poster_type = args[0].lower()
    instruction = " ".join(args[1:])
    return poster_type, instruction


async def _send_image(bot: Bot, chat_id: int, path: Path, caption: str = ""):
    """Send image file to a chat."""
    with open(path, "rb") as f:
        await bot.send_photo(chat_id=chat_id, photo=f, caption=caption)


async def _reply_text(update: Update, text: str):
    await update.message.reply_text(text, parse_mode=None)


def _normalize_type(raw: str) -> str:
    """Accept 'usa', 'USD', 'us', 'u' etc and normalize to key."""
    aliases = {
        "us": "usa", "usd": "usa", "dollar": "usa",
        "can": "canada", "cad": "canada", "ca": "canada",
        "usacan": "usacan", "uscan": "usacan", "usdcad": "usacan",
        "tether": "usdt", "trc20": "usdt",
        "euro": "eur", "eu": "eur",
        "logo": "logo",
    }
    return aliases.get(raw.lower(), raw.lower())


# ── Command: /poster_pipeline ─────────────────────────────────────────────────

async def cmd_poster_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full pipeline: AI design → Python prices → Gemini QC → private preview."""
    if not _is_admin(update.effective_user.id):
        return

    args = context.args or []
    raw_type, instruction = _parse_poster_type(args)
    poster_type = _normalize_type(raw_type) if raw_type else ""

    if poster_type not in VALID_TYPES:
        await _reply_text(update,
            f"Usage: /poster_pipeline <type> [instruction]\n"
            f"Valid types: {', '.join(VALID_TYPES)}"
        )
        return

    await _reply_text(update, f"🚀 Starting full pipeline for: {poster_type.upper()}\n"
                               f"Instruction: {instruction or '(default)'}")

    chat_id = update.effective_chat.id

    # ── Step 1: AI Design ─────────────────────────────────────────────────────
    ok, key_msg = check_api_key()
    if ok:
        await _reply_text(update, "🎨 Step 1/3: GPT Image generating design…")
        try:
            design_path = generate_poster_design(poster_type, instruction)
            await _reply_text(update, f"✅ Design created: {design_path.name}")
            await _send_image(context.bot, chat_id, design_path,
                              caption=f"🎨 AI Design — {poster_type.upper()} (EMPTY boxes)")
        except Exception as exc:
            await _reply_text(update, f"⚠️ GPT Image failed: {exc}\n"
                                       f"Falling back to existing template.")
            design_path = None
    else:
        await _reply_text(update, f"⚠️ {key_msg}\nStep 1 skipped — using existing template.")
        design_path = None

    # ── Step 2: Python Prices ─────────────────────────────────────────────────
    await _reply_text(update, "🔢 Step 2/3: Python writing exact prices…")
    try:
        toronto_now  = datetime.now(TORONTO_TZ)
        rendered_path = render_prices(poster_type, design_path, toronto_now)
        prices = get_expected_prices(poster_type)
        await _reply_text(update,
            f"✅ Prices rendered:\n"
            f"  Sell: {prices['sell_str']}\n"
            f"  Buy:  {prices['buy_str']}"
        )
        # Save private preview
        PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        preview_path = PREVIEWS_DIR / f"{poster_type}_preview_latest.png"
        shutil.copy(rendered_path, preview_path)
        await _send_image(context.bot, chat_id, rendered_path,
                          caption=f"🔢 Prices Rendered — {poster_type.upper()}\n"
                                  f"sell={prices['sell_str']} buy={prices['buy_str']}")
    except Exception as exc:
        await _reply_text(update, f"❌ Price rendering failed: {exc}")
        log.error(f"[pipeline] render_prices failed: {exc}", exc_info=True)
        return

    # ── Step 3: Gemini Vision QC ──────────────────────────────────────────────
    await _reply_text(update, "🔍 Step 3/3: Gemini Vision QC checking poster…")
    try:
        report = run_qc(poster_type, rendered_path)
        summary = summarize_report(report)
        await _reply_text(update, f"QC Report:\n{summary}")

        if report.get("passed"):
            # Copy to approved_posters
            APPROVED_DIR.mkdir(parents=True, exist_ok=True)
            approved_path = APPROVED_DIR / f"{poster_type}_approved.png"
            shutil.copy(rendered_path, approved_path)
            await _reply_text(update,
                f"✅ QC PASSED — poster saved to approved_posters/\n"
                f"Use /publish_approved to post to {PUBLIC_CHANNEL}"
            )
        else:
            await _reply_text(update,
                f"❌ QC FAILED — poster NOT approved for public publishing.\n"
                f"Issues: {report.get('issues', [])}"
            )
    except Exception as exc:
        await _reply_text(update, f"⚠️ Gemini QC error: {exc}\nPoster NOT approved.")
        log.error(f"[pipeline] qc failed: {exc}", exc_info=True)


# ── Command: /ai_design_poster ────────────────────────────────────────────────

async def cmd_ai_design_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate AI design only — no prices, no publishing."""
    if not _is_admin(update.effective_user.id):
        return

    args = context.args or []
    raw_type, instruction = _parse_poster_type(args)
    poster_type = _normalize_type(raw_type) if raw_type else ""

    if not poster_type:
        await _reply_text(update,
            f"Usage: /ai_design_poster <type> [instruction]\n"
            f"Valid types: logo, {', '.join(VALID_TYPES)}"
        )
        return

    ok, key_msg = check_api_key()
    if not ok:
        await _reply_text(update, f"❌ Cannot run: {key_msg}")
        return

    await _reply_text(update, f"🎨 Generating AI design for {poster_type.upper()}…")
    try:
        path = generate_poster_design(poster_type, instruction)
        await _send_image(update.effective_chat.id
                          if hasattr(update.effective_chat, 'id') else update.message.chat_id,
                          update.message.chat_id,  # just use reply
                          path, f"🎨 AI Design: {poster_type.upper()} (empty boxes)")
        # Actually send properly:
        with open(path, "rb") as f:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=f,
                caption=f"🎨 AI Design: {poster_type.upper()}\nEmpty price boxes — Python writes numbers next.",
            )
        await _reply_text(update, f"✅ Saved: {path.name}\nUse /render_prices {poster_type} to add prices.")
    except Exception as exc:
        await _reply_text(update, f"❌ Design failed: {exc}")
        log.error(f"[pipeline] ai_design failed: {exc}", exc_info=True)


# ── Command: /render_prices ───────────────────────────────────────────────────

async def cmd_render_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Render Python prices onto the latest design for a poster type."""
    if not _is_admin(update.effective_user.id):
        return

    args = context.args or []
    raw_type = args[0] if args else ""
    poster_type = _normalize_type(raw_type) if raw_type else ""

    if poster_type not in VALID_TYPES:
        await _reply_text(update,
            f"Usage: /render_prices <type>\nValid: {', '.join(VALID_TYPES)}"
        )
        return

    design_path = latest_design(poster_type)
    if design_path:
        await _reply_text(update, f"🔢 Rendering prices onto: {design_path.name}")
    else:
        await _reply_text(update, "🔢 No AI design found — using existing template.")

    try:
        toronto_now   = datetime.now(TORONTO_TZ)
        rendered_path = render_prices(poster_type, design_path, toronto_now)
        prices        = get_expected_prices(poster_type)
        with open(rendered_path, "rb") as f:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=f,
                caption=(
                    f"🔢 Prices Rendered: {poster_type.upper()}\n"
                    f"Sell: {prices['sell_str']}\n"
                    f"Buy:  {prices['buy_str']}\n"
                    f"File: {rendered_path.name}"
                ),
            )
    except Exception as exc:
        await _reply_text(update, f"❌ Render failed: {exc}")
        log.error(f"[pipeline] render failed: {exc}", exc_info=True)


# ── Command: /vision_qc ───────────────────────────────────────────────────────

async def cmd_vision_qc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run Gemini Vision QC on the latest rendered poster."""
    if not _is_admin(update.effective_user.id):
        return

    args = context.args or []
    raw_type = args[0] if args else ""
    poster_type = _normalize_type(raw_type) if raw_type else ""

    if poster_type not in VALID_TYPES:
        await _reply_text(update,
            f"Usage: /vision_qc <type>\nValid: {', '.join(VALID_TYPES)}"
        )
        return

    # Find latest rendered file
    from python_price_renderer import RENDERED_DIR
    rendered_files = sorted(
        (RENDERED_DIR / "").glob(f"{poster_type}_*.png")
        if (RENDERED_DIR / "").exists() else [],
        reverse=True
    )
    # Use RENDERED_DIR directly
    rendered_files = sorted(RENDERED_DIR.glob(f"{poster_type}_*.png"), reverse=True) \
        if RENDERED_DIR.exists() else []

    if not rendered_files:
        await _reply_text(update,
            f"No rendered poster found for {poster_type}. Run /render_prices {poster_type} first."
        )
        return

    rendered_path = rendered_files[0]
    await _reply_text(update, f"🔍 Running Gemini Vision QC on {rendered_path.name}…")

    try:
        report  = run_qc(poster_type, rendered_path)
        summary = summarize_report(report)
        await _reply_text(update, summary)

        if report.get("passed"):
            APPROVED_DIR.mkdir(parents=True, exist_ok=True)
            approved_path = APPROVED_DIR / f"{poster_type}_approved.png"
            shutil.copy(rendered_path, approved_path)
            await _reply_text(update,
                f"✅ Approved → Use /publish_approved to post to {PUBLIC_CHANNEL}"
            )
        else:
            await _reply_text(update, "❌ QC failed — not approved for publishing.")
    except Exception as exc:
        await _reply_text(update, f"❌ QC error: {exc}")
        log.error(f"[pipeline] vision_qc failed: {exc}", exc_info=True)


# ── Command: /publish_approved ────────────────────────────────────────────────

async def cmd_publish_approved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Publish all QC-approved posters to the public channel."""
    if not _is_admin(update.effective_user.id):
        return

    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    approved = []
    for pt in PUBLISH_ORDER:
        p = APPROVED_DIR / f"{pt}_approved.png"
        if p.exists():
            approved.append((pt, p))

    if not approved:
        await _reply_text(update,
            "No approved posters found.\n"
            "Run /poster_pipeline <type> or /vision_qc <type> first."
        )
        return

    await _reply_text(update,
        f"📢 Publishing {len(approved)} approved poster(s) to {PUBLIC_CHANNEL}:\n"
        + "\n".join(f"  • {pt}" for pt, _ in approved)
    )

    published = []
    for pt, path in approved:
        try:
            with open(path, "rb") as f:
                msg = await context.bot.send_photo(
                    chat_id=PUBLIC_CHANNEL,
                    photo=f,
                )
            log.info(f"[pipeline] Published {pt} → {PUBLIC_CHANNEL}  msg_id={msg.message_id}")
            published.append(pt)
            # Remove from approved after publishing
            path.unlink(missing_ok=True)
        except Exception as exc:
            await _reply_text(update, f"❌ Failed to publish {pt}: {exc}")
            log.error(f"[pipeline] publish {pt} failed: {exc}")

    if published:
        await _reply_text(update,
            f"✅ Published to {PUBLIC_CHANNEL}: {', '.join(published)}"
        )


# ── Command: /pipeline_status ─────────────────────────────────────────────────

async def cmd_pipeline_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current state of the pipeline."""
    if not _is_admin(update.effective_user.id):
        return

    from python_price_renderer import RENDERED_DIR
    from ai_image_designer import DESIGNS_DIR

    # API key status
    ok, key_msg = check_api_key()
    gemini_key = "✅" if os.environ.get("GEMINI_API_KEY") else "❌ missing"

    lines = [
        "📊 Pipeline Status",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"OpenAI API key: {'✅ set' if ok else '❌ ' + key_msg}",
        f"Gemini API key: {gemini_key}",
        "",
        "📁 Designs (ai_designs/):",
    ]

    for pt in VALID_TYPES + ["logo"]:
        designs = list_designs(pt)
        lines.append(f"  {pt}: {len(designs)} file(s)" + (f" — latest: {designs[0].name}" if designs else ""))

    lines += ["", "🖼 Rendered (rendered_prices/):"]
    if RENDERED_DIR.exists():
        for pt in VALID_TYPES:
            files = sorted(RENDERED_DIR.glob(f"{pt}_*.png"), reverse=True)
            lines.append(f"  {pt}: {len(files)} file(s)" + (f" — latest: {files[0].name}" if files else ""))
    else:
        lines.append("  (none)")

    lines += ["", "✅ Approved (approved_posters/):"]
    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    for pt in PUBLISH_ORDER:
        p = APPROVED_DIR / f"{pt}_approved.png"
        lines.append(f"  {pt}: {'✅ ready' if p.exists() else '—'}")

    lines += ["", "📋 QC Reports:"]
    if QC_DIR.exists():
        reports = sorted(QC_DIR.glob("*.json"), reverse=True)[:5]
        for r in reports:
            lines.append(f"  {r.name}")
    else:
        lines.append("  (none)")

    await _reply_text(update, "\n".join(lines))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Poster Pipeline Agent starting…")

    # Ensure directories exist
    for d in [APPROVED_DIR, PREVIEWS_DIR, QC_DIR,
              Path("/var/www/exchange_bot/ai_designs"),
              Path("/var/www/exchange_bot/rendered_prices")]:
        d.mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("poster_pipeline",   cmd_poster_pipeline))
    app.add_handler(CommandHandler("ai_design_poster",  cmd_ai_design_poster))
    app.add_handler(CommandHandler("render_prices",     cmd_render_prices))
    app.add_handler(CommandHandler("vision_qc",         cmd_vision_qc))
    app.add_handler(CommandHandler("publish_approved",  cmd_publish_approved))
    app.add_handler(CommandHandler("pipeline_status",   cmd_pipeline_status))

    log.info("Commands registered. Bot polling.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
