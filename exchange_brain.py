"""
exchange_brain.py — Central intelligence controller.

Before every reply this module:
  1. Loads customer profile & emotional fingerprint
  2. Loads last 6 conversation turns
  3. Loads admin-corrected mistakes to never repeat
  4. Loads successful closing patterns
  5. Gets live price (DB → current_price.json fallback)
  6. Analyzes customer emotion (rule-based + background Hume)
  7. Decides voice vs text
  8. Generates response via Gemini-2.5-flash
  9. Saves interaction to conversation_memory

NEVER invents prices. Uses exact values from price engine only.
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime

from config import (
    ADMIN_ID, BOT_TOKEN, GEMINI_API_KEY,
    BUY_SPREAD_TOMAN, SELL_SPREAD_TOMAN, VOICE_REPLIES_ENABLED,
)
from price_engine import calculate_rate
from emotion_engine import analyze as analyze_emotion, get_emotional_fingerprint
from natural_replies import get_reply

log = logging.getLogger("exchange_brain")
DB_PATH = "/var/www/exchange_bot/exchange.db"
CURRENT_PRICE_JSON = "/var/www/exchange_bot/current_price.json"

# ─── Gemini init ──────────────────────────────────────────────────────────────

_GEMINI_OK = False
_gemini    = None
try:
    from google import genai as _gai
    _gemini   = _gai.Client(api_key=GEMINI_API_KEY)
    _GEMINI_OK = True
    log.info("Gemini ready in exchange_brain")
except Exception as _e:
    log.warning("Gemini unavailable: %s", _e)

# ─── Price keyword detection ──────────────────────────────────────────────────

_PRICE_KW = [
    "قیمت", "نرخ", "چنده", "چقدره", "چقدر",
    "rate", "price", "تتر", "usdt", "دلار", "usd", "cad", "کانادا",
    "نرخ خرید", "نرخ فروش",
]

def is_price_request(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _PRICE_KW)

def detect_currency(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["تتر", "usdt"]):
        return "USDT"
    if any(k in t for k in ["آمریکا", "امریکا", " usd"]):
        return "USD"
    return "CAD"

# ─── DB helpers ───────────────────────────────────────────────────────────────

def _q(conn, sql, params=()):
    return conn.execute(sql, params)


def _ensure_profile(uid: int, name: str = ""):
    try:
        conn = sqlite3.connect(DB_PATH)
        now = datetime.now().isoformat()
        _q(conn,
           """INSERT OR IGNORE INTO customer_profiles
              (user_id, username, profile_type, total_conversations, total_deals,
               successful_deals, last_seen, created_at)
              VALUES (?,?,?,0,0,0,?,?)""",
           (str(uid), name, "unknown", now, now))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("_ensure_profile: %s", e)


def _get_profile(uid: int) -> dict:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = _q(conn, "SELECT * FROM customer_profiles WHERE user_id=?", (str(uid),)).fetchone()
        if not row:
            conn.close()
            return {}
        cols = [d[0] for d in conn.execute("SELECT * FROM customer_profiles LIMIT 0").description]
        conn.close()
        return dict(zip(cols, row))
    except Exception:
        return {}


def _get_history(uid: int, limit: int = 6) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = _q(conn,
                  "SELECT role, message FROM conversation_memory WHERE user_id=? ORDER BY id DESC LIMIT ?",
                  (str(uid), limit)).fetchall()
        conn.close()
        return list(reversed(rows))
    except Exception:
        return []


def _get_mistakes(limit: int = 8) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = _q(conn,
                  "SELECT original_reply, corrected_reply, mistake_type FROM learned_mistakes ORDER BY id DESC LIMIT ?",
                  (limit,)).fetchall()
        conn.close()
        return [{"original": r[0], "corrected": r[1], "type": r[2]} for r in rows]
    except Exception:
        return []


def _get_patterns(limit: int = 5) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = _q(conn,
                  "SELECT pattern_text FROM successful_patterns ORDER BY success_count DESC LIMIT ?",
                  (limit,)).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _save_turn(uid: int, role: str, message: str,
               was_voice: bool = False, emotion: str = None) -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = _q(conn,
                 """INSERT INTO conversation_memory (user_id, role, message, timestamp, was_voice, emotion_detected)
                    VALUES (?,?,?,?,?,?)""",
                 (str(uid), role, message, datetime.now().isoformat(), int(was_voice), emotion))
        cid = cur.lastrowid
        conn.commit()
        conn.close()
        return cid
    except Exception as e:
        log.error("_save_turn: %s", e)
        return None


def _update_profile(uid: int, **kw):
    if not kw:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        sets = ", ".join(f"{k}=?" for k in kw)
        vals = list(kw.values()) + [str(uid)]
        conn.execute(f"UPDATE customer_profiles SET {sets} WHERE user_id=?", vals)
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("_update_profile: %s", e)


def _save_pricing_decision(uid: int, price: float, source: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO pricing_decisions (user_id, amount_cad, price_toman, source, created_at) VALUES (?,?,?,?,?)",
            (str(uid), 0, price, source, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def save_learned_mistake(original: str, corrected: str, mistake_type: str = "admin_correction", admin_id: str = "admin"):
    """Save an admin correction so the brain never repeats it."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO learned_mistakes (mistake_type, original_reply, corrected_reply, admin_id, created_at) VALUES (?,?,?,?,?)",
            (mistake_type, original, corrected, str(admin_id), datetime.now().isoformat()))
        conn.commit()
        conn.close()
        log.info("Learned mistake saved: %s → %s", original[:40], corrected[:40])
    except Exception as e:
        log.error("save_learned_mistake: %s", e)


def save_successful_pattern(pattern: str, mood: str = "neutral"):
    """Record a reply pattern that closed a deal."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO successful_patterns (pattern_text, success_count, customer_mood, last_used)
               VALUES (?,1,?,?)
               ON CONFLICT(pattern_text) DO UPDATE SET
                 success_count=success_count+1, last_used=excluded.last_used""",
            (pattern[:300], mood, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("save_successful_pattern: %s", e)


# ─── Live price ───────────────────────────────────────────────────────────────

def _get_live_price(currency: str) -> dict | None:
    rate = calculate_rate(currency)
    if rate:
        return rate
    try:
        with open(CURRENT_PRICE_JSON) as f:
            data = json.load(f)
        entry = data.get(currency)
        if entry and entry.get("price"):
            p = int(entry["price"])
            return {
                "currency":   currency,
                "raw_source": p,
                "our_sell":   p - SELL_SPREAD_TOMAN,
                "our_buy":    p - BUY_SPREAD_TOMAN,
                "amount":     0,
                "margin":     0,
            }
    except Exception:
        pass
    return None


# ─── Prompt builder ───────────────────────────────────────────────────────────

_SYSTEM = """
تو سیروس هستی — صراف ایرانی تورنتو با ۲۰ سال تجربه. سرت شلوغه.

قوانین ثابت:
- فارسی محاوره‌ای تهرانی مدرن — مثل پیام واتساپ یه آدم واقعی
- حداکثر ۲ جمله کوتاه در هر پیام
- هرگز عدد اختراع نکن
- اگه نرخ نداری: «نرخ الان تابلوئه، چند دقیقه دیگه دوباره پیام بدید.»
- ایموجی نفرست
- جملات ممنوع: «یه لحظه»، «چک می‌کنم»، «صبر کن»، «مشتری گرامی»،
  «کاربر گرامی»، «در حال بررسی»، «خیالتون راحت باشه»

خدمات: خرید و فروش CAD/USD/USDT — Interac (۱۵–۳۰ دقیقه) — انتقال ایران/ترکیه/امارات

نشانه‌های انتهای پیام (فقط اگه لازم بود):
[CONFIRMED] — توافق نهایی   [BARGAINING] — چانه‌زنی   [SILENT] — مشتری جواب نداد
"""

_EMOTION_GUIDE = {
    "urgent":     "پیام خیلی کوتاه. بدون سلام. مستقیم برو سر قیمت.",
    "suspicious": "اطمینان‌بخش باش. تجربه ۲۰ ساله رو نشون بده. شفاف.",
    "negotiator": "از ۲۰۰ تومان بالاتر از نرخ ما شروع کن. فضای چانه داری.",
    "vip":        "بهترین قیمت همین اول. گرم‌تر. از اسمش استفاده کن.",
    "friendly":   "کمی گرم‌تر. یه جمله بیشتر.",
    "serious":    "فقط تجاری. بدون گپ. مستقیم.",
    "impatient":  "یک جمله. همین.",
    "neutral":    "طبیعی. مستقیم.",
}

_CUR_LABEL = {"USDT": "تتر", "USD": "دلار آمریکا", "CAD": "دلار کانادا"}


def _build_prompt(uid: int, text: str, profile: dict, emotion: dict,
                  fp: dict, history: list, price_data: dict | None,
                  mistakes: list, is_price: bool, currency: str) -> str:

    dominant = emotion.get("dominant_emotion", "neutral")
    is_vip   = bool(profile.get("is_vip"))
    if is_vip:
        dominant = "vip"
    emotion_instr = _EMOTION_GUIDE.get(dominant, _EMOTION_GUIDE["neutral"])

    profile_hint = ""
    dc = profile.get("deal_count", profile.get("total_deals", 0))
    if is_vip:
        profile_hint = "⭐ VIP — بهترین قیمت، لحن گرم."
    elif dc > 0:
        profile_hint = f"مشتری قدیمی ({dc} معامله)."
    if profile.get("negotiation_style") == "aggressive":
        profile_hint += " چانه‌زن جدی."
    fp_note = ""
    if fp.get("dominant_pattern") not in ("unknown", None):
        fp_note = f"الگوی احساسی طولانی‌مدت: {fp['dominant_pattern']}"

    # Price block
    if is_price:
        if price_data:
            label = _CUR_LABEL.get(currency, currency)
            neg_buf = 200 if dominant == "negotiator" else 0
            sell = price_data.get("our_sell", 0) + neg_buf
            buy  = price_data.get("our_buy", 0)
            price_block = (
                f"[نرخ تأیید شده — فقط این اعداد، هیچ عددی اختراع نکن]\n"
                f"فروش ما به مشتری ({label}): {sell:,} تومان\n"
                f"خرید ما از مشتری ({label}): {buy:,} تومان\n"
                "پاسخ: کوتاه، مستقیم، فقط نرخ."
            )
        else:
            price_block = (
                "قیمت لحظه‌ای در دسترس نیست.\n"
                "بگو: «نرخ الان تابلوئه، چند دقیقه دیگه دوباره پیام بدید.»\n"
                "هیچ عددی اختراع نکن."
            )
    else:
        price_block = ""

    mistakes_block = ""
    if mistakes:
        mistakes_block = "اشتباهاتی که هرگز نباید تکرار بشن:\n"
        for m in mistakes[:3]:
            if m.get("original"):
                c = m.get("corrected", "") or "بهتر"
                mistakes_block += f'- ممنوع: "{m["original"][:50]}" → باید: "{c[:50]}"\n'

    history_text = ""
    if history:
        history_text = "تاریخچه:\n" + "\n".join(
            f"{'مشتری' if r[0]=='user' else 'سیروس'}: {r[1]}"
            for r in history[-4:]
        )

    return (
        f"{_SYSTEM}\n\n"
        f"━━ وضعیت ━━\n"
        f"احساس: {dominant} | اطمینان: {emotion.get('emotion_confidence', 0):.0%}\n"
        f"دستور سبک: {emotion_instr}\n"
        f"{profile_hint}\n{fp_note}\n\n"
        f"{price_block}\n"
        f"{mistakes_block}\n"
        f"{history_text}\n\n"
        f"پیام مشتری: {text}\n"
        f"پاسخ:"
    )


# ─── Voice/text decision ─────────────────────────────────────────────────────

# Per-uid conversation timing for Rule 3 (text→voice escalation after 60s)
_conv_start: dict[int, dict] = {}
_last_msg:   dict[int, float] = {}
_RESET_IDLE = 300  # 5 min idle resets conversation type
_ESCALATE_S = 60   # 60s text conversation switches to voice


def _decide_voice(uid: int, is_voice_in: bool, is_price: bool,
                  profile: dict, emotion: dict) -> bool:
    if not VOICE_REPLIES_ENABLED:
        return False
    if is_price:
        return False  # Rule 4: price → always text

    now = time.time()
    start = _conv_start.get(uid)

    # Reset stale conversation
    if start and (now - _last_msg.get(uid, now)) > _RESET_IDLE:
        _conv_start.pop(uid, None)
        start = None

    _last_msg[uid] = now

    if start is None:
        _conv_start[uid] = {"time": now, "type": "voice" if is_voice_in else "text"}
        start = _conv_start[uid]

    if is_voice_in:           # Rule 2
        return True
    if profile.get("prefers_voice"):
        return True
    if (start["type"] == "text"  # Rule 3
            and (now - start["time"]) > _ESCALATE_S):
        return True
    return False               # Rule 1: text → text


# ─── Main entry point ─────────────────────────────────────────────────────────

async def process(uid: int, text: str, sender_name: str = "",
                  is_voice_input: bool = False) -> tuple[str, bool]:
    """
    Central message processor. Returns (reply_text, use_voice).
    Never invents prices. Uses only live DB/JSON data.
    """
    _ensure_profile(uid, sender_name)

    # ── 1. Load context ──────────────────────────────────────────
    profile  = _get_profile(uid)
    history  = _get_history(uid)
    mistakes = _get_mistakes()
    fp       = get_emotional_fingerprint(uid)

    # ── 2. Emotion analysis ──────────────────────────────────────
    emotion = await analyze_emotion(uid, text)

    # ── 3. Price detection ───────────────────────────────────────
    price_req  = is_price_request(text)
    currency   = detect_currency(text)
    price_data = _get_live_price(currency) if price_req else None

    # ── 4. Save user turn ────────────────────────────────────────
    _save_turn(uid, "user", text, was_voice=is_voice_input, emotion=emotion["dominant_emotion"])

    # ── 5. Update profile ────────────────────────────────────────
    _update_profile(
        uid,
        mood=emotion["dominant_emotion"],
        last_seen=datetime.now().isoformat(),
        total_conversations=(profile.get("total_conversations") or 0) + 1,
    )

    # ── 6. Generate reply ────────────────────────────────────────
    if price_req and not price_data:
        reply = "نرخ الان تابلوئه، چند دقیقه دیگه دوباره پیام بدید."
    elif _GEMINI_OK:
        prompt = _build_prompt(
            uid, text, profile, emotion, fp, history,
            price_data, mistakes, price_req, currency
        )
        try:
            resp = await asyncio.to_thread(
                lambda: _gemini.models.generate_content(
                    model="gemini-2.5-flash", contents=prompt
                ).text.strip()
            )
            reply = resp
        except Exception as e:
            log.error("Gemini error: %s", e)
            reply = get_reply("customer_buy")
    else:
        reply = get_reply("customer_buy")

    # Strip status tags from text sent to customer
    reply = re.sub(r'\[(CONFIRMED|BARGAINING|SILENT)\]', '', reply).strip()
    # Strip ElevenLabs stage-direction brackets
    reply = re.sub(r'^\s*\[[^\]]*\]\s*', '', reply).strip()

    # Notify admin on confirmed deal
    if "[CONFIRMED]" in reply or "CONFIRMED" in reply:
        try:
            from telegram import Bot
            bot = Bot(token=BOT_TOKEN)
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=f"✅ توافق شد\n👤 uid={uid}\n💬 {text[:80]}\n🤝 {reply[:80]}"
            )
        except Exception:
            pass

    # ── 7. Save bot turn & pricing ───────────────────────────────
    _save_turn(uid, "bot", reply)
    if price_req and price_data:
        _save_pricing_decision(uid, price_data.get("our_sell", 0), currency)

    # ── 8. Decide voice ──────────────────────────────────────────
    use_voice = _decide_voice(uid, is_voice_input, price_req, profile, emotion)

    return reply, use_voice
