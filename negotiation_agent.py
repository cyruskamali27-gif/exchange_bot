import asyncio
import logging
from datetime import datetime
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from config import BOT_TOKEN, ADMIN_ID, GEMINI_API_KEY, ADMIN_APPROVAL_THRESHOLD
from price_engine import calculate_customer_price
from database import add_deal
from natural_replies import get_reply
from agent_memory import (
    log_conversation,
    get_or_create_customer_profile,
    update_customer_profile,
    increment_customer_conversations,
    get_best_reply_style,
    detect_customer_tone,
)

logging.basicConfig(level=logging.INFO)

try:
    from google import genai
    _genai_client = genai.Client(api_key=GEMINI_API_KEY)
    AI_AVAILABLE = True
    print("✅ Gemini AI آماده است")
except Exception as e:
    AI_AVAILABLE = False
    print(f"❌ Gemini error: {e}")

conversations = {}

SYSTEM_PROMPT = """
تو سیروس هستی — یه صراف ایرانی که سال‌هاست تو کاناداست.
مثل یه آدم معمولی تهرانی حرف می‌زنی — نه رسمی، نه خیلی شل.
هیچوقت نمی‌گی ربات یا AI هستی.
فارسی محاوره‌ای می‌نویسی — مثل پیام واتساپ به یه آشنا.
حداکثر ۲ جمله کوتاه در هر پیام.
ایموجی خیلی کم — فقط اگه کاملاً لازم بود.
هرگز نگو: "در حال پردازش"، "لطفاً منتظر بمانید"، "کاربر گرامی"، "مشتری عزیز"، "سامانه".
کارت: خرید و فروش دلار کانادا، انتقال پول به ایران، ترکیه، امارات.
پرداخت Interac — معمولاً ۱۵ تا ۳۰ دقیقه.
اگه توافق شد بنویس [CONFIRMED]
اگه مشتری چانه زد بنویس [BARGAINING]
اگه مشتری جواب نداد بنویس [SILENT]
"""


def _build_profile_hint(profile):
    if not profile:
        return ""
    ptype = profile.get("profile_type", "unknown")
    hints = {
        "impatient":      "این مشتری صبر کمی داره — جواب‌ها خیلی کوتاه و سریع.",
        "price_sensitive": "این مشتری روی قیمت حساسه — نرخ رو با اطمینان بگو.",
        "vip":            "این مشتری قدیمیه — یکم گرم‌تر باش.",
        "suspicious":     "این مشتری محتاطه — واضح و شفاف توضیح بده.",
        "serious_buyer":  "این مشتری جدیه و می‌خواد معامله کنه.",
    }
    return hints.get(ptype, "")


def _feedback_keyboard(log_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ پاسخ خوب",    callback_data=f"fb_good_{log_id}"),
            InlineKeyboardButton("❌ پاسخ بد",     callback_data=f"fb_bad_{log_id}"),
            InlineKeyboardButton("🤖 خیلی رسمی",   callback_data=f"fb_robotic_{log_id}"),
        ],
        [
            InlineKeyboardButton("💸 قیمت اشتباه", callback_data=f"fb_wrongprice_{log_id}"),
            InlineKeyboardButton("😠 ناراضی",      callback_data=f"fb_unhappy_{log_id}"),
            InlineKeyboardButton("🤝 معامله شد",   callback_data=f"fb_dealok_{log_id}"),
        ],
    ])


async def generate_response(user_id, user_message, user_type, amount_cad, stage, profile=None):
    price_info = calculate_customer_price("CAD", amount_cad, user_type)
    if price_info and not price_info.get("manager_required"):
        price_text = f"{price_info['price']:,.0f} تومان"
    else:
        price_text = get_reply("error_price_unavailable")

    best_tone = get_best_reply_style(stage)
    profile_hint = _build_profile_hint(profile)

    history = conversations.get(user_id, {}).get("history", [])
    history_text = "\n".join(f"{h['role']}: {h['msg']}" for h in history[-6:])

    prompt = f"""{SYSTEM_PROMPT}
{profile_hint}
نوع معامله: {"فروش دلار به ما" if user_type == "seller" else "خرید دلار از ما"}
مبلغ: {amount_cad} دلار کانادا
نرخ ما: {price_text}
تاریخچه:
{history_text}
پیام جدید: {user_message}
جواب:"""

    if AI_AVAILABLE:
        try:
            resp = _genai_client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            )
            return resp.text.strip(), best_tone
        except Exception as e:
            logging.error(f"Gemini error: {e}")

    return get_reply("customer_buy" if user_type == "buyer" else "customer_sell"), "friendly"


async def process_message(client, user_id, username, user_message, user_type, amount_cad):
    if user_id not in conversations:
        conversations[user_id] = {
            "history": [], "stage": "negotiating",
            "amount": amount_cad, "type": user_type, "username": username
        }
    conv = conversations[user_id]
    conv["history"].append({"role": "مشتری", "msg": user_message})

    profile = get_or_create_customer_profile(user_id, username)
    increment_customer_conversations(user_id)
    customer_tone = detect_customer_tone(user_message)

    if customer_tone == "impatient" and profile.get("profile_type") == "unknown":
        update_customer_profile(user_id, profile_type="impatient")

    response, tone_used = await generate_response(
        user_id, user_message, user_type, amount_cad, conv["stage"], profile
    )

    bot = Bot(token=BOT_TOKEN)
    situation = conv["stage"]

    if "[CONFIRMED]" in response:
        response = response.replace("[CONFIRMED]", "").strip()
        conv["stage"] = "confirmed"
        update_customer_profile(
            user_id,
            profile_type="serious_buyer" if user_type == "buyer" else "serious_seller"
        )
        log_id = log_conversation(
            user_id, user_message, response,
            situation=situation, tone=tone_used, customer_tone=customer_tone
        )
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"✅ توافق شد!\n━━━━━━━━━━━━━━━━━━\n"
                f"👤 @{username}\n"
                f"💰 {amount_cad} CAD\n"
                f"نوع: {'فروش' if user_type == 'seller' else 'خرید'}\n"
                f"⏰ {datetime.now().strftime('%H:%M')}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"پاسخ ایجنت:\n{response}"
            ),
            reply_markup=_feedback_keyboard(log_id)
        )

    elif "[BARGAINING]" in response:
        response = response.replace("[BARGAINING]", "").strip()
        conv["stage"] = "bargaining"
        if profile.get("profile_type") == "unknown":
            update_customer_profile(user_id, profile_type="price_sensitive")
        log_id = log_conversation(
            user_id, user_message, response,
            situation="bargaining", tone=tone_used, customer_tone=customer_tone
        )
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"⚠️ چانه‌زنی!\n━━━━━━━━━━━━━━━━━━\n"
                f"👤 @{username}\n"
                f"💰 {amount_cad} CAD\n"
                f"پیام: {user_message}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"پاسخ ایجنت:\n{response}"
            ),
            reply_markup=_feedback_keyboard(log_id)
        )

    elif "[SILENT]" in response:
        response = response.replace("[SILENT]", "").strip()
        if not response:
            response = get_reply("customer_stopped_replying")
        log_id = log_conversation(
            user_id, user_message, response,
            situation="silent", tone=tone_used, customer_tone=customer_tone
        )

    else:
        log_id = log_conversation(
            user_id, user_message, response,
            situation=situation, tone=tone_used, customer_tone=customer_tone
        )

    conv["history"].append({"role": "سیروس", "msg": response})
    conv["last_log_id"] = log_id
    return response


async def send_intro_message(client, user_id, username, user_type, amount_cad):
    profile = get_or_create_customer_profile(user_id, username)
    is_repeat = profile.get("total_conversations", 0) > 0

    if is_repeat:
        msg = get_reply("greeting_repeat")
    elif user_type == "seller":
        price_info = calculate_customer_price("CAD", amount_cad, "seller")
        if price_info and not price_info.get("manager_required"):
            msg = (
                f"سلام! دیدم دلار داری برای فروش. "
                f"برای {amount_cad} دلار، {price_info['price']:,.0f} تومان می‌دم — Interac. خوبه؟"
            )
        else:
            msg = f"سلام! دیدم دلار داری برای فروش. چقدر می‌خوای بفروشی؟"
    else:
        msg = f"سلام! دیدم دلار کانادا می‌خوای. چه مبلغی لازم داری؟"

    conversations[user_id] = {
        "history": [{"role": "سیروس", "msg": msg}],
        "stage": "intro", "amount": amount_cad,
        "type": user_type, "username": username,
        "last_log_id": None,
    }
    log_conversation(user_id, "[اولین تماس]", msg, situation="greeting", tone="friendly")
    return msg


# ─── Admin Approval (high-value deals) ──────────────────────────────────────

pending_approval = {}


async def request_admin_approval(user_id, username, amount_cad, response_text):
    bot = Bot(token=BOT_TOKEN)
    pending_approval[str(user_id)] = response_text

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ارسال",   callback_data=f"send_{user_id}"),
        InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_{user_id}"),
        InlineKeyboardButton("❌ رد",      callback_data=f"cancel_{user_id}"),
    ]])

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"⚠️ مبلغ بالا — نیاز به تأیید\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 @{username}\n"
            f"💰 {amount_cad:,.0f} CAD\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"پیام ایجنت:\n{response_text}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"تأیید می‌کنید؟"
        ),
        reply_markup=keyboard
    )
