"""
nightly_learning.py — Runs at 02:00 daily.

  - Analyzes yesterday's conversations
  - Extracts successful/failed patterns
  - Updates daily_learning table
  - Sends summary report to admin via Telegram
"""

import asyncio
import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, date

from telegram import Bot
from config import BOT_TOKEN, ADMIN_ID, GEMINI_API_KEY

log = logging.getLogger("nightly_learning")
DB_PATH = "/var/www/exchange_bot/exchange.db"


# ─── Analysis helpers ─────────────────────────────────────────────────────────

def _analyze_yesterday() -> dict:
    conn   = sqlite3.connect(DB_PATH)
    today  = date.today().isoformat()
    yest   = (date.today() - timedelta(days=1)).isoformat()

    def _c(sql, *p):
        return conn.execute(sql, p).fetchone()[0] or 0

    total   = _c("SELECT COUNT(*) FROM conversation_memory WHERE timestamp LIKE ? AND role='user'", f"{yest}%")
    success = _c("SELECT COUNT(*) FROM agent_learning_log WHERE deal_outcome='success' AND created_at LIKE ?", f"{yest}%")
    failed  = _c("SELECT COUNT(*) FROM agent_learning_log WHERE deal_outcome='failed' AND created_at LIKE ?", f"{yest}%")

    # Emotion distribution
    emotion_rows = conn.execute(
        "SELECT dominant_emotion, COUNT(*) FROM emotion_history WHERE detected_at LIKE ? GROUP BY dominant_emotion",
        (f"{yest}%",)
    ).fetchall()
    emotions = {r[0]: r[1] for r in emotion_rows}

    # Successful reply patterns (admin marked as good)
    good_rows = conn.execute(
        """SELECT al.agent_reply FROM agent_learning_log al
           JOIN admin_feedback af ON af.log_id=al.id
           WHERE af.feedback_type='good_reply' AND al.created_at LIKE ?
           LIMIT 20""",
        (f"{yest}%",)
    ).fetchall()

    # Failed patterns (admin marked bad or deal failed)
    bad_rows = conn.execute(
        """SELECT al.agent_reply FROM agent_learning_log al
           WHERE al.deal_outcome='failed' AND al.created_at LIKE ?
           LIMIT 20""",
        (f"{yest}%",)
    ).fetchall()

    conn.close()

    # Save patterns
    _save_patterns([r[0] for r in good_rows if r[0]], "success", emotions)
    _save_patterns([r[0] for r in bad_rows if r[0]], "fail", emotions)

    return {
        "date":          yest,
        "total_convs":   total,
        "success":       success,
        "failed":        failed,
        "emotions":      emotions,
        "good_patterns": len(good_rows),
        "bad_patterns":  len(bad_rows),
    }


def _save_patterns(replies: list, outcome: str, emotions: dict):
    dominant = max(emotions, key=emotions.get) if emotions else "neutral"
    conn = sqlite3.connect(DB_PATH)
    now  = datetime.now().isoformat()
    table = "successful_patterns" if outcome == "success" else "failed_patterns"
    col   = "success_count"       if outcome == "success" else "fail_count"
    for reply in replies[:10]:
        snippet = reply[:200].strip()
        if not snippet:
            continue
        try:
            conn.execute(
                f"""INSERT INTO {table} (pattern_text, {col}, customer_mood, last_used)
                    VALUES (?,1,?,?)
                    ON CONFLICT(pattern_text) DO UPDATE SET
                        {col}={col}+1, last_used=excluded.last_used""",
                (snippet, dominant, now)
            )
        except Exception:
            pass
    conn.commit()
    conn.close()


def _save_daily_record(stats: dict):
    conn = sqlite3.connect(DB_PATH)
    total  = max(stats["total_convs"], 1)
    rate   = stats["success"] / total
    conn.execute(
        """INSERT INTO daily_learning (date, total_deals, success_rate, common_emotions, notes)
           VALUES (?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET
               total_deals=excluded.total_deals,
               success_rate=excluded.success_rate,
               common_emotions=excluded.common_emotions""",
        (
            stats["date"],
            stats["success"],
            round(rate, 3),
            json.dumps(stats["emotions"], ensure_ascii=False),
            f"good_patterns={stats['good_patterns']} bad_patterns={stats['bad_patterns']}",
        )
    )
    conn.commit()
    conn.close()


# ─── Gemini prompt update ─────────────────────────────────────────────────────

def _build_updated_prompt_notes(stats: dict) -> str:
    """Generate a short note about what worked/failed — injected into next day's brain."""
    try:
        conn = sqlite3.connect(DB_PATH)
        good = conn.execute(
            "SELECT pattern_text FROM successful_patterns ORDER BY success_count DESC LIMIT 3"
        ).fetchall()
        bad  = conn.execute(
            "SELECT pattern_text FROM failed_patterns ORDER BY fail_count DESC LIMIT 3"
        ).fetchall()
        conn.close()

        notes = []
        if good:
            notes.append("الگوهای موفق دیروز:\n" + "\n".join(f"• {r[0][:80]}" for r in good))
        if bad:
            notes.append("الگوهای ناموفق دیروز:\n" + "\n".join(f"• {r[0][:80]}" for r in bad))
        return "\n\n".join(notes)
    except Exception:
        return ""


# ─── Admin report ─────────────────────────────────────────────────────────────

async def _send_report(stats: dict, prompt_notes: str):
    try:
        bot  = Bot(token=BOT_TOKEN)
        emot = ", ".join(f"{k}:{v}" for k, v in list(stats["emotions"].items())[:5])
        text = (
            f"🌙 گزارش یادگیری شبانه\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 {stats['date']}\n"
            f"💬 گفتگوها: {stats['total_convs']}\n"
            f"✅ موفق: {stats['success']} | ❌ ناموفق: {stats['failed']}\n"
            f"😊 احساسات: {emot or '—'}\n"
            f"📈 الگوهای خوب ثبت شده: {stats['good_patterns']}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )
        if prompt_notes:
            text += prompt_notes[:800] + "\n"
        await bot.send_message(chat_id=ADMIN_ID, text=text[:4096])
    except Exception as e:
        log.error("Send report error: %s", e)


# ─── Scheduler ───────────────────────────────────────────────────────────────

async def run_learning_cycle():
    log.info("Nightly learning started")
    stats        = _analyze_yesterday()
    prompt_notes = _build_updated_prompt_notes(stats)
    _save_daily_record(stats)
    await _send_report(stats, prompt_notes)
    log.info("Nightly learning done: %s", stats)


async def _wait_until_2am():
    now   = datetime.now()
    target = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    wait_s = (target - now).total_seconds()
    log.info("Next nightly run at %s (%.0f s)", target.strftime("%H:%M %d-%m"), wait_s)
    await asyncio.sleep(wait_s)


async def main():
    logging.basicConfig(level=logging.INFO)
    log.info("Nightly learning scheduler started")
    while True:
        await _wait_until_2am()
        await run_learning_cycle()


if __name__ == "__main__":
    asyncio.run(main())
