import asyncio
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

from config import BOT_TOKEN, ADMIN_ID, IRAN_AGENT_ID, MARGIN_CAD
from database import init_db, get_today_stats, get_pending_deals, get_usdt_balance, update_deal_status, get_deal
from price_engine import calculate_rate, get_market_base
from agent_memory import (
    save_admin_feedback, get_last_log_id,
    get_daily_report, get_all_customer_profiles,
    get_or_create_customer_profile, update_customer_profile,
)

logging.basicConfig(level=logging.INFO)

FEEDBACK_LABELS = {
    "good":       "✅ پاسخ خوب",
    "bad":        "❌ پاسخ بد",
    "robotic":    "🤖 خیلی رسمی",
    "wrongprice": "💸 قیمت اشتباه",
    "unhappy":    "😠 مشتری ناراضی",
    "dealok":     "🤝 معامله موفق",
    "dealfail":   "❌ معامله ناموفق",
}

PROFILE_LABELS = {
    "unknown":       "ناشناخته",
    "calm":          "آروم",
    "impatient":     "بی‌حوصله",
    "price_sensitive": "قیمت‌حساس",
    "serious_buyer": "خریدار جدی",
    "suspicious":    "محتاط",
    "vip":           "VIP",
    "repeat":        "قدیمی",
}


def is_admin(user_id):
    return user_id == ADMIN_ID


# ─── Main Panel ──────────────────────────────────────────────────────────────

async def show_main_panel(update, context):
    stats = get_today_stats()
    balance = get_usdt_balance()
    pending = get_pending_deals()
    report = get_daily_report()

    text = (
        f"🏦 پنل مدیریت صرافی\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 موجودی تتر: {balance:,.0f} USDT\n"
        f"⏳ در انتظار: {len(pending)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 امروز:\n"
        f"  معاملات: {stats['count']}\n"
        f"  سود: {stats['profit']:,.0f} CAD\n"
        f"  گفتگوها: {report['total_conversations']}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [
            InlineKeyboardButton("⏳ معاملات در انتظار", callback_data="pending_deals"),
            InlineKeyboardButton("💱 قیمت لحظه‌ای",     callback_data="live_price"),
        ],
        [
            InlineKeyboardButton("📈 گزارش یادگیری",    callback_data="learning_report"),
            InlineKeyboardButton("👥 پروفایل مشتریان",  callback_data="customer_profiles"),
        ],
        [
            InlineKeyboardButton("📊 گزارش امروز",      callback_data="today_report"),
        ],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


# ─── Commands ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ دسترسی ندارید")
        return
    await show_main_panel(update, context)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await _send_learning_report(update.message.reply_text)


# ─── Pending Deals ───────────────────────────────────────────────────────────

async def show_pending_deals(update, context):
    pending = get_pending_deals()
    if not pending:
        keyboard = [[InlineKeyboardButton("🔙 برگشت", callback_data="main_panel")]]
        await update.callback_query.edit_message_text(
            "✅ هیچ معامله‌ای در انتظار نیست",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    text = f"⏳ معاملات در انتظار ({len(pending)})\n━━━━━━━━━━━━━━━━━━\n"
    keyboard = []
    for deal in pending[:5]:
        emoji = "📤" if deal['type'] == 'sell' else "📥"
        text += f"\n{emoji} #{deal['id']} — {deal['customer_name']} — {deal['amount_cad']:,.0f} CAD\n"
        keyboard.append([
            InlineKeyboardButton(f"✅ #{deal['id']}", callback_data=f"approve_{deal['id']}"),
            InlineKeyboardButton(f"❌ #{deal['id']}", callback_data=f"reject_{deal['id']}"),
        ])
    keyboard.append([InlineKeyboardButton("🔙 برگشت", callback_data="main_panel")])
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── Live Price ──────────────────────────────────────────────────────────────

async def show_live_price(update, context):
    await update.callback_query.answer("در حال دریافت قیمت...")
    r100  = calculate_rate("CAD", 100)
    r500  = calculate_rate("CAD", 500)
    r1000 = calculate_rate("CAD", 1000)
    usdt  = get_market_base("USDT")
    if r100:
        usdt_line = f"نرخ تتر: {usdt['best_sell']:,.0f} تومان\n" if usdt and usdt.get("best_sell") else ""
        text = (
            f"💱 قیمت‌های لحظه‌ای\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{usdt_line}"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"100 CAD  = {r100['our_sell']:,.0f} تومان\n"
            f"500 CAD  = {r500['our_sell']:,.0f} تومان\n"
            f"1000 CAD = {r1000['our_sell']:,.0f} تومان\n"
        )
    else:
        text = "❌ قیمت در دسترس نیست"
    keyboard = [
        [InlineKeyboardButton("🔄 رفرش",   callback_data="live_price")],
        [InlineKeyboardButton("🔙 برگشت",  callback_data="main_panel")],
    ]
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── Deal Actions ────────────────────────────────────────────────────────────

async def approve_deal(update, context, deal_id):
    deal = get_deal(deal_id)
    if not deal:
        await update.callback_query.answer("معامله یافت نشد")
        return
    update_deal_status(deal_id, "approved")
    iran_message = (
        f"✅ معامله جدید\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 #{deal_id}\n"
        f"👤 {deal['customer_name']}\n"
        f"💰 {deal['amount_usdt']} USDT\n"
        f"🏦 {deal['bank_account']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"لطفاً واریز کنید"
    )
    try:
        await context.bot.send_message(chat_id=IRAN_AGENT_ID, text=iran_message)
        await update.callback_query.answer("✅ تأیید شد — پیام فرستاده شد")
    except Exception:
        await update.callback_query.answer("⚠️ تأیید شد اما پیام نرسید")
    await show_pending_deals(update, context)


async def reject_deal(update, context, deal_id):
    update_deal_status(deal_id, "rejected")
    await update.callback_query.answer("❌ معامله رد شد")
    await show_pending_deals(update, context)


# ─── Learning Report ─────────────────────────────────────────────────────────

async def _send_learning_report(reply_func):
    r = get_daily_report()
    objections_text = ""
    for msg, cnt in r["common_objections"]:
        short = (msg[:40] + "...") if len(msg) > 40 else msg
        objections_text += f"  • {short} ({cnt}×)\n"

    text = (
        f"📈 گزارش یادگیری امروز\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💬 گفتگوها: {r['total_conversations']}\n"
        f"✅ معامله موفق: {r['successful_deals']}\n"
        f"❌ معامله ناموفق: {r['failed_deals']}\n"
        f"👤 مشتری جدید: {r['new_customers']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"بازخورد ادمین:\n"
        f"  ✅ پاسخ خوب: {r['good_replies']}\n"
        f"  ❌ پاسخ بد: {r['bad_replies']}\n"
        f"  🤖 خیلی رسمی: {r['robotic_replies']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    if objections_text:
        text += f"⚠️ اعتراضات رایج:\n{objections_text}"
    else:
        text += "✅ مشکل خاصی ثبت نشده."

    await reply_func(text)


async def show_learning_report(update, context):
    await _send_learning_report(
        lambda t: update.callback_query.edit_message_text(
            t, reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 برگشت", callback_data="main_panel")]]
            )
        )
    )


# ─── Customer Profiles ───────────────────────────────────────────────────────

async def show_customer_profiles(update, context):
    profiles = get_all_customer_profiles(limit=10)
    if not profiles:
        text = "👥 هنوز هیچ مشتری‌ای ثبت نشده."
    else:
        text = f"👥 مشتریان ({len(profiles)})\n━━━━━━━━━━━━━━━━━━\n"
        for p in profiles:
            label = PROFILE_LABELS.get(p['profile_type'], p['profile_type'])
            name = p['username'] or p['user_id']
            text += (
                f"👤 {name} — {label}\n"
                f"  گفتگو: {p['total_conversations']} | موفق: {p['successful_deals']}\n"
            )
    keyboard = [[InlineKeyboardButton("🔙 برگشت", callback_data="main_panel")]]
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── Feedback Handler ────────────────────────────────────────────────────────

async def handle_feedback(update, context, data):
    # data format: fb_{type}_{log_id}
    parts = data.split("_", 2)   # ['fb', type, log_id]
    if len(parts) != 3:
        await update.callback_query.answer("خطا در پردازش")
        return
    _, fb_type, log_id_str = parts
    try:
        log_id = int(log_id_str)
    except ValueError:
        await update.callback_query.answer("خطا")
        return

    feedback_map = {
        "good":       "good_reply",
        "bad":        "bad_reply",
        "robotic":    "too_robotic",
        "wrongprice": "wrong_price",
        "unhappy":    "customer_unhappy",
        "dealok":     "deal_successful",
        "dealfail":   "deal_failed",
    }
    feedback_type = feedback_map.get(fb_type, fb_type)
    save_admin_feedback(log_id, "admin", feedback_type)

    label = FEEDBACK_LABELS.get(fb_type, feedback_type)
    await update.callback_query.answer(f"ثبت شد: {label}")


# ─── Callback Router ─────────────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.callback_query.answer("⛔ دسترسی ندارید")
        return
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main_panel":
        await show_main_panel(update, context)
    elif data == "pending_deals":
        await show_pending_deals(update, context)
    elif data == "live_price":
        await show_live_price(update, context)
    elif data == "learning_report":
        await show_learning_report(update, context)
    elif data == "customer_profiles":
        await show_customer_profiles(update, context)
    elif data.startswith("approve_"):
        await approve_deal(update, context, int(data.split("_")[1]))
    elif data.startswith("reject_"):
        await reject_deal(update, context, int(data.split("_")[1]))
    elif data.startswith("fb_"):
        await handle_feedback(update, context, data)


# ─── Text Message Handler ────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        amount = float(update.message.text.replace(",", "").replace(" ", ""))
        result = calculate_rate("CAD", amount)
        if result:
            await update.message.reply_text(
                f"💱 {amount:,.0f} CAD\n"
                f"خرید از مشتری: {result['our_buy']:,.0f} تومان\n"
                f"فروش به مشتری: {result['our_sell']:,.0f} تومان"
            )
        return
    except Exception:
        pass
    await update.message.reply_text("برای دیدن پنل: /start")


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 پنل مدیریت شروع شد...")
    app.run_polling()


if __name__ == "__main__":
    main()
