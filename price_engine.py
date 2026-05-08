import time
import sqlite3

from config import (
    MARKET_SPREAD_TOMAN,
    MAX_PRICE_AGE_MINUTES,
    USD_CAD_RATE,
)

DB_PATH = "/var/www/exchange_bot/exchange.db"

# When a channel only posts sell price, estimate buy price using this gap
DEFAULT_BUY_GAP = 4000

# Amounts at or above this require manager sign-off
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
    # Add confidence column to existing tables that pre-date this schema
    try:
        cur.execute("ALTER TABLE market_prices ADD COLUMN confidence REAL DEFAULT 1.0")
    except Exception:
        pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS current_rates (
            currency   TEXT PRIMARY KEY,
            best_sell  INTEGER,
            best_buy   INTEGER,
            our_sell   INTEGER,
            our_buy    INTEGER,
            updated_at INTEGER
        )
    """)
    conn.commit()
    conn.close()


def get_margin_by_amount(amount):
    """Negotiation wiggle-room (toman) communicated to the AI — not shown to customer."""
    try:
        amount = float(amount)
    except Exception:
        return 100
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
    if sell_price and not buy_price:
        buy_price = int(sell_price) - DEFAULT_BUY_GAP
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


def _get_direct_base(currency):
    """
    Average sell and buy prices from fresh channel data for `currency`.
    Fresh = within MAX_PRICE_AGE_MINUTES. Rejects low-confidence entries.
    Returns None if not enough data.
    """
    init_price_db()
    now = int(time.time())
    max_age = MAX_PRICE_AGE_MINUTES * 60
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT sell_price, buy_price, confidence FROM market_prices
        WHERE currency = ?
          AND created_at >= ?
          AND confidence >= 0.5
    """, (currency, now - max_age))
    rows = cur.fetchall()
    conn.close()

    sells = [r[0] for r in rows if r[0]]
    buys  = [r[1] for r in rows if r[1]]

    if not sells and not buys:
        return None

    avg_sell = int(sum(sells) / len(sells)) if sells else None
    avg_buy  = int(sum(buys)  / len(buys))  if buys  else None

    return {"avg_sell": avg_sell, "avg_buy": avg_buy}


def get_market_base(currency):
    """
    Returns average market sell/buy for the given currency.
    For CAD: tries direct prices first, then derives from USDT/USD.
    """
    if currency == "CAD":
        direct = _get_direct_base("CAD")
        if direct:
            return direct
        # Derive from USDT (preferred) or USD
        usd_base = _get_direct_base("USDT") or _get_direct_base("USD")
        if usd_base:
            sell = int(usd_base["avg_sell"] / USD_CAD_RATE) if usd_base.get("avg_sell") else None
            buy  = int(usd_base["avg_buy"]  / USD_CAD_RATE) if usd_base.get("avg_buy")  else None
            return {"avg_sell": sell, "avg_buy": buy}
        return None

    return _get_direct_base(currency)


def calculate_rate(currency, amount):
    """
    our_sell = avg_market_sell - MARKET_SPREAD_TOMAN   (we sell cheaper → attract buyers)
    our_buy  = avg_market_buy  + MARKET_SPREAD_TOMAN   (we buy more → attract sellers)
    Returns None if no fresh market data — never invents prices.
    """
    base = get_market_base(currency)
    if not base:
        return None

    avg_sell = base["avg_sell"]
    avg_buy  = base["avg_buy"]
    margin   = get_margin_by_amount(amount)

    our_sell = (avg_sell - MARKET_SPREAD_TOMAN) if avg_sell else None
    our_buy  = (avg_buy  + MARKET_SPREAD_TOMAN) if avg_buy  else None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO current_rates
            (currency, best_sell, best_buy, our_sell, our_buy, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (currency, avg_sell, avg_buy, our_sell, our_buy, int(time.time())))
    conn.commit()
    conn.close()

    return {
        "currency": currency,
        "amount":   amount,
        "margin":   margin,
        "avg_sell": avg_sell,
        "avg_buy":  avg_buy,
        "our_sell": our_sell,
        "our_buy":  our_buy,
    }


def calculate_customer_price(currency, amount, deal_type):
    """
    deal_type:
      buyer  → customer buys from us  → we show our_sell
      seller → customer sells to us   → we show our_buy

    Returns None when no live data exists (never returns a fake price).
    Returns {"manager_required": True} for amounts >= MANAGER_LIMIT.
    """
    if should_send_to_manager(amount):
        return {"manager_required": True}

    rate = calculate_rate(currency, amount)
    if not rate:
        return None

    if deal_type == "buyer":
        price = rate["our_sell"]
    elif deal_type == "seller":
        price = rate["our_buy"]
    else:
        return None

    if price is None:
        return None

    return {
        "manager_required": False,
        "price":  price,
        "margin": rate["margin"],
        "rate":   rate,
    }
