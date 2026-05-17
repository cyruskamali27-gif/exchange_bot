import asyncio
import logging
from datetime import datetime
from design_iteration_agent import (
    register_design_handlers, design_post_init,
    _pending_screenshot, _detect_region_from_text, REGION_KEYWORDS,
)

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
from exchange_brain import save_learned_mistake
import sqlite3

# admin_id → log_id: tracks when admin pressed ✏️ and needs to type a correction
_awaiting_correction: dict[int, int] = {}

DB_FILE = "/var/www/exchange_bot/exchange.db"

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


# ─── New admin commands ───────────────────────────────────────────────────────

async def cmd_safemode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle SAFE_MODE: /safemode on  or  /safemode off"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        await update.message.reply_text("استفاده: /safemode on  یا  /safemode off")
        return
    val = "true" if args[0].lower() == "on" else "false"
    # Update .env in-place
    env_path = "/var/www/exchange_bot/.env"
    try:
        with open(env_path) as f:
            lines = f.readlines()
        with open(env_path, "w") as f:
            for line in lines:
                if line.startswith("SAFE_MODE="):
                    f.write(f"SAFE_MODE={val}\n")
                elif line.startswith("TEST_GROUP_ONLY="):
                    f.write(f"TEST_GROUP_ONLY={val}\n")
                else:
                    f.write(line)
        status = "🔒 فعال" if val == "true" else "🔓 غیرفعال"
        await update.message.reply_text(f"SAFE_MODE {status}\nبرای اعمال: pm2 restart exchange-scanner")
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")


async def cmd_corrections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show learned mistakes: /corrections"""
    if not is_admin(update.effective_user.id):
        return
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute(
            "SELECT id, mistake_type, original_reply, corrected_reply, created_at FROM learned_mistakes ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("هیچ اشتباه یاد گرفته‌ای ثبت نشده.")
            return
        text = "📚 اشتباهات یاد گرفته شده:\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            text += f"\n#{r[0]} [{r[1]}]\n❌ {r[2][:60] if r[2] else '—'}\n✅ {r[3][:60] if r[3] else '—'}\n"
        await update.message.reply_text(text[:4000])
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")


async def cmd_learning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show learning stats: /learning"""
    if not is_admin(update.effective_user.id):
        return
    await _send_learning_report(update.message.reply_text)


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show live prices: /price"""
    if not is_admin(update.effective_user.id):
        return
    lines = []
    for cur in ("USDT", "USD", "CAD"):
        r = calculate_rate(cur)
        if r:
            lines.append(f"{cur}: فروش {r['our_sell']:,} | خرید {r['our_buy']:,} | منبع {r['source_label']}")
        else:
            lines.append(f"{cur}: — در دسترس نیست")
    await update.message.reply_text("💱 قیمت‌های لحظه‌ای\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines))


async def cmd_customers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List customers: /customers"""
    if not is_admin(update.effective_user.id):
        return
    profiles = get_all_customer_profiles(limit=15)
    if not profiles:
        await update.message.reply_text("هنوز مشتری‌ای ثبت نشده.")
        return
    text = f"👥 مشتریان ({len(profiles)})\n━━━━━━━━━━━━━━━━━━\n"
    for p in profiles:
        vip  = "⭐ " if p.get("is_vip") else ""
        name = p.get("username") or p["user_id"]
        text += f"{vip}👤 {name} | گفتگو: {p['total_conversations']} | موفق: {p['successful_deals']}\n"
    await update.message.reply_text(text[:4000])


async def cmd_emotion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/emotion [user_id] — show emotion history for a user"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("استفاده: /emotion [user_id]")
        return
    uid = args[0]
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute(
            """SELECT dominant_emotion, urgency_score, trust_score, suspicion_score, detected_at
               FROM emotion_history WHERE user_id=? ORDER BY id DESC LIMIT 10""",
            (uid,)
        ).fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text(f"تاریخچه احساسی برای {uid} یافت نشد.")
            return
        text = f"🧠 احساسات کاربر {uid}\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            text += (f"• {r[0]} | urgency={r[1]:.1f} trust={r[2]:.1f} "
                     f"suspicion={r[3]:.1f} | {r[4][:16]}\n")
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")


async def cmd_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/vip [user_id] — toggle VIP status"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("استفاده: /vip [user_id]")
        return
    uid = args[0]
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute("SELECT is_vip FROM customer_profiles WHERE user_id=?", (uid,)).fetchone()
        if not row:
            await update.message.reply_text(f"کاربر {uid} یافت نشد.")
            conn.close()
            return
        new_vip = 0 if row[0] else 1
        conn.execute("UPDATE customer_profiles SET is_vip=? WHERE user_id=?", (new_vip, uid))
        conn.commit()
        conn.close()
        status = "⭐ VIP شد" if new_vip else "VIP برداشته شد"
        await update.message.reply_text(f"کاربر {uid} — {status}")
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ban [user_id] — ban a user"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("استفاده: /ban [user_id]")
        return
    uid = args[0]
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT OR IGNORE INTO customer_profiles (user_id, profile_type, last_seen, created_at) VALUES (?,?,?,?)",
            (uid, "banned", datetime.now().isoformat(), datetime.now().isoformat())
        )
        conn.execute("UPDATE customer_profiles SET banned=1 WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"🚫 کاربر {uid} بن شد.")
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")


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


# ─── Learning helpers ────────────────────────────────────────────────────────

def _get_log_entry(log_id: int) -> dict | None:
    """Fetch agent_reply + customer_message from agent_learning_log."""
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute(
            "SELECT agent_reply, customer_message, tone FROM agent_learning_log WHERE id=?",
            (log_id,)
        ).fetchone()
        conn.close()
        if row:
            return {"reply": row[0] or "", "message": row[1] or "", "tone": row[2] or "neutral"}
    except Exception as e:
        logging.error("_get_log_entry: %s", e)
    return None


def _save_to_successful_patterns(log_id: int):
    entry = _get_log_entry(log_id)
    if not entry or not entry["reply"]:
        return
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            """INSERT INTO successful_patterns (pattern_text, success_count, customer_mood, last_used)
               VALUES (?,1,?,?)
               ON CONFLICT(pattern_text) DO UPDATE SET
                 success_count=success_count+1, last_used=excluded.last_used""",
            (entry["reply"][:300], entry["tone"], datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        logging.info("Successful pattern saved (log_id=%s)", log_id)
    except Exception as e:
        logging.error("_save_to_successful_patterns: %s", e)


def _save_to_learned_mistakes(log_id: int, mistake_type: str, corrected: str | None = None):
    entry = _get_log_entry(log_id)
    if not entry or not entry["reply"]:
        return
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            """INSERT INTO learned_mistakes
               (mistake_type, original_reply, corrected_reply, admin_id, created_at)
               VALUES (?,?,?,?,?)""",
            (mistake_type, entry["reply"][:500], corrected, "admin", datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        logging.info("Learned mistake saved (log_id=%s type=%s)", log_id, mistake_type)
    except Exception as e:
        logging.error("_save_to_learned_mistakes: %s", e)


# ─── Feedback Handler ────────────────────────────────────────────────────────

async def handle_feedback(update, context, data):
    # data format: fb_{type}_{log_id}
    parts = data.split("_", 2)   # ['fb', type, log_id]
    if len(parts) != 3:
        await update.callback_query.answer("خطا در پردازش")
        return
    _, fb_type, log_id_str = parts

    # ── Correction mode: admin presses ✏️ ────────────────────────────
    if fb_type == "correct":
        try:
            log_id = int(log_id_str)
            _awaiting_correction[update.effective_user.id] = log_id
            await update.callback_query.answer("پاسخ صحیح را تایپ کنید")
            entry = _get_log_entry(log_id)
            original = entry["reply"][:120] if entry else "—"
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=(
                    f"✏️ تصحیح لاگ #{log_id}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"❌ پاسخ اشتباه:\n{original}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"✅ پاسخ صحیح را تایپ کنید:"
                )
            )
        except Exception as e:
            await update.callback_query.answer("خطا")
            logging.error("correction setup: %s", e)
        return

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

    # ── Wire feedback into learning tables ────────────────────────────
    if feedback_type in ("good_reply", "deal_successful"):
        _save_to_successful_patterns(log_id)
    elif feedback_type in ("bad_reply", "too_robotic", "wrong_price"):
        _save_to_learned_mistakes(log_id, feedback_type)

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
    admin_id = update.effective_user.id

    # ── Correction flow: admin typed the corrected reply ─────────────
    if admin_id in _awaiting_correction:
        log_id = _awaiting_correction.pop(admin_id)
        corrected = (update.message.text or "").strip()
        if corrected:
            entry = _get_log_entry(log_id)
            original = entry["reply"] if entry else ""
            _save_to_learned_mistakes(log_id, "admin_correction", corrected)
            await update.message.reply_text(
                f"✅ تصحیح ذخیره شد\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"❌ {original[:100]}\n"
                f"✅ {corrected[:100]}"
            )
        else:
            await update.message.reply_text("تصحیح لغو شد.")
        return

    # ── Screenshot pending: attach text as design complaint ──────────────
    text_msg = (update.message.text or "").strip()
    if admin_id in _pending_screenshot and text_msg:
        region = _detect_region_from_text(text_msg)
        is_design_complaint = any(
            kw in text_msg.lower()
            for kws in REGION_KEYWORDS.values()
            for kw in kws
        ) or any(w in text_msg.lower() for w in ["خراب", "بد", "درست", "نسخه", "بهتر", "fix", "مشکل"])
        if is_design_complaint:
            _pending_screenshot[admin_id]["text"] = text_msg
            await update.message.reply_text(
                f"📝 متن شکایت ثبت شد: «{text_msg}»\n"
                f"ناحیه تشخیص داده شده: {region}\n\n"
                "برای شروع فیکس خودکار:\n/design_fix_from_screenshot"
            )
            return

    # ── Numeric CAD → price lookup ────────────────────────────────────
    try:
        amount = float(text_msg.replace(",", "").replace(" ", ""))
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


# ─── Go-Live Command ─────────────────────────────────────────────────────────

async def cmd_go_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/go_live — disable TEST_GROUP_ONLY and enable full production routing."""
    if not is_admin(update.effective_user.id):
        return

    env_path = "/var/www/exchange_bot/.env"
    try:
        with open(env_path) as f:
            lines = f.readlines()

        changes = []
        new_lines = []
        for line in lines:
            if line.startswith("TEST_GROUP_ONLY="):
                new_lines.append("TEST_GROUP_ONLY=false\n")
                changes.append("TEST_GROUP_ONLY=false")
            elif line.startswith("SAFE_MODE="):
                new_lines.append("SAFE_MODE=false\n")
                changes.append("SAFE_MODE=false")
            else:
                new_lines.append(line)

        with open(env_path, "w") as f:
            f.writelines(new_lines)

        await update.message.reply_text(
            "🚀 حالت تولید فعال شد!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(f"✅ {c}" for c in changes) +
            "\n━━━━━━━━━━━━━━━━━━\n"
            "برای فعال‌سازی کامل:\n"
            "`pm2 restart exchange-scanner`\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⚠️ اسکنر را یک ساعت مانیتور کنید!\n"
            "برای برگشت: /safemode on",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(design_post_init).build()
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("report",      cmd_report))
    app.add_handler(CommandHandler("safemode",    cmd_safemode))
    app.add_handler(CommandHandler("corrections", cmd_corrections))
    app.add_handler(CommandHandler("learning",    cmd_learning))
    app.add_handler(CommandHandler("price",       cmd_price))
    app.add_handler(CommandHandler("customers",   cmd_customers))
    app.add_handler(CommandHandler("emotion",     cmd_emotion))
    app.add_handler(CommandHandler("vip",         cmd_vip))
    app.add_handler(CommandHandler("ban",         cmd_ban))
    app.add_handler(CommandHandler("go_live",     cmd_go_live))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    register_design_handlers(app)

    # ── Poster Pipeline commands (same bot, avoids token conflict) ────────────
    from poster_pipeline import (
        cmd_poster_pipeline, cmd_ai_design_poster, cmd_render_prices,
        cmd_vision_qc, cmd_publish_approved, cmd_pipeline_status,
    )
    app.add_handler(CommandHandler("poster_pipeline",  cmd_poster_pipeline))
    app.add_handler(CommandHandler("ai_design_poster", cmd_ai_design_poster))
    app.add_handler(CommandHandler("render_prices",    cmd_render_prices))
    app.add_handler(CommandHandler("vision_qc",        cmd_vision_qc))
    app.add_handler(CommandHandler("publish_approved", cmd_publish_approved))
    app.add_handler(CommandHandler("pipeline_status",  cmd_pipeline_status))

    print("🚀 پنل مدیریت شروع شد...")
    app.run_polling()


if __name__ == "__main__":
    main()
