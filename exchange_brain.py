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
from zoneinfo import ZoneInfo

from config import (
    ADMIN_ID, BOT_TOKEN, GEMINI_API_KEY,
    BUY_SPREAD_TOMAN, SELL_SPREAD_TOMAN,
    TIMEZONE, USD_CAD_OPEN_HOUR, USD_CAD_CLOSE_HOUR,
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
    log.info("[BRAIN] Gemini ready")
except Exception as _e:
    log.warning("[BRAIN] Gemini unavailable: %s", _e)

# ─── ElevenLabs Agent — disabled ─────────────────────────────────────────────

_ELEVEN_OK = False
_eleven_chat = None
log.info("[BRAIN] ElevenLabs Agent disabled — Gemini handles all text replies")

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
    if any(k in t for k in ["آمریکا", "امریکا", " usd", "دلار آمریکا"]):
        return "USD"
    if any(k in t for k in ["کانادا", "cad", "کانادایی"]):
        return "CAD"
    return "USD"   # default when just "دلار" with no qualifier


def _is_business_hours() -> bool:
    """USD/CAD only available 9 AM – 5 PM America/Toronto."""
    try:
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        return USD_CAD_OPEN_HOUR <= now.hour < USD_CAD_CLOSE_HOUR
    except Exception:
        return True   # fail open


def _detect_direction(text: str, history: list) -> str | None:
    """Return 'buyer' | 'seller' | None. Only reads user turns from history."""
    sell_kw = ["فروش", "sell", "میفروشم", "می‌فروشم", "می فروشم", "فروشی", "دارم فروش"]
    buy_kw  = ["خرید", "buy", "میخرم", "می‌خرم", "می خرم", "خریدار", "میخوام", "نیاز دارم"]
    # Only check current text + user messages in history (skip bot messages)
    user_texts = [text] + [m for role, m in (history or [])[-6:] if role == "user"]
    for t in user_texts:
        tl = t.lower()
        if any(k in tl for k in sell_kw):
            return "seller"
        if any(k in tl for k in buy_kw):
            return "buyer"
    return None


def _detect_method(text: str, history: list) -> str | None:
    """Return 'etransfer' | 'cash' | 'cheque' | None. Only reads user turns."""
    etransfer_kw = ["حواله", "e-transfer", "etransfer", "ترنسفر", "اینترک", "interac"]
    cash_kw      = ["نقدی", "نقد", "cash", "کش"]
    cheque_kw    = ["چک", "cheque", "check"]
    user_texts = [text] + [m for role, m in (history or [])[-6:] if role == "user"]
    for t in user_texts:
        tl = t.lower()
        if any(k in tl for k in etransfer_kw):
            return "etransfer"
        if any(k in tl for k in cash_kw):
            return "cash"
        if any(k in tl for k in cheque_kw):
            return "cheque"
    return None

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
        log.error("[BRAIN] _ensure_profile: %s", e)


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
        log.error("[BRAIN] _save_turn: %s", e)
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
        log.error("[BRAIN] _update_profile: %s", e)


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
        log.info("[BRAIN] Learned mistake saved: %s → %s", original[:40], corrected[:40])
    except Exception as e:
        log.error("[BRAIN] save_learned_mistake: %s", e)


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
        log.error("[BRAIN] save_successful_pattern: %s", e)


# ─── Live price ───────────────────────────────────────────────────────────────

def _get_live_price(currency: str, method: str = None) -> dict | None:
    rate = calculate_rate(currency, method=method)
    if rate:
        return rate
    try:
        with open(CURRENT_PRICE_JSON) as f:
            data = json.load(f)
        entry = data.get(currency)
        if entry and entry.get("price"):
            p = int(entry["price"])
            # Use pre-computed values from price_engine when available;
            # USDT has 0 sell spread — don't apply SELL_SPREAD_TOMAN (500) here.
            our_sell = (entry.get("our_sell")
                        or (p if currency == "USDT" else p - SELL_SPREAD_TOMAN))
            our_buy  = (entry.get("our_buy")
                        or entry.get("buy")
                        or p - BUY_SPREAD_TOMAN)
            return {
                "currency":   currency,
                "raw_source": p,
                "our_sell":   int(our_sell),
                "our_buy":    int(our_buy),
                "amount":     0,
                "margin":     0,
                "payment_method": method or "general",
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
    _METHOD_LABEL = {"etransfer": "حواله", "cash": "نقدی", "cheque": "چک", "general": ""}

    if is_price:
        if price_data:
            label  = _CUR_LABEL.get(currency, currency)
            method = price_data.get("payment_method", "general")
            mlabel = _METHOD_LABEL.get(method, "")
            if mlabel:
                label = f"{label} ({mlabel})"
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



# ─── Main entry point ─────────────────────────────────────────────────────────

async def process(uid: int, text: str, sender_name: str = "",
                  is_voice_input: bool = False) -> tuple[str, bool]:
    """
    Central message processor. Returns (reply_text, use_voice).
    Never invents prices. Uses only live DB/JSON data.
    """
    log.info("[BRAIN_IN] uid=%s voice=%s text=%r", uid, is_voice_input, text[:60])
    _ensure_profile(uid, sender_name)

    # ── 1. Load context ──────────────────────────────────────────
    profile  = _get_profile(uid)
    history  = _get_history(uid)
    mistakes = _get_mistakes()
    fp       = get_emotional_fingerprint(uid)

    # ── 2. Emotion analysis ──────────────────────────────────────
    emotion = await analyze_emotion(uid, text)

    # ── 3. Price detection ───────────────────────────────────────
    price_req = is_price_request(text)
    currency  = detect_currency(text)
    log.info("[INTENT_DETECTED] uid=%s price_req=%s currency=%s emotion=%s",
             uid, price_req, currency, emotion.get("dominant_emotion"))

    # ── 4. Save user turn ────────────────────────────────────────
    _save_turn(uid, "user", text, was_voice=is_voice_input, emotion=emotion["dominant_emotion"])

    # ── 5. Update profile ────────────────────────────────────────
    _update_profile(
        uid,
        mood=emotion["dominant_emotion"],
        last_seen=datetime.now().isoformat(),
        total_conversations=(profile.get("total_conversations") or 0) + 1,
    )

    # ── 6. Business rules for price requests ─────────────────────
    price_data = None
    if price_req:
        log.info("[PRICE_REQUEST] uid=%s currency=%s", uid, currency)
        if currency != "USDT":
            # USD/CAD: time-gated
            if not _is_business_hours():
                reply = "نرخ دلار و دلار کانادا فقط بین ساعت ۹ صبح تا ۵ عصر ارائه میشه."
                _save_turn(uid, "bot", reply)
                return reply, True

            # Must know buy/sell direction
            direction = _detect_direction(text, history)
            if not direction:
                reply = "برای خرید می‌خواید یا فروش؟"
                _save_turn(uid, "bot", reply)
                return reply, True

            # Must know payment method
            method = _detect_method(text, history)
            if not method:
                reply = "حواله، نقدی یا چک؟"
                _save_turn(uid, "bot", reply)
                return reply, True

            price_data = _get_live_price(currency, method=method)
        else:
            # USDT 24/7 — no method needed
            method    = None
            direction = _detect_direction(text, history)
            price_data = _get_live_price("USDT")

        log.info("[PRICE_REQUEST] uid=%s price_data=%s",
                 uid, bool(price_data))

    # ── 7. Generate reply ────────────────────────────────────────
    if price_req and not price_data:
        log.info("[ROUTE] using local price engine uid=%s (no data available)", uid)
        reply = "نرخ الان تابلوئه، چند دقیقه دیگه دوباره پیام بدید."
        log.warning("[PRICE_REQUEST] uid=%s no live price data available", uid)

    elif price_req:
        # Price data available — local engine supplied numbers, Gemini formats reply
        log.info("[ROUTE] using local price engine uid=%s currency=%s", uid, currency)
        if _GEMINI_OK:
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
                log.error("[ERROR] uid=%s Gemini price-format error: %s", uid, e)
                _lbl = {"USDT": "تتر", "USD": "دلار", "CAD": "دلار کانادا"}.get(currency, currency)
                reply = (f"فروش {_lbl}: {price_data.get('our_sell', 0):,} تومان — "
                         f"خرید: {price_data.get('our_buy', 0):,} تومان")
        else:
            _lbl = {"USDT": "تتر", "USD": "دلار", "CAD": "دلار کانادا"}.get(currency, currency)
            reply = (f"فروش {_lbl}: {price_data.get('our_sell', 0):,} تومان — "
                     f"خرید: {price_data.get('our_buy', 0):,} تومان")

    elif _GEMINI_OK:
        log.info("[ROUTE] using Gemini uid=%s", uid)
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
            log.error("[ERROR] uid=%s Gemini error: %s", uid, e)
            reply = "مشکلی پیش اومد، دوباره پیام بده."

    else:
        reply = "مشکلی پیش اومد، دوباره پیام بده."

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

    # ── 8. Save bot turn & pricing ───────────────────────────────
    _save_turn(uid, "bot", reply)
    if price_req and price_data:
        _save_pricing_decision(uid, price_data.get("our_sell", 0), currency)

    log.info("[VOICE_REPLY] uid=%s reply_len=%d reply=%r", uid, len(reply), reply[:60])
    return reply, True
