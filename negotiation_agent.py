import asyncio
import logging
from datetime import datetime
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from config import BOT_TOKEN, ADMIN_ID, GEMINI_API_KEY, ADMIN_APPROVAL_THRESHOLD
from price_engine import calculate_customer_price, get_margin_by_amount, should_send_to_manager
from database import add_deal
from natural_replies import get_reply, reply_delay, should_send_ack
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
تو سیروس هستی — صراف ایرانی تورنتو. سرت شلوغه.

━━ قوانین سخت — هیچوقت نقض نمیشن ━━

سبک نوشتن:
- فارسی محاوره‌ای تهرانی مدرن — مثل پیام واتساپ یه آدم واقعی
- حداکثر ۱ یا ۲ جمله کوتاه در هر پیام
- اگه مشتری هنوز همه چیز رو نگفته، فقط یه سوال بپرس
- یه پیام بده، منتظر جواب مشتری بمون — چند پیام پشت‌سرهم نفرست
- ایموجی نفرست

جملات کاملاً ممنوع — هرگز استفاده نکن:
- "یه لحظه" / "یه لحظه چک می‌کنم" / "باشه چک می‌کنم"
- "چک می‌کنم براتون" / "در حال بررسی"
- "صبر کن" / "لطفاً منتظر باشید"
- "مشتری گرامی" / "کاربر گرامی" / "مشتری عزیز"
- "خوشحالم دوباره پیام دادید" / "احوال شریف" / "در خدمتم بفرمایید"
- "در حال پردازش" / "سامانه" / "ربات"

تعارف ممنوع:
- هرگز با احوال‌پرسی شروع نکن
- اولین جواب فقط "سلام" یا "سلام، بفرمایید"

قیمت:
- هرگز عدد اختراع نکن
- اگه نرخ در دسترس نیست: بگو "نرخ الان تابلوئه، چون قیمت‌ها لحظه‌ای هستن"
- اگه نرخ داری: مستقیم بگو، بدون مقدمه

کارت:
خرید و فروش دلار کانادا (CAD)، دلار آمریکا (USD)، تتر (USDT)
انتقال به ایران، ترکیه، امارات — پرداخت Interac (۱۵–۳۰ دقیقه)

نشانه‌ها (فقط آخر پیام اگه لازم بود):
[CONFIRMED] — توافق شد
[BARGAINING] — مشتری چانه زد
[SILENT] — مشتری جواب نداد
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


def _detect_currency_from_message(text):
    t = text.lower()
    if any(k in t for k in ["تتر", "usdt"]):
        return "USDT"
    if any(k in t for k in ["آمریکا", "امریکا", " usd"]):
        return "USD"
    return "CAD"


async def generate_response(user_id, user_message, user_type, amount_cad, stage, profile=None):
    # Manager handoff — no auto-price for large amounts
    if should_send_to_manager(amount_cad):
        return get_reply("manager_required"), "friendly"

    currency = _detect_currency_from_message(user_message)
    price_info = calculate_customer_price(currency, amount_cad, user_type)

    if price_info and not price_info.get("manager_required"):
        price_text   = f"{price_info['price']:,.0f} تومان"
        margin       = price_info.get("margin", 0)
        price_line   = (
            f"نرخ ما: {price_text}"
            + (f" | سقف چانه‌زنی: {margin} تومان (به مشتری نگو)" if margin else "")
        )
    else:
        # No live data — never invent a price
        price_line = (
            "قیمت لحظه‌ای الان در دسترس نیست. "
            "به مشتری بگو نرخ الان تابلوئه و چند دقیقه دیگه دوباره پیام بده. "
            "هیچ عددی اختراع نکن."
        )

    best_tone    = get_best_reply_style(stage)
    profile_hint = _build_profile_hint(profile)

    history      = conversations.get(user_id, {}).get("history", [])
    history_text = "\n".join(f"{h['role']}: {h['msg']}" for h in history[-6:])

    cur_label = {"USDT": "تتر", "USD": "دلار آمریکا", "CAD": "دلار کانادا"}.get(currency, currency)
    prompt = f"""{SYSTEM_PROMPT}
{profile_hint}
نوع معامله: {"فروش " + cur_label + " به ما" if user_type == "seller" else "خرید " + cur_label + " از ما"}
مبلغ: {amount_cad} {cur_label}
{price_line}
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

    # natural pause — feels human, not instant-bot
    await asyncio.sleep(reply_delay(customer_tone))

    # occasionally send a short ack before the real reply
    if should_send_ack(customer_tone):
        try:
            await client.send_message(user_id, get_reply("listening"))
            await asyncio.sleep(1.2)
        except Exception:
            pass

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
            msg = f"سلام. دیدم می‌فروشی — {amount_cad} دلار؟ {price_info['price']:,.0f} تومان می‌دم."
        else:
            msg = "سلام. دیدم دلار می‌فروشی. چقدر؟"
    else:
        msg = "سلام. دیدم دلار می‌خوای. چند تا؟"

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
