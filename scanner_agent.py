import asyncio
import hashlib
import logging
import re
import time

from telethon import TelegramClient, events
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE,
    ADMIN_ID, BOT_TOKEN, EXCLUDED_IDS,
    TEST_GROUP_ONLY, TEST_GROUP_ID, ALLOWED_TELEGRAM_CHAT,
)
import telegram_guard
from negotiation_agent import send_intro_message
import exchange_brain
from contacted_users import is_contacted, add as add_contacted
from voice_agent import voice_to_text, send_convai_audio
import elevenlabs_agent
from natural_replies import get_reply
from agent_memory import log_conversation, detect_customer_tone
import conversation_qc_agent as _qc
from qc_command_router import register_qc_handlers

log = logging.getLogger("scanner")
logging.basicConfig(level=logging.INFO)

last_message_time = {}

# Own Telethon account ID — populated at startup, used to block self-replies
_self_id: int | None = None

# Anti-loop: MD5(reply[:80]) → timestamp, 5-minute window
_recent_bot_replies: dict[str, float] = {}
_LOOP_WINDOW_S = 300

# Text patterns that only appear in bot/admin messages — drop immediately
_BOT_TEXT_PATTERNS = [
    "/start", "پنل مدیریت صرافی", "برای دیدن پنل",
    "گزارش امروز", "پروفایل مشتریان",
]

# Tracks first message time + type per user; reset after 5 min idle
_conv_start = {}
_CONV_RESET_IDLE = 300

_PRICE_KEYWORDS = [
    "قیمت", "نرخ", "چنده", "چقدره", "چقدر",
    "rate", "price", "buy price", "sell price",
    "تتر", "usdt",
    "دلار", "usd", "cad", "کانادا",
    "نرخ خرید", "نرخ فروش",
]

# Keywords that force text-only routing for voice messages
_VOICE_PRICE_KEYWORDS = [
    "نرخ", "قیمت", "دلار", "تتر", "usdt", "usd", "cad",
    "خرید", "فروش", "exchange rate", "price", "rate", "dollar", "tether",
    "چنده", "چقدره", "چقدر", "کانادا",
]


def _is_rate_request(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _VOICE_PRICE_KEYWORDS)

_MARKETPLACE_BLACKLIST = [
    "فروش ماشین", "فروش خودرو", "فروش آپارتمان", "فروش ملک", "فروش موبایل",
    "فروش لپتاپ", "فروش تلویزیون", "فروش یخچال", "فروش لوازم",
    "کرایه", "اجاره", "رهن", "طلافروشی", "car for sale", "apartment",
    "furniture", "appliance", "phone for sale", "laptop for sale",
    "5 dollar", "10 dollar", "20 dollar", "50 dollar",
    "فروش وسایل", "اثاثیه", "مبل", "کاناپه", "فرش",
]

_LEAD_KEYWORDS = [
    "تتر", "usdt", "تتر میخوام", "تتر دارم",
    "دلار آمریکا", "دلار امریکا", "usd", "دلار فروش", "دلار خرید",
    "حواله دلار", "cash usd", "usd available",
    "دلار کانادا", "cad", "کانادایی", "cash cad",
    "صرافی", "خریدار دلار", "فروش دلار", "ارز", "حواله",
]


# ── Self / loop protection ────────────────────────────────────────

def _is_bot_text(text: str) -> bool:
    return any(p in (text or "") for p in _BOT_TEXT_PATTERNS)


def _is_loop_reply(text: str) -> bool:
    """True if we sent a very similar message in the last 5 minutes."""
    key = hashlib.md5(text[:80].encode("utf-8", errors="replace")).hexdigest()
    now = time.time()
    expired = [k for k, t in list(_recent_bot_replies.items()) if now - t > _LOOP_WINDOW_S]
    for k in expired:
        del _recent_bot_replies[k]
    return key in _recent_bot_replies


def _record_bot_reply(text: str):
    """Store reply hash so _is_loop_reply can catch it if it re-enters."""
    key = hashlib.md5(text[:80].encode("utf-8", errors="replace")).hexdigest()
    _recent_bot_replies[key] = time.time()


def _should_ignore(uid: int, text: str, event) -> str | None:
    """
    Return a reason string if the message should be silently dropped, else None.
    Checked before any processing in every handler.
    """
    if event.out:
        return "outgoing"
    if _self_id and uid == _self_id:
        return "self_id"
    if uid in EXCLUDED_IDS:
        return "excluded"
    if getattr(event.message, "via_bot_id", None):
        return "via_bot"
    fwd = getattr(event.message, "forward", None)
    if fwd:
        fwd_from = getattr(fwd, "from_id", None)
        if fwd_from and _self_id:
            fwd_uid = getattr(fwd_from, "user_id", None)
            if fwd_uid == _self_id:
                return "forwarded_self"
    if _is_bot_text(text):
        return "bot_text_pattern"
    if text and _is_loop_reply(text):
        return "loop_detected"
    return None


# ── Lead scoring ─────────────────────────────────────────────────

def _score_lead(text: str) -> float:
    t = text.lower()
    if any(phrase in t for phrase in _MARKETPLACE_BLACKLIST):
        return 0.0
    small_dollar = re.findall(r"(\d+)\s*(?:dollar|دلار)", t)
    if small_dollar and all(int(n) < 200 for n in small_dollar):
        if not any(k in t for k in ["نرخ", "صرافی", "حواله", "usdt", "تتر", "کانادا"]):
            return 0.0
    score = 0.0
    for kw in _LEAD_KEYWORDS:
        if kw in t:
            score += 0.3
    return min(score, 1.0)


def detect_type(text):
    t = text.lower()
    if any(k in t for k in ["فروش", "sell", "میفروشم", "می‌فروشم", "دارم"]):
        return "seller"
    if any(k in t for k in ["خرید", "buy", "میخرم", "می‌خرم", "میخوام", "خریدار"]):
        return "buyer"
    return None


def extract_amount(text):
    nums = re.findall(r'\d+', text.replace(',', ''))
    for n in nums:
        v = float(n)
        if 10 <= v <= 50000:
            return v
    return 10


# ── Admin feedback ───────────────────────────────────────────────

_feedback_kb_cache: dict[int, InlineKeyboardMarkup] = {}


def _feedback_keyboard(log_id: int) -> InlineKeyboardMarkup:
    if log_id not in _feedback_kb_cache:
        _feedback_kb_cache[log_id] = InlineKeyboardMarkup([[
            InlineKeyboardButton("👍",      callback_data=f"fb_good_{log_id}"),
            InlineKeyboardButton("👎",      callback_data=f"fb_bad_{log_id}"),
            InlineKeyboardButton("✏️ تصحیح", callback_data=f"fb_correct_{log_id}"),
        ]])
    return _feedback_kb_cache[log_id]


async def _notify_admin_reply(uid: int, name: str, text: str, reply: str, channel: str):
    try:
        log_id = log_conversation(uid, text, reply, channel=channel)
        bot = Bot(token=BOT_TOKEN)
        label = name or str(uid)
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"💬 {label}\n"
                f"📥 {text[:80]}\n"
                f"🤖 {reply[:100]}"
            ),
            reply_markup=_feedback_keyboard(log_id),
        )
    except Exception as e:
        log.warning("[SCANNER] Admin notify error: %s", e)


async def _handle_message(client, uid, text, customer_sent_voice=False, sender_name=""):
    """Delegate to exchange_brain. Returns (reply, use_voice)."""
    last_message_time[uid] = time.time()
    return await exchange_brain.process(
        uid, text, sender_name=sender_name, is_voice_input=customer_sent_voice
    )


# ── QC: ConvAI voice reply with quality check ────────────────────────────────

async def _convai_with_qc(uid: int, text: str) -> tuple[str, bytes]:
    """
    Call ElevenLabs ConvAI, then run QC on the reply text.
    If QC rewrites the text, return empty audio (caller sends text instead).
    """
    reply_text, audio_bytes = await elevenlabs_agent.chat_with_audio(uid, text)

    status = _qc.get_qc_status(uid)
    if status["enabled"]:
        approved = await _qc.inspect_reply(uid, reply_text)
        if approved != reply_text:
            # Text was rewritten — audio no longer matches; force text reply
            log.info("[QC] uid=%s ConvAI text rewritten — falling back to text", uid)
            return approved, b""
        return reply_text, audio_bytes
    else:
        # QC off: still update state tracking
        await _qc.inspect_reply(uid, reply_text)
        return reply_text, audio_bytes


# ── Admin QC commands ─────────────────────────────────────────────────────────

async def _handle_qc_command(text: str) -> str | None:
    """
    Handle /conv_qc_* commands from admin.
    Returns reply string or None if not a recognised command.
    """
    cmd = text.strip().lower().split()[0]

    if cmd == "/conv_qc_on":
        _qc.enable_qc()
        return "QC روشن شد — همه پاسخ‌ها بررسی می‌شوند."

    if cmd == "/conv_qc_off":
        _qc.disable_qc()
        return "QC خاموش شد — پاسخ‌ها بدون بررسی ارسال می‌شوند."

    if cmd == "/conv_qc_status":
        st = _qc.get_qc_status()
        enabled_fa = "روشن ✅" if st["enabled"] else "خاموش ❌"
        return (
            f"وضعیت QC: {enabled_fa}\n"
            f"آستانه بازنویسی: {st['threshold']:.0%}\n"
            f"کارکرد: بررسی تکرار + روباتیک + لهجه + فارسی طبیعی"
        )

    if cmd == "/conv_last_reply":
        reply = _qc.get_last_reply()
        if reply:
            return f"آخرین پاسخ ارسال‌شده:\n\n{reply}"
        return "هنوز پاسخی در این سشن ثبت نشده."

    if cmd == "/conv_last_qc":
        result = _qc.get_last_qc()
        if not result:
            return "نتیجه QC‌ای ثبت نشده."
        sc = result["scores"]
        issues = result.get("issues") or []
        rewritten_fa = "بله ✏️" if result["was_rewritten"] else "خیر"
        return (
            f"آخرین بررسی QC:\n"
            f"امتیاز کلی: {sc['overall']:.2f}/1.00\n"
            f"انسانی: {sc['human']:.2f}   "
            f"تکرار: {sc['repetition']:.2f}\n"
            f"روانی: {sc['fluency']:.2f}   "
            f"فارسی طبیعی: {sc['persian_naturalness']:.2f}\n"
            f"مشکلات: {', '.join(issues) or 'هیچ'}\n"
            f"بازنویسی شد: {rewritten_fa}"
        )

    if cmd == "/conv_rewrite_last":
        new_reply = await _qc.rewrite_last()
        if new_reply:
            return f"پاسخ بازنویسی‌شده:\n\n{new_reply}"
        return "پاسخی برای بازنویسی نیست."

    return None


# ── Main ─────────────────────────────────────────────────────────

async def run():
    global _self_id

    client = TelegramClient('exchange_agent', TELETHON_API_ID, TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)

    me = await client.get_me()
    _self_id = me.id
    log.info("[SCANNER] Started — self_id=%s (self-messages will be blocked)", _self_id)

    # ── Security: resolve allowed group and init guard ────────────
    try:
        allowed_entity = await client.get_entity(ALLOWED_TELEGRAM_CHAT)
        _allowed_gid = allowed_entity.id
        telegram_guard.init(_allowed_gid, ADMIN_ID)
        log.info("[GUARD] Resolved %s → chat_id=%s", ALLOWED_TELEGRAM_CHAT, _allowed_gid)
    except Exception as e:
        log.error("[GUARD] FATAL: could not resolve %s: %s — bot will not send to any group",
                  ALLOWED_TELEGRAM_CHAT, e)
        telegram_guard.init(-1, ADMIN_ID)

    await register_qc_handlers(client)

    if TEST_GROUP_ONLY:
        print(f"⚠️  TEST_GROUP_ONLY=true — گروه تست ({TEST_GROUP_ID}) فعال است")
        print("   سایر گروه‌های تولید نادیده گرفته می‌شوند — پیام خصوصی همیشه پردازش می‌شود")
    print("✅ Scanner running...")

    # ── Debug: log ALL messages (including outgoing) ─────────────
    @client.on(events.NewMessage)
    async def debug_all(event):
        print(f"[DEBUG] out={event.out} chat_id={event.chat_id} text={repr((event.text or '')[:40])}")

    # ── Test-group: scan-only — replies go to PRIVATE chat ───────
    @client.on(events.NewMessage(chats=[TEST_GROUP_ID], incoming=True))
    async def test_group_handler(event):
        if event.out:
            return

        sender = await event.get_sender()
        uid = sender.id
        telegram_guard.register_customer(uid)  # allow private replies to this user
        customer_sent_voice = False
        text = ""

        if event.message.voice:
            customer_sent_voice = True
            log.info("[VOICE_STEP] uid=%s step=download_start", uid)
            try:
                audio = await event.download_media(bytes)
                log.info("[VOICE_STEP] uid=%s step=download_ok bytes=%d", uid, len(audio) if audio else 0)
            except Exception as e:
                log.error("[VOICE_STEP] uid=%s step=download_FAIL err=%s", uid, e)
                return

            log.info("[VOICE_STEP] uid=%s step=stt_start", uid)
            try:
                text = await voice_to_text(audio)
                log.info("[VOICE_STEP] uid=%s step=stt_ok text=%r", uid, (text or "")[:80])
            except Exception as e:
                log.error("[VOICE_STEP] uid=%s step=stt_FAIL err=%s", uid, e)
                return

            if not text:
                log.warning("[VOICE_STEP] uid=%s step=stt_empty — no transcription", uid)
                return
            log.info("[VOICE_ROUTE] transcribed_text=%r uid=%s", text[:100], uid)
        else:
            text = event.message.message or ""

        reason = _should_ignore(uid, text, event)
        if reason:
            log.info("[SELF_MESSAGE_BLOCKED] test_group uid=%s reason=%s", uid, reason)
            return

        if not text or len(text) < 2:
            return

        sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or ""
        log.info("[GROUP_SCAN_ONLY] uid=%s — processing then replying privately", uid)

        # Groups are scan-only — always reply via private message
        sent_ok = False
        if customer_sent_voice:
            log.info("[ROUTE] uid=%s Incoming message type: voice", uid)
            if _is_rate_request(text):
                log.info("[ROUTE] uid=%s Rate intent detected: text-only", uid)
                try:
                    reply, _ = await _handle_message(client, uid, text, True, sender_name)
                except Exception as e:
                    log.error("[BRAIN] uid=%s failed: %s", uid, e)
                    reply = "مشکلی پیش اومد، دوباره پیام بده."
                _record_bot_reply(reply)
                if telegram_guard.check_send(uid, "test_group_voice_text"):
                    try:
                        await client.send_message(uid, reply)
                        sent_ok = True
                    except Exception as e:
                        log.error("[SEND_ERROR] uid=%s: %s", uid, e)
                await _notify_admin_reply(uid, sender_name, text, reply, "voice_text")
            else:
                log.info("[ROUTE] uid=%s Voice input detected: ConvAI voice reply", uid)
                log.info("[ROUTE] uid=%s No greeting injected by Telegram code", uid)
                try:
                    reply_text, audio_bytes = await _convai_with_qc(uid, text)
                except Exception as e:
                    log.error("[CONVAI] uid=%s failed: %s", uid, e)
                    reply_text, audio_bytes = "مشکلی پیش اومد، دوباره پیام بده.", b""

                _record_bot_reply(reply_text)
                if telegram_guard.check_send(uid, "test_group_voice"):
                    try:
                        if audio_bytes:
                            ok = await send_convai_audio(client, uid, audio_bytes)
                            sent_ok = ok
                            if not ok:
                                await client.send_message(uid, reply_text)
                                sent_ok = True
                        else:
                            await client.send_message(uid, reply_text)
                            sent_ok = True
                    except Exception as e:
                        log.error("[SEND_ERROR] uid=%s: %s", uid, e)
                await _notify_admin_reply(uid, sender_name, text, reply_text, "voice")
        else:
            log.info("[ROUTE] uid=%s Incoming message type: text", uid)
            log.info("[ROUTE] uid=%s Text input detected: text reply", uid)
            try:
                reply, _ = await _handle_message(client, uid, text, False, sender_name)
            except Exception as e:
                log.error("[BRAIN] uid=%s failed: %s", uid, e)
                reply = "مشکلی پیش اومد، دوباره پیام بده."
            _record_bot_reply(reply)
            if telegram_guard.check_send(uid, "test_group_text"):
                try:
                    await client.send_message(uid, reply)
                    sent_ok = True
                except Exception as e:
                    log.error("[SEND_ERROR] uid=%s: %s", uid, e)
            await _notify_admin_reply(uid, sender_name, text, reply, "text")

        log.info("[PRIVATE_REPLY_SENT] uid=%s sent_ok=%s", uid, sent_ok)

    # ── Production groups: lead detection + private outreach ─────
    @client.on(events.NewMessage)
    async def group_handler(event):
        if TEST_GROUP_ONLY:
            return
        if event.out or event.is_private:
            return

        # Only operate in the authorised group
        if not telegram_guard.is_allowed_group(event.chat_id):
            return

        sender = await event.get_sender()
        if not sender:
            return
        uid = sender.id
        text = event.message.message or ""

        reason = _should_ignore(uid, text, event)
        if reason:
            log.info("[SELF_MESSAGE_BLOCKED] group uid=%s reason=%s", uid, reason)
            return

        if len(text) < 3:
            return

        lead_score = _score_lead(text)
        if lead_score < 0.3:
            return

        t = detect_type(text)
        if not t:
            return

        if uid in EXCLUDED_IDS or is_contacted(uid):
            return

        amount = extract_amount(text)
        add_contacted(uid)
        telegram_guard.register_customer(uid)
        await asyncio.sleep(2)

        reply = await send_intro_message(client, uid, "user", t, amount)
        if not telegram_guard.check_send(uid, "group_lead_intro"):
            log.warning("[GUARD] Blocked lead intro to uid=%s", uid)
            return
        await client.send_message(uid, reply)
        _record_bot_reply(reply)
        log.info("[PRIVATE_LEAD_STARTED] uid=%s amount=%s type=%s conf=%.0f%%",
                 uid, amount, t, lead_score * 100)

        bot = Bot(token=BOT_TOKEN)
        deal_label = "فروش" if t == "seller" else "خرید"
        await bot.send_message(
            ADMIN_ID,
            f"👤 مشتری جدید: `{uid}` | {amount} CAD | {deal_label} | conf={lead_score:.0%}",
            parse_mode="Markdown"
        )

    # ── Private chat: full conversation handler ──────────────────
    @client.on(events.NewMessage(incoming=True))
    async def private_handler(event):
        # TEST_GROUP_ONLY only blocks production group scanning,
        # NOT private replies — users who were redirected privately must be served.
        if event.out or not event.is_private:
            return

        sender = await event.get_sender()
        uid = sender.id

        # Only handle DMs from ADMIN or customers seen in @cyrusGlobalExchange
        if not telegram_guard.is_allowed_target(uid):
            log.info("[GUARD] Blocked private DM from unregistered uid=%s", uid)
            return

        customer_sent_voice = False
        text = ""

        # ── Admin QC commands (text-only, no voice) ───────────────
        raw_text = event.message.message or ""
        if uid == ADMIN_ID and raw_text.startswith("/conv_"):
            result = await _handle_qc_command(raw_text)
            if result is not None:
                try:
                    await client.send_message(uid, result)
                except Exception as e:
                    log.error("[QC_CMD] send failed: %s", e)
                return

        if event.message.voice:
            customer_sent_voice = True
            log.info("[VOICE_STEP] uid=%s step=telegram_voice_received", uid)

            # ── Download ─────────────────────────────────────────────
            log.info("[VOICE_STEP] uid=%s step=download_start", uid)
            try:
                audio = await event.download_media(bytes)
                if not audio:
                    raise ValueError("Empty audio bytes")
                log.info("[VOICE_STEP] uid=%s step=download_ok bytes=%d", uid, len(audio))
            except Exception as e:
                log.error("[VOICE_STEP] uid=%s step=download_FAIL err=%s", uid, e)
                try:
                    await event.respond("مشکل در دریافت فایل صوتی، دوباره بفرست.")
                except Exception:
                    pass
                return

            # ── STT ──────────────────────────────────────────────────
            log.info("[VOICE_STEP] uid=%s step=stt_start", uid)
            try:
                text = await voice_to_text(audio)
                log.info("[VOICE_STEP] uid=%s step=stt_ok text=%r", uid, (text or "")[:80])
            except Exception as e:
                log.error("[VOICE_STEP] uid=%s step=stt_FAIL err=%s", uid, e)
                try:
                    await event.respond("نشنیدم، دوباره بگو.")
                except Exception:
                    pass
                return

            if not text:
                log.warning("[VOICE_STEP] uid=%s step=stt_empty — STT returned nothing", uid)
                try:
                    await event.respond("نشنیدم، دوباره بگو.")
                except Exception:
                    pass
                return

            log.info("[VOICE_ROUTE] transcribed_text=%r uid=%s", text[:100], uid)

        else:
            text = event.message.message or ""

        reason = _should_ignore(uid, text, event)
        if reason:
            log.info("[SELF_MESSAGE_BLOCKED] private uid=%s reason=%s", uid, reason)
            return

        if not text:
            return

        sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or ""

        sent_ok = False
        if customer_sent_voice:
            log.info("[ROUTE] uid=%s Incoming message type: voice", uid)
            if _is_rate_request(text):
                log.info("[ROUTE] uid=%s Rate intent detected: text-only", uid)
                try:
                    reply, _ = await _handle_message(client, uid, text, True, sender_name)
                except Exception as e:
                    log.error("[BRAIN] uid=%s failed: %s", uid, e)
                    reply = "مشکلی پیش اومد، دوباره پیام بده."
                _record_bot_reply(reply)
                try:
                    await client.send_message(uid, reply)
                    sent_ok = True
                except Exception as e:
                    log.error("[SEND_ERROR] uid=%s: %s", uid, e)
                await _notify_admin_reply(uid, sender_name, text, reply, "voice_text")

            else:
                log.info("[ROUTE] uid=%s Voice input detected: ConvAI voice reply", uid)
                log.info("[ROUTE] uid=%s No greeting injected by Telegram code", uid)
                log.info("[VOICE_STEP] uid=%s step=convai_start text=%r", uid, text[:60])
                try:
                    reply_text, audio_bytes = await _convai_with_qc(uid, text)
                    log.info("[VOICE_STEP] uid=%s step=convai_ok reply=%r audio=%d bytes",
                             uid, reply_text[:80], len(audio_bytes))
                except Exception as e:
                    log.error("[VOICE_STEP] uid=%s step=convai_FAIL err=%s", uid, e)
                    reply_text, audio_bytes = "مشکلی پیش اومد، دوباره پیام بده.", b""

                _record_bot_reply(reply_text)
                try:
                    if audio_bytes:
                        ok = await send_convai_audio(client, uid, audio_bytes)
                        sent_ok = ok
                        if not ok:
                            log.warning("[VOICE_STEP] uid=%s audio send failed — text fallback", uid)
                            await client.send_message(uid, reply_text)
                            sent_ok = True
                    else:
                        log.warning("[VOICE_STEP] uid=%s no audio from ConvAI — text fallback", uid)
                        await client.send_message(uid, reply_text)
                        sent_ok = True
                except Exception as e:
                    log.error("[SEND_ERROR] uid=%s: %s", uid, e)
                    try:
                        await client.send_message(uid, reply_text)
                        sent_ok = True
                    except Exception:
                        pass

                await _notify_admin_reply(uid, sender_name, text, reply_text, "voice")

        else:
            log.info("[ROUTE] uid=%s Incoming message type: text", uid)
            log.info("[ROUTE] uid=%s Text input detected: text reply", uid)
            try:
                reply, _ = await _handle_message(client, uid, text, False, sender_name)
            except Exception as e:
                log.error("[BRAIN] uid=%s failed: %s", uid, e)
                reply = "مشکلی پیش اومد، دوباره پیام بده."

            _record_bot_reply(reply)
            try:
                await client.send_message(uid, reply)
                sent_ok = True
            except Exception as e:
                log.error("[SEND_ERROR] uid=%s: %s", uid, e)

            await _notify_admin_reply(uid, sender_name, text, reply, "text")

        log.info("[PRIVATE_REPLY_SENT] uid=%s sent_ok=%s", uid, sent_ok)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(run())
