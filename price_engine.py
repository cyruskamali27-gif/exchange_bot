import json
import logging
import os
import time
import sqlite3

from config import (
    BUY_SPREAD_TOMAN,
    SELL_SPREAD_TOMAN,
    MAX_PRICE_AGE_MINUTES,
    USD_CAD_RATE,
    USDT_BUY_SPREAD,
    USD_CAD_SELL_MARGIN,
    USD_CAD_BUY_MARGIN,
)

log = logging.getLogger("price_engine")
# All entries prefixed [PRICE_ENGINE]
CURRENT_PRICE_JSON = "/var/www/exchange_bot/current_price.json"

DB_PATH = "/var/www/exchange_bot/exchange.db"
MANAGER_LIMIT = 5000


# ─── JSON price cache helpers ─────────────────────────────────────────────────

def _write_price_json(currency: str, ref: int, our_sell: int, our_buy: int):
    """Persist current rate to JSON so exchange_brain can fall back to it offline."""
    try:
        try:
            with open(CURRENT_PRICE_JSON) as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[currency] = {
            "price":      ref,
            "our_sell":   our_sell,
            "our_buy":    our_buy,
            "updated_at": int(time.time()),
        }
        with open(CURRENT_PRICE_JSON, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("[PRICE_ENGINE] price JSON write error: %s", e)


def init_price_json():
    """
    Merge current_rates DB into current_price.json on startup.
    Adds any currencies missing from the file without overwriting fresh prices
    already tracked by the price_monitor.  Safe to call multiple times.
    """
    # Load whatever is already in the file (may be empty or partial)
    existing: dict = {}
    try:
        if os.path.exists(CURRENT_PRICE_JSON):
            with open(CURRENT_PRICE_JSON) as f:
                existing = json.load(f) or {}
    except Exception:
        existing = {}

    # Pull all rows from current_rates
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT currency, raw_source, our_sell, our_buy FROM current_rates"
        ).fetchall()
        conn.close()
    except Exception as e:
        log.warning("[PRICE_ENGINE] init_price_json DB error: %s", e)
        return

    added = []
    for currency, ref, our_sell, our_buy in rows:
        if not ref:
            continue
        if currency not in existing:
            # Only add currencies not already tracked by price_monitor
            existing[currency] = {
                "price":      ref,
                "our_sell":   our_sell,
                "our_buy":    our_buy,
                "updated_at": int(time.time()),
            }
            added.append(currency)

    if added:
        try:
            with open(CURRENT_PRICE_JSON, "w") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            log.info("[PRICE_ENGINE] price JSON: added %s from DB", added)
        except Exception as e:
            log.warning("[PRICE_ENGINE] init_price_json write error: %s", e)


# Seed on module load so any importer gets a valid JSON immediately
init_price_json()


def init_price_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_prices (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            currency       TEXT,
            source         TEXT,
            sell_price     INTEGER,
            buy_price      INTEGER,
            message_date   TEXT,
            confidence     REAL DEFAULT 1.0,
            payment_method TEXT DEFAULT 'general',
            created_at     INTEGER
        )
    """)
    for col, dflt in [("confidence", "1.0"), ("payment_method", "'general'")]:
        try:
            cur.execute(f"ALTER TABLE market_prices ADD COLUMN {col} REAL DEFAULT {dflt}")
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
    for col in ("raw_source INTEGER", "best_sell INTEGER", "best_buy INTEGER"):
        try:
            cur.execute(f"ALTER TABLE current_rates ADD COLUMN {col}")
        except Exception:
            pass
    # Per-method rates table for USD/CAD
    cur.execute("""
        CREATE TABLE IF NOT EXISTS current_method_rates (
            currency       TEXT,
            payment_method TEXT,
            our_sell       INTEGER,
            our_buy        INTEGER,
            avg_comp_sell  INTEGER,
            avg_comp_buy   INTEGER,
            updated_at     INTEGER,
            PRIMARY KEY (currency, payment_method)
        )
    """)
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
    message_date=None, confidence=1.0,
    payment_method="general",
):
    init_price_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO market_prices
            (currency, source, sell_price, buy_price, message_date,
             confidence, payment_method, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        currency,
        source,
        int(sell_price) if sell_price else None,
        int(buy_price)  if buy_price  else None,
        message_date,
        float(confidence),
        payment_method,
        int(time.time()),
    ))
    conn.commit()
    conn.close()


def _get_avg_sell(currency, method=None):
    """Average competitor sell price from fresh channel data."""
    init_price_db()
    now = int(time.time())
    max_age = MAX_PRICE_AGE_MINUTES * 60
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if method and method != "general":
        cur.execute("""
            SELECT sell_price FROM market_prices
            WHERE currency = ? AND payment_method = ?
              AND created_at >= ? AND sell_price IS NOT NULL AND confidence >= 0.5
        """, (currency, method, now - max_age))
    else:
        cur.execute("""
            SELECT sell_price FROM market_prices
            WHERE currency = ?
              AND created_at >= ? AND sell_price IS NOT NULL AND confidence >= 0.5
        """, (currency, now - max_age))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return None
    return int(sum(r[0] for r in rows) / len(rows))


def _get_avg_buy(currency, method=None):
    """Average competitor buy price from fresh channel data."""
    init_price_db()
    now = int(time.time())
    max_age = MAX_PRICE_AGE_MINUTES * 60
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if method and method != "general":
        cur.execute("""
            SELECT buy_price FROM market_prices
            WHERE currency = ? AND payment_method = ?
              AND created_at >= ? AND buy_price IS NOT NULL AND confidence >= 0.5
        """, (currency, method, now - max_age))
    else:
        cur.execute("""
            SELECT buy_price FROM market_prices
            WHERE currency = ?
              AND created_at >= ? AND buy_price IS NOT NULL AND confidence >= 0.5
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


def calculate_rate(currency, amount=100, method=None):
    """
    Compute our buy/sell rates using currency-specific spread rules.

    USDT:
      our_sell = @tetherpriceFa sell price  (no markup — match market)
      our_buy  = @tetherpriceFa sell price - USDT_BUY_SPREAD (4000)

    USD/CAD:
      our_sell = competitor avg_sell - USD_CAD_SELL_MARGIN (200)
      our_buy  = competitor avg_buy  + USD_CAD_BUY_MARGIN  (200)
      Falls back to avg_sell - 4000 when no buy prices scraped yet.

    Returns None if no live data — never invents prices.
    """
    if currency == "USDT":
        ref = _get_avg_sell("USDT")
        if not ref:
            return None
        our_sell = ref
        our_buy  = ref - USDT_BUY_SPREAD
        source_label = "tetherpriceFa"
    else:
        avg_sell = _get_avg_sell(currency, method)
        if currency == "USD" and not avg_sell:
            avg_sell = _get_avg_sell("USDT")   # USDT ≈ USD fallback
            source_label = "USDT→USD"
        elif currency == "CAD" and not avg_sell:
            usdt = _get_avg_sell("USDT")
            avg_sell = int(usdt / USD_CAD_RATE) if usdt else None
            source_label = "USDT→CAD"
        else:
            source_label = currency

        if not avg_sell:
            return None

        avg_buy = _get_avg_buy(currency, method)
        ref      = avg_sell
        our_sell = avg_sell - USD_CAD_SELL_MARGIN
        our_buy  = (avg_buy + USD_CAD_BUY_MARGIN) if avg_buy else (avg_sell - USDT_BUY_SPREAD)

    margin = get_margin_by_amount(amount)

    init_price_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO current_rates
            (currency, raw_source, our_sell, our_buy, updated_at)
        VALUES (?, ?, ?, ?, ?)
    """, (currency, ref, our_sell, our_buy, int(time.time())))
    if method:
        cur.execute("""
            INSERT OR REPLACE INTO current_method_rates
                (currency, payment_method, our_sell, our_buy,
                 avg_comp_sell, avg_comp_buy, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (currency, method, our_sell, our_buy,
              ref, _get_avg_buy(currency, method), int(time.time())))
    conn.commit()
    conn.close()

    _write_price_json(currency, ref, our_sell, our_buy)

    return {
        "currency":     currency,
        "amount":       amount,
        "margin":       margin,
        "raw_source":   ref,
        "source_label": source_label,
        "our_sell":     our_sell,
        "our_buy":      our_buy,
        "payment_method": method or "general",
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
