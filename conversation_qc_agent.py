"""
Conversation Quality Control Agent.

As a library (imported by exchange_brain and scanner_agent):
  - inspect_reply(uid, reply) → approved reply text
  - enable_qc / disable_qc / get_qc_status
  - get_last_reply / get_last_qc / rewrite_last

As a PM2 daemon (python conversation_qc_agent.py):
  - Monitors QC log files and sends hourly reports to admin Telegram

QC Pipeline:
  AI text → score (4 metrics) → rewrite if needed → return approved text
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean

log = logging.getLogger("conv_qc")

LOG_DIR = "/var/www/exchange_bot/conversation_qc_logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Score below this triggers auto-rewrite
QUALITY_THRESHOLD = 0.65

# Global on/off toggle
_QC_ENABLED_GLOBAL = True

# ── Per-user conversation state ───────────────────────────────────────────────

_states: dict[int, dict] = {}

# Tracks the most recent QC action across all users (for admin /conv_last_* commands)
_last_global: dict = {"uid": None, "reply": None, "qc_result": None}


def _get_state(uid: int) -> dict:
    if uid not in _states:
        _states[uid] = {
            "greeting_used": False,
            "turn_count": 0,
            "customer_tone": "neutral",
            "last_topic": None,
            "last_rate": None,
            "qc_enabled": True,
            "last_reply": None,
            "last_qc_result": None,
        }
    return _states[uid]


def reset_user_state(uid: int):
    _states.pop(uid, None)


# ── Toggle controls ───────────────────────────────────────────────────────────

def enable_qc(uid: int | None = None):
    global _QC_ENABLED_GLOBAL
    if uid is None:
        _QC_ENABLED_GLOBAL = True
    else:
        _get_state(uid)["qc_enabled"] = True


def disable_qc(uid: int | None = None):
    global _QC_ENABLED_GLOBAL
    if uid is None:
        _QC_ENABLED_GLOBAL = False
    else:
        _get_state(uid)["qc_enabled"] = False


def get_qc_status(uid: int | None = None) -> dict:
    user_enabled = _get_state(uid)["qc_enabled"] if uid is not None else True
    return {
        "enabled": _QC_ENABLED_GLOBAL and user_enabled,
        "global": _QC_ENABLED_GLOBAL,
        "threshold": QUALITY_THRESHOLD,
        "turn_count": _get_state(uid).get("turn_count", 0) if uid else 0,
        "greeting_used": _get_state(uid).get("greeting_used", False) if uid else False,
    }


def get_last_reply(uid: int | None = None) -> str | None:
    if uid:
        return _get_state(uid).get("last_reply")
    return _last_global.get("reply")


def get_last_qc(uid: int | None = None) -> dict | None:
    if uid:
        return _get_state(uid).get("last_qc_result")
    return _last_global.get("qc_result")


# ── Detection lists ───────────────────────────────────────────────────────────

_GREETING_STARTS = [
    "سلام", "درود", "خوش آمدید", "صبح بخیر", "شب بخیر", "عصر بخیر",
    "hello", "hi ",
]

# Robotic / AI-disclosure phrases — hard failures
_ROBOTIC_PHRASES = [
    "چطور می‌توانم به شما کمک کنم",
    "چطور میتوانم به شما کمک کنم",
    "چطور می توانم به شما کمک کنم",
    "چگونه می‌توانم کمکتان کنم",
    "من یک هوش مصنوعی",
    "من یک ربات",
    "به عنوان یک دستیار",
    "به‌عنوان دستیار",
    "as an ai",
    "i am an ai",
    "i'm an ai",
    "language model",
    "مشتری گرامی",
    "کاربر گرامی",
    "خوشحال می‌شوم که",
    "از اینکه تماس گرفتید",
    "برای کمک بیشتر در",
    "هر سوال دیگری داشتید",
    "در صورت نیاز به کمک بیشتر",
    "همواره در خدمتتان",
    "آیا سوال دیگری دارید",
    "آیا می‌توانم کمک بیشتری بکنم",
    "ممنون از اینکه با ما تماس گرفتید",
]

# Afghan dialect markers
_AFGHAN_MARKERS = [
    "می‌باشد",
    "می باشد",
    "می‌باشید",
    "می باشید",
    "می‌باشم",
    "می باشم",
    "می‌باشند",
    "نموده‌اند",
    "می‌نمایند",
    "می نمایید",
]

# Overly-formal AI-style patterns
_FORMAL_AI_PATTERNS = [
    r"صبور باشید",
    r"لطفاً منتظر بمانید",
    r"در اسرع وقت",
    r"در کوتاه‌ترین زمان ممکن",
    r"با کمال میل",
]

# Literary Persian (نوشتاری — not spoken)
_LITERARY_PERSIAN = [
    "بدین وسیله",
    "ضمناً",
    "متعاقباً",
    "اعلام می‌دارم",
    "به اطلاع می‌رساند",
    "مستدعی است",
]

# Issues that always trigger rewrite regardless of overall score
_ALWAYS_REWRITE = {"repeated_greeting"}


# ── QC Result ─────────────────────────────────────────────────────────────────

@dataclass
class QCResult:
    original: str
    final: str
    human_score: float
    repetition_score: float
    fluency_score: float
    persian_naturalness_score: float
    overall_score: float
    issues: list
    was_rewritten: bool
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ── Scoring functions ─────────────────────────────────────────────────────────

def _has_greeting(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(g) for g in _GREETING_STARTS)


def _score_repetition(text: str, state: dict) -> tuple[float, list]:
    issues = []
    score = 1.0
    # Repeated greeting is a hard fail for this dimension
    if state["greeting_used"] and _has_greeting(text):
        issues.append("repeated_greeting")
        score = 0.0
    else:
        # Check 4-gram overlap with previous reply
        last = state.get("last_reply") or ""
        if last and state["turn_count"] > 0 and len(text) > 15:
            t_words = text.split()
            l_words = last.split()
            if len(t_words) >= 4 and len(l_words) >= 4:
                t_4g = {tuple(t_words[i:i+4]) for i in range(len(t_words)-3)}
                l_4g = {tuple(l_words[i:i+4]) for i in range(len(l_words)-3)}
                overlap = len(t_4g & l_4g) / max(len(t_4g), 1)
                if overlap > 0.45:
                    issues.append("repeated_phrasing")
                    score -= 0.45
    return max(0.0, score), issues


def _score_human(text: str) -> tuple[float, list]:
    issues = []
    score = 1.0
    tl = text.lower()
    for phrase in _ROBOTIC_PHRASES:
        if phrase.lower() in tl:
            issues.append(f"robotic:{phrase[:25]}")
            score -= 0.5
    for pat in _FORMAL_AI_PATTERNS:
        if re.search(pat, text):
            issues.append("formal_ai_pattern")
            score -= 0.2
    # Too long
    sentences = [s.strip() for s in re.split(r'[.!?؟\n]', text) if s.strip()]
    if len(sentences) > 5:
        issues.append("too_long")
        score -= 0.25
    return max(0.0, score), issues


def _score_fluency(text: str) -> tuple[float, list]:
    issues = []
    score = 1.0
    for marker in _AFGHAN_MARKERS:
        if marker in text:
            issues.append(f"afghan:{marker[:20]}")
            score -= 0.4
    return max(0.0, score), issues


def _score_persian_naturalness(text: str) -> tuple[float, list]:
    issues = []
    score = 1.0
    for phrase in _LITERARY_PERSIAN:
        if phrase in text:
            issues.append(f"literary:{phrase}")
            score -= 0.3
    words = text.split()
    if len(words) > 50:
        issues.append("too_verbose")
        score -= 0.2
    elif len(words) == 0:
        issues.append("empty_reply")
        score = 0.0
    return max(0.0, score), issues


def _calculate_scores(text: str, state: dict) -> QCResult:
    rep_score, rep_issues = _score_repetition(text, state)
    hum_score, hum_issues = _score_human(text)
    flu_score, flu_issues = _score_fluency(text)
    nat_score, nat_issues = _score_persian_naturalness(text)

    # Weighted: repetition most important, then human, fluency, naturalness
    overall = (
        rep_score * 0.40 +
        hum_score * 0.30 +
        flu_score * 0.20 +
        nat_score * 0.10
    )
    return QCResult(
        original=text,
        final=text,
        human_score=hum_score,
        repetition_score=rep_score,
        fluency_score=flu_score,
        persian_naturalness_score=nat_score,
        overall_score=overall,
        issues=rep_issues + hum_issues + flu_issues + nat_issues,
        was_rewritten=False,
    )


# ── Simple fixes (no Gemini needed) ──────────────────────────────────────────

def _strip_leading_greeting(text: str) -> str:
    """Remove greeting phrase from the start of text."""
    for g in _GREETING_STARTS:
        pattern = rf'^{re.escape(g)}[\s،,،\.،!\n]*'
        new = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
        if new and new != text:
            return new
    return text


# ── Gemini rewriter ───────────────────────────────────────────────────────────

_REWRITE_PROMPT = """\
تو سیروس هستی، صراف ایرانی در تورنتو. ۲۰ سال تجربه داری. سرت شلوغه.

این پیام مشکل دارد. مشکلات: {issues}

اصلاحش کن:
— فارسی محاوره‌ای تهرانی مدرن
— حداکثر ۲ جمله کوتاه
— بدون ایموجی
— بدون سلام تکراری
— بدون عبارات رباتیک یا رسمی‌بازی
— مستقیم و طبیعی مثل پیام واتساپ یه آدم واقعی

پیام اصلی:
{original}

پیام اصلاح‌شده (فقط پیام، بدون توضیح):"""


async def _rewrite_with_gemini(original: str, issues: list, uid: int) -> str:
    try:
        from config import GEMINI_API_KEY
        from google import genai as _gai
        _client = _gai.Client(api_key=GEMINI_API_KEY)
        prompt = _REWRITE_PROMPT.format(
            issues="، ".join(str(i) for i in issues[:4]),
            original=original,
        )
        resp = await asyncio.to_thread(
            lambda: _client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            ).text.strip()
        )
        resp = re.sub(r'^\s*\[[^\]]*\]\s*', '', resp).strip()
        resp = re.sub(r'\[(CONFIRMED|BARGAINING|SILENT)\]', '', resp).strip()
        if resp and len(resp) > 2:
            log.info("[QC_REWRITE] uid=%s: %r → %r", uid, original[:40], resp[:40])
            return resp
    except Exception as exc:
        log.error("[QC_REWRITE] uid=%s Gemini failed: %s", uid, exc)
    return original


async def _fix_reply(original: str, issues: list, uid: int) -> str:
    """Apply the cheapest fix possible before resorting to Gemini."""
    only_issues = set(issues)

    # Pure repeated-greeting: just strip the greeting (no Gemini cost)
    if only_issues == {"repeated_greeting"}:
        fixed = _strip_leading_greeting(original)
        if fixed and fixed != original:
            log.info("[QC_STRIP_GREETING] uid=%s stripped greeting", uid)
            return fixed

    # Afghan dialect only: simple string replacement is unreliable — use Gemini
    # Robotic phrase / other complex issues: use Gemini
    return await _rewrite_with_gemini(original, issues, uid)


# ── Log ───────────────────────────────────────────────────────────────────────

def _save_log(uid: int, result: QCResult):
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(LOG_DIR, f"qc_{date_str}.jsonl")
        entry = {
            "ts": result.timestamp,
            "uid": uid,
            "original": result.original,
            "final": result.final,
            "was_rewritten": result.was_rewritten,
            "scores": {
                "overall":             round(result.overall_score, 3),
                "human":               round(result.human_score, 3),
                "repetition":          round(result.repetition_score, 3),
                "fluency":             round(result.fluency_score, 3),
                "persian_naturalness": round(result.persian_naturalness_score, 3),
            },
            "issues": result.issues,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.error("[QC_LOG] save failed: %s", exc)


# ── State updater ─────────────────────────────────────────────────────────────

def _update_state(uid: int, reply: str, result: QCResult):
    state = _get_state(uid)
    if _has_greeting(reply):
        state["greeting_used"] = True
    state["turn_count"] += 1
    state["last_reply"] = reply
    if re.search(r'\d[\d,]+\s*تومان', reply):
        state["last_rate"] = reply
    qc_snapshot = {
        "scores": {
            "overall":             round(result.overall_score, 3),
            "human":               round(result.human_score, 3),
            "repetition":          round(result.repetition_score, 3),
            "fluency":             round(result.fluency_score, 3),
            "persian_naturalness": round(result.persian_naturalness_score, 3),
        },
        "issues":       result.issues,
        "was_rewritten": result.was_rewritten,
        "timestamp":    result.timestamp,
    }
    state["last_qc_result"] = qc_snapshot
    _last_global["uid"]       = uid
    _last_global["reply"]     = reply
    _last_global["qc_result"] = qc_snapshot


# ── Main API ──────────────────────────────────────────────────────────────────

async def inspect_reply(uid: int, reply: str) -> str:
    """
    Inspect a reply before sending. Returns the final (possibly rewritten) reply.

    Call this after AI generates a response and before sending to customer.
    """
    if not reply:
        return reply

    state = _get_state(uid)
    if not _QC_ENABLED_GLOBAL or not state["qc_enabled"]:
        # QC off — still track state so greeting/rate memory works
        _update_state(uid, reply, QCResult(
            original=reply, final=reply,
            human_score=1.0, repetition_score=1.0,
            fluency_score=1.0, persian_naturalness_score=1.0,
            overall_score=1.0, issues=[], was_rewritten=False,
        ))
        return reply

    result = _calculate_scores(reply, state)
    log.info("[QC] uid=%s score=%.2f issues=%s",
             uid, result.overall_score, result.issues or "none")

    # Decide whether to rewrite
    has_critical = bool(_ALWAYS_REWRITE & set(result.issues))
    needs_rewrite = (
        has_critical or
        result.overall_score < QUALITY_THRESHOLD or
        any(i.startswith("robotic:") for i in result.issues) or
        any(i.startswith("afghan:") for i in result.issues)
    )

    if needs_rewrite and result.issues:
        fixed = await _fix_reply(reply, result.issues, uid)
        if fixed != reply:
            result.final = fixed
            result.was_rewritten = True

    _save_log(uid, result)
    _update_state(uid, result.final, result)

    return result.final


async def rewrite_last(uid: int | None = None) -> str | None:
    """Force-rewrite the last reply. Returns new version or None."""
    target_uid = uid or _last_global.get("uid")
    if not target_uid:
        return None
    last = _last_global["reply"] if not uid else _get_state(uid).get("last_reply")
    if not last:
        return None
    rewritten = await _rewrite_with_gemini(last, ["manual_rewrite_request"], target_uid)
    if uid:
        _get_state(uid)["last_reply"] = rewritten
    _last_global["reply"] = rewritten
    return rewritten


# ── PM2 daemon ────────────────────────────────────────────────────────────────

async def _send_daily_report(bot, admin_id: int):
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(LOG_DIR, f"qc_{date_str}.jsonl")
    if not os.path.exists(path):
        return

    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass

    if not entries:
        return

    total     = len(entries)
    rewritten = sum(1 for e in entries if e.get("was_rewritten"))
    scores    = [e["scores"]["overall"] for e in entries if "scores" in e]
    avg_score = mean(scores) if scores else 0.0

    issue_counter: dict[str, int] = {}
    for e in entries:
        for iss in e.get("issues", []):
            key = iss.split(":")[0]
            issue_counter[key] = issue_counter.get(key, 0) + 1
    top = sorted(issue_counter.items(), key=lambda x: -x[1])[:3]
    issues_text = "\n".join(f"  • {k}: {v}" for k, v in top) or "  هیچ مشکلی"

    report = (
        f"📊 گزارش QC — {date_str}\n"
        f"پیام‌های بررسی‌شده: {total}\n"
        f"بازنویسی‌شده: {rewritten} ({rewritten/total:.0%})\n"
        f"میانگین امتیاز: {avg_score:.2f}/1.00\n"
        f"مشکلات رایج:\n{issues_text}"
    )
    await bot.send_message(chat_id=admin_id, text=report)
    log.info("[QC_DAEMON] daily report sent: %d entries, avg_score=%.2f", total, avg_score)


async def _daemon_loop():
    log.info("[QC_DAEMON] started — monitoring %s", LOG_DIR)
    try:
        from config import BOT_TOKEN, ADMIN_ID
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        admin_id = ADMIN_ID
    except Exception as exc:
        log.error("[QC_DAEMON] Telegram init failed: %s — reports disabled", exc)
        bot = None
        admin_id = None

    reported_today: str | None = None

    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            # Send report once per day at 09:00
            if now.hour == 9 and now.minute < 5 and reported_today != today:
                reported_today = today
                if bot and admin_id:
                    await _send_daily_report(bot, admin_id)
        except Exception as exc:
            log.error("[QC_DAEMON] loop error: %s", exc)
        await asyncio.sleep(60)


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [conv_qc] %(message)s",
    )
    log.info("Conversation QC daemon starting")
    log.info("QC logic runs inline within exchange_brain + scanner_agent processes")
    log.info("This daemon watches logs and sends daily reports to admin")
    asyncio.run(_daemon_loop())
