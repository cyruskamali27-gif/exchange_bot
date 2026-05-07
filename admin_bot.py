import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from config import BOT_TOKEN, ADMIN_ID, IRAN_AGENT_ID, MARGIN_CAD
NAZZY_VOICE_ID = "WwAjIyMBDBNl1dvId9Xe"
ADRIAN_VOICE_ID = "BognUUMX6W1qmZKB2TOw"
from database import init_db, get_today_stats, get_pending_deals, get_usdt_balance, update_deal_status, get_deal
from price_engine import calculate_rate, get_market_base

logging.basicConfig(level=logging.INFO)

def is_admin(user_id):
    return user_id == ADMIN_ID

async def show_main_panel(update, context):
    stats = get_today_stats()
    balance = get_usdt_balance()
    pending = get_pending_deals()
    text = (
        f"🏦 پنل مدیریت صرافی\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 موجودی تتر: {balance:,.0f} USDT\n"
        f"⏳ در انتظار: {len(pending)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 امروز:\n"
        f"  معاملات: {stats['count']}\n"
        f"  سود: {stats['profit']:,.0f} CAD\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [InlineKeyboardButton("⏳ معاملات در انتظار", callback_data="pending_deals"),
         InlineKeyboardButton("💱 قیمت لحظه‌ای", callback_data="live_price")],
        [InlineKeyboardButton("👥 کارمندان", callback_data="agents_status"),
         InlineKeyboardButton("📊 گزارش امروز", callback_data="today_report")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ دسترسی ندارید")
        return
    await show_main_panel(update, context)

async def show_pending_deals(update, context):
    pending = get_pending_deals()
    if not pending:
        keyboard = [[InlineKeyboardButton("🔙 برگشت", callback_data="main_panel")]]
        await update.callback_query.edit_message_text("✅ هیچ معامله‌ای در انتظار نیست", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    text = f"⏳ معاملات در انتظار ({len(pending)})\n━━━━━━━━━━━━━━━━━━\n"
    keyboard = []
    for deal in pending[:5]:
        emoji = "📤" if deal['type']=='sell' else "📥"
        text += f"\n{emoji} #{deal['id']} — {deal['customer_name']} — {deal['amount_cad']:,.0f} CAD\n"
        keyboard.append([
            InlineKeyboardButton(f"✅ #{deal['id']}", callback_data=f"approve_{deal['id']}"),
            InlineKeyboardButton(f"❌ #{deal['id']}", callback_data=f"reject_{deal['id']}")
        ])
    keyboard.append([InlineKeyboardButton("🔙 برگشت", callback_data="main_panel")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_live_price(update, context):
    await update.callback_query.answer("در حال دریافت قیمت...")
    r100 = calculate_rate("CAD", 100)
    r500 = calculate_rate("CAD", 500)
    r1000 = calculate_rate("CAD", 1000)
    usdt = get_market_base("USDT")
    if r100:
        usdt_line = f"نرخ تتر: {usdt['best_sell']:,.0f} تومان\n" if usdt and usdt.get("best_sell") else ""
        text = (
            f"💱 قیمت‌های لحظه‌ای\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{usdt_line}"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"100 CAD = {r100['our_sell']:,.0f} تومان\n"
            f"500 CAD = {r500['our_sell']:,.0f} تومان\n"
            f"1000 CAD = {r1000['our_sell']:,.0f} تومان\n"
        )
    else:
        text = "❌ خطا در دریافت قیمت"
    keyboard = [
        [InlineKeyboardButton("🔄 رفرش", callback_data="live_price")],
        [InlineKeyboardButton("🔙 برگشت", callback_data="main_panel")]
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

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
        await update.callback_query.answer("✅ تأیید شد - پیام به علی فرستاده شد")
    except:
        await update.callback_query.answer("⚠️ تأیید شد اما پیام به علی نرسید")
    await show_pending_deals(update, context)

async def reject_deal(update, context, deal_id):
    update_deal_status(deal_id, "rejected")
    await update.callback_query.answer("❌ معامله رد شد")
    await show_pending_deals(update, context)

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
    elif data.startswith("approve_"):
        await approve_deal(update, context, int(data.split("_")[1]))
    elif data.startswith("reject_"):
        await reject_deal(update, context, int(data.split("_")[1]))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        amount = float(update.message.text.replace(",","").replace(" ",""))
        result = calculate_rate("CAD", amount)
        if result:
            await update.message.reply_text(
                f"💱 {amount:,.0f} CAD\n"
                f"خرید: {result['our_buy']:,.0f} تومان\n"
                f"فروش: {result['our_sell']:,.0f} تومان"
            )
        return
    except:
        pass
    await update.message.reply_text("برای دیدن پنل: /start")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 پنل مدیریت شروع شد...")
    app.run_polling()

if __name__ == "__main__":
    main()
