import time
import sqlite3

DB_PATH = "/var/www/exchange_bot/exchange.db"

# اختلاف پیش‌فرض خرید با فروش، اگر فقط قیمت فروش پیدا شد
DEFAULT_BUY_GAP = 4000

# بالای این مبلغ، قیمت اتوماتیک نده؛ وصل به مدیر
MANAGER_LIMIT = 5000


def init_price_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS market_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        currency TEXT,
        source TEXT,
        sell_price INTEGER,
        buy_price INTEGER,
        message_date TEXT,
        created_at INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS current_rates (
        currency TEXT PRIMARY KEY,
        best_sell INTEGER,
        best_buy INTEGER,
        our_sell INTEGER,
        our_buy INTEGER,
        updated_at INTEGER
    )
    """)

    conn.commit()
    conn.close()


def get_margin_by_amount(amount):
    """
    مقدار چانه‌زنی/تخفیف بر اساس مبلغ معامله
    """
    try:
        amount = float(amount)
    except:
        return 100

    if amount < 1000:
        return 100
    elif amount < 2000:
        return 100
    elif amount < 3000:
        return 200
    elif amount < 4000:
        return 300
    elif amount < 5000:
        return 400
    else:
        return 500


def should_send_to_manager(amount):
    try:
        return float(amount) >= MANAGER_LIMIT
    except:
        return False


def save_market_price(currency, source, sell_price=None, buy_price=None, message_date=None):
    """
    ذخیره قیمت بازار از کانال‌ها
    currency مثال:
    USD
    CAD
    USDT
    """

    init_price_db()

    if sell_price and not buy_price:
        buy_price = int(sell_price) - DEFAULT_BUY_GAP

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO market_prices
    (currency, source, sell_price, buy_price, message_date, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        currency,
        source,
        int(sell_price) if sell_price else None,
        int(buy_price) if buy_price else None,
        message_date,
        int(time.time())
    ))

    conn.commit()
    conn.close()


def get_market_base(currency):
    """
    از قیمت‌های یک ساعت اخیر:
    فروش بازار = پایین‌ترین فروش
    خرید بازار = بالاترین خرید
    """

    init_price_db()

    now = int(time.time())
    max_age = 60 * 60

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    SELECT sell_price, buy_price FROM market_prices
    WHERE currency = ?
    AND created_at >= ?
    """, (currency, now - max_age))

    rows = cur.fetchall()
    conn.close()

    sells = [r[0] for r in rows if r[0]]
    buys = [r[1] for r in rows if r[1]]

    if not sells and not buys:
        return None

    best_sell = min(sells) if sells else None
    best_buy = max(buys) if buys else None

    return {
        "best_sell": best_sell,
        "best_buy": best_buy
    }


def calculate_rate(currency, amount):
    """
    فروش ما = پایین‌ترین فروش صرافی‌ها - margin
    خرید ما = بالاترین خرید صرافی‌ها + margin
    """

    base = get_market_base(currency)

    if not base:
        return None

    margin = get_margin_by_amount(amount)

    best_sell = base["best_sell"]
    best_buy = base["best_buy"]

    our_sell = best_sell - margin if best_sell else None
    our_buy = best_buy + margin if best_buy else None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    INSERT OR REPLACE INTO current_rates
    (currency, best_sell, best_buy, our_sell, our_buy, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        currency,
        best_sell,
        best_buy,
        our_sell,
        our_buy,
        int(time.time())
    ))

    conn.commit()
    conn.close()

    return {
        "currency": currency,
        "amount": amount,
        "margin": margin,
        "best_sell": best_sell,
        "best_buy": best_buy,
        "our_sell": our_sell,
        "our_buy": our_buy
    }


def calculate_customer_price(currency, amount, deal_type):
    """
    deal_type:
    buyer  = مشتری می‌خواهد بخرد → ما به او می‌فروشیم → our_sell
    seller = مشتری می‌خواهد بفروشد → ما از او می‌خریم → our_buy
    """

    if should_send_to_manager(amount):
        return {
            "manager_required": True,
            "message": "برای این مبلغ، لطفاً با مدیر صحبت کنید تا قیمت دقیق و ویژه بهتون بدن."
        }

    rate = calculate_rate(currency, amount)

    if not rate:
        return None

    if deal_type == "buyer":
        return {
            "manager_required": False,
            "price": rate["our_sell"],
            "rate": rate
        }

    if deal_type == "seller":
        return {
            "manager_required": False,
            "price": rate["our_buy"],
            "rate": rate
        }

    return None
