import asyncio
import logging
import re
import time

from telethon import TelegramClient, events
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE,
    ADMIN_ID, BOT_TOKEN, EXCLUDED_IDS,
    VOICE_REPLIES_ENABLED,
    TEST_GROUP_ONLY, TEST_GROUP_ID,
)
from negotiation_agent import send_intro_message
import exchange_brain
from contacted_users import is_contacted, add as add_contacted
from voice_agent import voice_to_text, send_voice_message
from natural_replies import get_reply
from agent_memory import log_conversation, detect_customer_tone

logging.basicConfig(level=logging.INFO)

last_message_time = {}

# Tracks first message time + type per user; reset after 5 min idle
_conv_start = {}
_CONV_RESET_IDLE = 300  # seconds

# Keywords that force text-only reply (rules 4)
_PRICE_KEYWORDS = [
    "قیمت", "نرخ", "چنده", "چقدره", "چقدر",
    "rate", "price", "buy price", "sell price",
    "تتر", "usdt",
    "دلار", "usd", "cad", "کانادا",
    "نرخ خرید", "نرخ فروش",
]

# 1 minute: after this, text-started conversations switch to voice
_VOICE_ESCALATE_SECONDS = 60


def is_price_request(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in _PRICE_KEYWORDS)


def detect_currency(text):
    t = text.lower()
    if any(k in t for k in ["تتر", "usdt"]):
        return "USDT"
    if any(k in t for k in ["آمریکا", "امریکا", " usd"]):
        return "USD"
    return "CAD"


def extract_amount(text):
    nums = re.findall(r'\d+', text.replace(',', ''))
    for n in nums:
        v = float(n)
        if 10 <= v <= 50000:
            return v
    return 10


_MARKETPLACE_BLACKLIST = [
    "فروش ماشین", "فروش خودرو", "فروش آپارتمان", "فروش ملک", "فروش موبایل",
    "فروش لپتاپ", "فروش تلویزیون", "فروش یخچال", "فروش لوازم",
    "کرایه", "اجاره", "رهن", "طلافروشی", "car for sale", "apartment",
    "furniture", "appliance", "phone for sale", "laptop for sale",
    "5 dollar", "10 dollar", "20 dollar", "50 dollar",
    "فروش وسایل", "اثاثیه", "مبل", "کاناپه", "فرش",
]

_LEAD_KEYWORDS = [
    # USDT
    "تتر", "usdt", "تتر میخوام", "تتر دارم",
    # USD
    "دلار آمریکا", "دلار امریکا", "usd", "دلار فروش", "دلار خرید",
    "حواله دلار", "cash usd", "usd available",
    # CAD
    "دلار کانادا", "cad", "کانادایی", "cash cad",
    # Generic exchange intent
    "صرافی", "خریدار دلار", "فروش دلار", "ارز", "حواله",
]


def _score_lead(text: str) -> float:
    """Score 0–1 confidence this is a currency exchange lead."""
    t = text.lower()
    # Instantly disqualify marketplace posts
    if any(phrase in t for phrase in _MARKETPLACE_BLACKLIST):
        return 0.0
    # Block small-dollar product prices
    import re as _re
    small_dollar = _re.findall(r"(\d+)\s*(?:dollar|دلار)", t)
    if small_dollar and all(int(n) < 200 for n in small_dollar):
        if not any(k in t for k in ["نرخ", "صرافی", "حواله", "usdt", "تتر", "کانادا"]):
            return 0.0
    score = 0.0
    for kw in _LEAD_KEYWORDS:
        if kw in t:
            score += 0.3
    score = min(score, 1.0)
    return score


def detect_type(text):
    t = text.lower()
    if any(k in t for k in ["فروش", "sell", "میفروشم", "می‌فروشم", "دارم"]):
        return "seller"
    if any(k in t for k in ["خرید", "buy", "میخرم", "می‌خرم", "میخوام", "خریدار"]):
        return "buyer"
    return None


def _resolve_voice(uid: int, customer_sent_voice: bool, price_req: bool) -> bool:
    """
    Decide whether to reply with voice.

    Rule 1: text in  → text out
    Rule 2: voice in → voice out
    Rule 3: text-started convo > 60 s → voice (escalate)
    Rule 4: price request → always text, regardless of above
    """
    if not VOICE_REPLIES_ENABLED:
        return False

    # Rule 4 always wins
    if price_req:
        return False

    now = time.time()
    start = _conv_start.get(uid)

    # Reset stale conversation tracking
    if start and (now - last_message_time.get(uid, now)) > _CONV_RESET_IDLE:
        _conv_start.pop(uid, None)
        start = None

    # Record first message of this conversation
    if start is None:
        _conv_start[uid] = {"time": now, "type": "voice" if customer_sent_voice else "text"}
        start = _conv_start[uid]

    # Rule 2: voice in → voice out
    if customer_sent_voice:
        return True

    # Rule 3: text-started convo older than 60 s → escalate to voice
    if start["type"] == "text" and (now - start["time"]) > _VOICE_ESCALATE_SECONDS:
        return True

    # Rule 1: text in → text out
    return False


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
    """Log turn to agent_learning_log and send admin a feedback prompt."""
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
        logging.warning("Admin notify error: %s", e)


async def _handle_message(client, uid, text, customer_sent_voice=False, sender_name=""):
    """Core message handler — delegates all logic to exchange_brain."""
    last_message_time[uid] = time.time()
    reply, use_voice = await exchange_brain.process(
        uid, text, sender_name=sender_name, is_voice_input=customer_sent_voice
    )
    return reply, use_voice


async def run():
    client = TelegramClient('exchange_agent', TELETHON_API_ID, TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)

    if TEST_GROUP_ONLY:
        print(f"⚠️  TEST_GROUP_ONLY=true — فقط گروه تست ({TEST_GROUP_ID}) فعال است")
        print("   پیام‌های خصوصی و سایر گروه‌ها نادیده گرفته می‌شوند")
    print("✅ Scanner running...")

    # ── Debug: log ALL messages including outgoing (temporary) ───────
    @client.on(events.NewMessage)
    async def debug_all(event):
        print(f"[DEBUG] out={event.out} chat_id={event.chat_id} text={repr((event.text or '')[:40])}")

    # ── Test-group conversation handler ──────────────────────────
    @client.on(events.NewMessage(chats=[TEST_GROUP_ID], incoming=True))
    async def test_group_handler(event):
        if event.out:
            return

        sender = await event.get_sender()
        uid = sender.id

        if uid in EXCLUDED_IDS:
            return

        customer_sent_voice = False

        if event.message.voice:
            customer_sent_voice = True
            audio = await event.download_media(bytes)
            text  = await voice_to_text(audio)
            if not text:
                await event.respond("نشنیدم، دوباره بگو.")
                return
        else:
            text = event.message.message or ""

        if not text or len(text) < 2:
            return

        sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or ""
        reply, use_voice = await _handle_message(client, uid, text, customer_sent_voice, sender_name)

        if use_voice:
            ok = await send_voice_message(client, TEST_GROUP_ID, reply)
            if not ok:
                await event.respond(reply)
        else:
            await event.respond(reply)

        channel = "voice" if customer_sent_voice else "text"
        await _notify_admin_reply(uid, sender_name, text, reply, channel)

    # ── Production group scanner (outreach to new leads) ─────────
    @client.on(events.NewMessage)
    async def group_handler(event):
        if TEST_GROUP_ONLY:
            return

        if event.out or event.is_private:
            return

        text = event.message.message or ""
        if len(text) < 3:
            return

        # Score lead confidence — ignore marketplace posts and low-confidence
        lead_score = _score_lead(text)
        if lead_score < 0.3:
            return

        t = detect_type(text)
        if not t:
            return

        sender = await event.get_sender()
        uid = sender.id

        if uid in EXCLUDED_IDS or is_contacted(uid):
            return

        amount = extract_amount(text)
        add_contacted(uid)

        await asyncio.sleep(2)

        reply = await send_intro_message(client, uid, "user", t, amount)
        await client.send_message(uid, reply)

        bot = Bot(token=BOT_TOKEN)
        deal_label = "فروش" if t == "seller" else "خرید"
        await bot.send_message(
            ADMIN_ID,
            f"👤 مشتری جدید: `{uid}` | {amount} CAD | {deal_label} | conf={lead_score:.0%}",
            parse_mode="Markdown"
        )

    # ── Private chat handler ──────────────────────────────────────
    @client.on(events.NewMessage(incoming=True))
    async def private_handler(event):
        if TEST_GROUP_ONLY:
            return

        if event.out or not event.is_private:
            return

        sender = await event.get_sender()
        uid = sender.id

        if uid in EXCLUDED_IDS:
            return

        customer_sent_voice = False

        if event.message.voice:
            customer_sent_voice = True
            audio = await event.download_media(bytes)
            text  = await voice_to_text(audio)
            if not text:
                await event.respond("نشنیدم، دوباره بگو.")
                return
        else:
            text = event.message.message or ""

        if not text:
            return

        sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or ""
        reply, use_voice = await _handle_message(client, uid, text, customer_sent_voice, sender_name)

        if use_voice:
            ok = await send_voice_message(client, uid, reply)
            if not ok:
                await client.send_message(uid, reply)
        else:
            await client.send_message(uid, reply)

        channel = "voice" if customer_sent_voice else "text"
        await _notify_admin_reply(uid, sender_name, text, reply, channel)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(run())
