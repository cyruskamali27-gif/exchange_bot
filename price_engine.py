import time
import sqlite3

from config import (
    BUY_SPREAD_TOMAN,
    SELL_SPREAD_TOMAN,
    MAX_PRICE_AGE_MINUTES,
    USD_CAD_RATE,
)

DB_PATH = "/var/www/exchange_bot/exchange.db"

MANAGER_LIMIT = 5000


def init_price_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_prices (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            currency     TEXT,
            source       TEXT,
            sell_price   INTEGER,
            buy_price    INTEGER,
            message_date TEXT,
            confidence   REAL DEFAULT 1.0,
            created_at   INTEGER
        )
    """)
    try:
        cur.execute("ALTER TABLE market_prices ADD COLUMN confidence REAL DEFAULT 1.0")
    except Exception:
        pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS current_rates (
            currency    TEXT PRIMARY KEY,
            raw_source  INTEGER,
            our_sell    INTEGER,
            our_buy     INTEGER,
            updated_at  INTEGER
        )
    """)
    # Migrate old schema (best_sell/best_buy columns) if needed
    try:
        cur.execute("ALTER TABLE current_rates ADD COLUMN raw_source INTEGER")
    except Exception:
        pass
    conn.commit()
    conn.close()


def get_margin_by_amount(amount):
    try:
        amount = float(amount)
    except Exception:
        return 0
    if amount < 1000:
        return 100
    if amount < 2000:
        return 100
    if amount < 3000:
        return 200
    if amount < 4000:
        return 300
    if amount < 5000:
        return 400
    return 0  # manager required


def should_send_to_manager(amount):
    try:
        return float(amount) >= MANAGER_LIMIT
    except Exception:
        return False


def save_market_price(
    currency, source,
    sell_price=None, buy_price=None,
    message_date=None, confidence=1.0
):
    init_price_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO market_prices
            (currency, source, sell_price, buy_price, message_date, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        currency,
        source,
        int(sell_price) if sell_price else None,
        int(buy_price)  if buy_price  else None,
        message_date,
        float(confidence),
        int(time.time()),
    ))
    conn.commit()
    conn.close()


def _get_avg_sell(currency):
    """
    Returns the average sell price from fresh channel data for `currency`.
    Fresh = within MAX_PRICE_AGE_MINUTES. Returns None if no data.
    """
    init_price_db()
    now = int(time.time())
    max_age = MAX_PRICE_AGE_MINUTES * 60
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT sell_price FROM market_prices
        WHERE currency = ?
          AND created_at >= ?
          AND sell_price IS NOT NULL
          AND confidence >= 0.5
    """, (currency, now - max_age))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return None
    return int(sum(r[0] for r in rows) / len(rows))


def get_market_base(currency):
    """
    Returns {"ref_price": int} — the single market reference price for a currency.
    USD falls back to USDT when no fresh Tehran data.
    CAD is derived from USDT when no direct CAD data.
    Returns None when no live data is available.
    """
    if currency == "USD":
        ref = _get_avg_sell("USD")
        if ref:
            return {"ref_price": ref, "source": "USD"}
        # Fall back to USDT (1 USDT ≈ 1 USD in toman terms)
        ref = _get_avg_sell("USDT")
        if ref:
            return {"ref_price": ref, "source": "USDT→USD"}
        return None

    if currency == "USDT":
        ref = _get_avg_sell("USDT")
        return {"ref_price": ref, "source": "USDT"} if ref else None

    if currency == "CAD":
        ref = _get_avg_sell("CAD")
        if ref:
            return {"ref_price": ref, "source": "CAD"}
        # Derive from USDT
        ref = _get_avg_sell("USDT")
        if ref:
            return {"ref_price": int(ref / USD_CAD_RATE), "source": "USDT→CAD"}
        return None

    ref = _get_avg_sell(currency)
    return {"ref_price": ref, "source": currency} if ref else None


def calculate_rate(currency, amount=100):
    """
    ref_price  = avg market sell price from channels
    our_sell   = ref_price - SELL_SPREAD_TOMAN  (we sell to customer, cheaper than market)
    our_buy    = ref_price - BUY_SPREAD_TOMAN   (we buy from customer, our profit margin)

    Returns None if no live data — never invents prices.
    """
    base = get_market_base(currency)
    if not base:
        return None

    ref   = base["ref_price"]
    our_sell = ref - SELL_SPREAD_TOMAN
    our_buy  = ref - BUY_SPREAD_TOMAN
    margin   = get_margin_by_amount(amount)

    init_price_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO current_rates
            (currency, raw_source, our_sell, our_buy, updated_at)
        VALUES (?, ?, ?, ?, ?)
    """, (currency, ref, our_sell, our_buy, int(time.time())))
    conn.commit()
    conn.close()

    return {
        "currency":  currency,
        "amount":    amount,
        "margin":    margin,
        "raw_source": ref,
        "source_label": base["source"],
        "our_sell":  our_sell,
        "our_buy":   our_buy,
    }


def calculate_customer_price(currency, amount, deal_type):
    """
    deal_type:
      buyer  → customer buys from us  → they pay our_sell
      seller → customer sells to us   → they receive our_buy

    Returns None when no live data.
    Returns {"manager_required": True} for amounts >= MANAGER_LIMIT.
    """
    if should_send_to_manager(amount):
        return {"manager_required": True}

    rate = calculate_rate(currency, amount)
    if not rate:
        return None

    price = rate["our_sell"] if deal_type == "buyer" else rate["our_buy"] if deal_type == "seller" else None
    if price is None:
        return None

    return {
        "manager_required": False,
        "price":       price,
        "margin":      rate["margin"],
        "raw_source":  rate["raw_source"],
        "rate":        rate,
    }
