import asyncio
import logging
from datetime import datetime
from telegram import Bot
from config import BOT_TOKEN, ADMIN_ID, GEMINI_API_KEY, MARGIN_CAD
from price_engine import calculate_customer_price
from database import add_deal

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
تو سیروس هستی — یه صراف ایرانی در کانادا.
مثل یه دوست قدیمی حرف می‌زنی — صمیمی، کوتاه، طبیعی.
هیچوقت مثل ربات یا AI حرف نمی‌زنی.
هیچوقت نمی‌گی ربات یا AI هستی.
فارسی محاوره‌ای می‌نویسی — مثل پیام واتساپ.
جملات کوتاه — حداکثر ۳ جمله.
ایموجی کم — حداکثر ۱ تا.
کارت اینه: خرید و فروش دلار کانادا، انتقال پول به ایران، ترکیه، چین.
پرداخت Interac — زمان ۱۵-۳۰ دقیقه.
اگر مشتری موضوع دیگه‌ای پرسید جواب بده مثل آدم، بعد برگرد سر معامله.
اگر توافق شد بنویس [CONFIRMED]
اگر چانه زد بنویس [BARGAINING]
"""

async def generate_response(user_id, user_message, user_type, amount_cad, stage):
    price_info = calculate_customer_price("CAD", amount_cad, user_type)
    if price_info and not price_info.get("manager_required"):
        price_text = f"{price_info['price']:,.0f} تومان"
    else:
        price_text = "الان چک می‌کنم"
    history = conversations.get(user_id, {}).get("history", [])
    history_text = "\n".join([f"{h['role']}: {h['msg']}" for h in history[-6:]])
    prompt = f"""
{SYSTEM_PROMPT}
نوع: {'فروش دلار' if user_type == 'seller' else 'خرید دلار'}
مبلغ: {amount_cad} دلار کانادا
قیمت ما: {price_text}
تاریخچه:\n{history_text}
پیام جدید: {user_message}
جواب بده:
"""
    if AI_AVAILABLE:
        try:
            response = _genai_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            return response.text.strip()
        except Exception as e:
            logging.error(f"Gemini error: {e}")
    return "آره، بگو چی می‌خوای؟"

async def process_message(client, user_id, username, user_message, user_type, amount_cad):
    if user_id not in conversations:
        conversations[user_id] = {"history":[],"stage":"negotiating","amount":amount_cad,"type":user_type,"username":username}
    conv = conversations[user_id]
    conv["history"].append({"role":"مشتری","msg":user_message})
    response = await generate_response(user_id, user_message, user_type, amount_cad, conv["stage"])
    if "[CONFIRMED]" in response:
        response = response.replace("[CONFIRMED]","").strip()
        conv["stage"] = "getting_info"
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(chat_id=ADMIN_ID, text=(
            f"✅ توافق شد!\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 @{username}\n💰 {amount_cad} CAD\n"
            f"نوع: {'فروش' if user_type=='seller' else 'خرید'}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        ))
    elif "[BARGAINING]" in response:
        response = response.replace("[BARGAINING]","").strip()
        conv["stage"] = "bargaining"
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(chat_id=ADMIN_ID, text=(
            f"⚠️ چانه‌زنی!\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 @{username}\n💰 {amount_cad} CAD\n"
            f"پیام: {user_message}"
        ))
    conv["history"].append({"role":"سیروس","msg":response})
    return response

async def send_intro_message(client, user_id, username, user_type, amount_cad):
    price_info = calculate_customer_price(amount_cad)
    if user_type == "seller" and price_info["success"]:
        msg = f"سلام {username}، دیدم دلار داری برای فروش. برای {amount_cad} دلار {price_info['net_toman']:,.0f} تومان می‌دم — Interac. علاقه داری؟"
    else:
        msg = f"سلام {username}! دیدم دلار کانادا می‌خوای. چه مبلغی لازم داری؟"
    conversations[user_id] = {"history":[{"role":"سیروس","msg":msg}],"stage":"intro","amount":amount_cad,"type":user_type,"username":username}
    return msg

# پیام‌های در انتظار تأیید ادمین
pending_approval = {}

async def request_admin_approval(user_id, username, amount_cad, response_text):
    """درخواست تأیید از ادمین برای مبالغ بالا"""
    from config import ADMIN_APPROVAL_THRESHOLD, BOT_TOKEN, ADMIN_ID
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    
    bot = Bot(token=BOT_TOKEN)
    pending_approval[f"{user_id}"] = response_text
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ ارسال", callback_data=f"send_{user_id}"),
            InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_{user_id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"cancel_{user_id}")
        ]
    ])
    
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
