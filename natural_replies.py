import random
import re

# ─────────────────────────────────────────────────────────────────
# قالب‌های فارسی — صراف ایرانی تورنتو
# کوتاه، مستقیم، طبیعی — نه منشی، نه ربات
# فارسی محاوره‌ای تهرانی مدرن
# ─────────────────────────────────────────────────────────────────

REPLIES = {

    "greeting_buyer": [
        "سلام",
        "سلام، بفرمایید",
        "بله؟",
        "جانم؟",
    ],

    "greeting_seller": [
        "سلام",
        "سلام، بفرمایید",
        "بله؟",
        "جانم؟",
    ],

    "greeting_repeat": [
        "سلام",
        "بفرمایید",
        "جانم؟",
        "بله؟",
    ],

    "ask_amount": [
        "چند تا؟",
        "چقدر؟",
        "مبلغ؟",
        "چند دلار؟",
        "بگو مبلغ.",
    ],

    "ask_interac": [
        "Interac می‌زنید؟",
        "دارید اینترک می‌فرستید؟",
        "اینتراک داری؟",
        "آدرس اینتراکت رو بفرست.",
    ],

    "confirm_received": [
        "آره، رسید اومد.",
        "اوکی، اومد.",
        "دیدم.",
        "رسید.",
        "اوکی شد.",
        "ثبت شد.",
    ],

    "payment_not_received": [
        "هنوز ننشسته.",
        "هنوز نیومده.",
        "نرسیده هنوز.",
        "چند دقیقه دیگه چک می‌کنم.",
    ],

    "wrong_amount": [
        "مبلغ اشتباهه — {amount} باید بیاد.",
        "این رقم فرق داره. {amount} بود.",
        "{amount} باید می‌فرستادی.",
    ],

    "suspicious_receipt": [
        "باید بررسی بشه.",
        "عکس بهتری بفرست.",
        "واضح نیست.",
    ],

    "customer_buy": [
        "اوکی.",
        "چند تا می‌خوای؟",
        "باشه.",
        "بگو مبلغ.",
    ],

    "customer_sell": [
        "اوکی.",
        "چقدر داری؟",
        "بگو مبلغ.",
        "باشه.",
    ],

    "amount_over_5000": [
        "برای این مبلغ باید مدیر نرخ بده.",
        "این رقم رو باید به مدیر وصل کنم.",
        "مبلغ بالاست — هماهنگ می‌کنم.",
    ],

    "customer_confused": [
        "بگو چی می‌خوای.",
        "مشکلی نیست، بگو.",
        "قدم به قدم پیش می‌ریم.",
    ],

    "customer_impatient": [
        "الان.",
        "اوکی.",
        "باشه.",
    ],

    "customer_stopped_replying": [
        "هنوز هستی؟",
        "اگه سوال داری بگو.",
        "هر وقت خواستی پیام بده.",
    ],

    "processing_voice": [
        "شنیدم.",
        "اوکی.",
    ],

    "deal_confirmed_customer": [
        "اوکی، ثبت شد.",
        "باشه، الان هماهنگ می‌کنم.",
        "اوکی شد.",
        "ثبت شد.",
    ],

    "ask_bank_account": [
        "شبا یا شماره حساب بده.",
        "شماره حساب ایرانت رو بفرست.",
        "شبا بده.",
    ],

    "ask_contact": [
        "شماره داری؟",
        "می‌تونیم تلفنی هم حل کنیم.",
    ],

    # وقتی قیمت لحظه‌ای در دسترس نیست — هرگز عدد اختراع نمی‌شود
    "error_price_unavailable": [
        "نرخ لحظه‌ای الان نیومده. به محض اینکه اومد می‌فرستم.",
        "نرخ الان تابلوئه، چون قیمت‌ها لحظه‌ای هستن. چند دقیقه دیگه دوباره پیام بدید.",
        "قیمت‌ها لحظه‌ای هستن؛ الان تابلو نیست.",
    ],

    # وقتی قیمت هست ولی مشتری جهت یا مبلغ نگفته
    "price_ask_direction": [
        "خرید می‌خواید یا فروش؟ چندتا؟",
        "برای خرید می‌خواید یا فروش؟ مقدار رو هم بفرستید.",
        "چند تا می‌خواید؟ خرید یا فروش؟",
        "قیمت الان تابلوئه؛ برای خرید یا فروش چندتا می‌خواید؟",
    ],

    "manager_required": [
        "برای این مبلغ باید مدیر نرخ بده.",
        "این رقم بالاست، باید مدیر تأیید کنه.",
        "مبلغ بالاست — باید هماهنگ کنم.",
    ],

    "transfer_initiated": [
        "الان هماهنگ می‌کنم.",
        "اوکی، ثبت شد.",
        "تا چند دقیقه دیگه واریز میشه.",
    ],

    "interac_sent": [
        "زدم. چک کن.",
        "Interac زدم.",
        "فرستادم، ایمیلت رو چک کن.",
    ],

    "listening": [
        "اوکی.",
        "باشه.",
    ],
}


# ─── دریافت پاسخ تصادفی ──────────────────────────────────────────

def get_reply(situation, tone="friendly", **kwargs):
    options = REPLIES.get(situation, ["باشه."])
    reply = random.choice(options)
    if kwargs:
        try:
            reply = reply.format(**kwargs)
        except (KeyError, ValueError):
            pass
    if tone == "urgent":
        reply = reply.rstrip(".").rstrip("،")
        reply += "، زود."
    elif tone == "reassuring":
        if "نگران" not in reply:
            reply += " مشکلی نیست."
    return reply


# ─── تأخیر طبیعی — شبیه‌سازی آدم واقعی ─────────────────────────

def reply_delay(customer_tone="neutral") -> float:
    """Seconds to wait before replying — simulates a busy human operator."""
    delays = {
        "impatient":  random.uniform(0.5, 1.2),
        "aggressive": random.uniform(0.4, 1.0),
        "confused":   random.uniform(1.0, 2.0),
        "neutral":    random.uniform(1.0, 2.5),
        "calm":       random.uniform(1.5, 3.0),
    }
    return delays.get(customer_tone, random.uniform(1.0, 2.5))


def should_send_ack(customer_tone="neutral") -> bool:
    """~18% chance to send a short reading-ack before the real reply. Never for impatient."""
    if customer_tone in ("impatient", "aggressive"):
        return False
    return random.random() < 0.18


# ─── بهینه‌سازی متن برای TTS ─────────────────────────────────────

def voice_optimize(text):
    if not text:
        return text
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'_+', '', text)
    text = re.sub(r'`+', '', text)
    emoji_pattern = re.compile(
        "[\U00010000-\U0010ffff"
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "☀-➿✂-➰]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub('', text)
    text = text.replace("...", ".")
    text = re.sub(r'\.(?=[^\s])', '. ', text)
    text = re.sub(r'  +', ' ', text).strip()
    sentences = []
    for chunk in text.split("."):
        chunk = chunk.strip()
        if not chunk:
            continue
        if len(chunk) > 70:
            parts = chunk.split("،")
            if len(parts) > 1:
                mid = len(parts) // 2
                sentences.append("، ".join(parts[:mid]))
                sentences.append("، ".join(parts[mid:]))
            else:
                sentences.append(chunk)
        else:
            sentences.append(chunk)
    return ". ".join(s for s in sentences if s)
