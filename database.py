import sqlite3
from datetime import datetime

DB_FILE = "/var/www/exchange_bot/exchange.db"

def _migrate_column(conn, table, col, definition):
    """Add column if it doesn't already exist."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    except Exception:
        pass


def init_extended_tables(conn):
    """Create all new tables and migrate existing ones."""
    c = conn.cursor()

    # ── Augment customer_profiles with new fields ──────────────────
    for col, defn in [
        ("mood",               "TEXT DEFAULT 'unknown'"),
        ("emotion",            "TEXT"),
        ("prefers_voice",      "INTEGER DEFAULT 0"),
        ("is_vip",             "INTEGER DEFAULT 0"),
        ("deal_count",         "INTEGER DEFAULT 0"),
        ("negotiation_style",  "TEXT DEFAULT 'unknown'"),
        ("trust_level",        "REAL DEFAULT 0.5"),
        ("banned",             "INTEGER DEFAULT 0"),
    ]:
        _migrate_column(conn, "customer_profiles", col, defn)

    c.execute("""CREATE TABLE IF NOT EXISTS conversation_memory (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          TEXT,
        role             TEXT,
        message          TEXT,
        timestamp        TEXT,
        was_voice        INTEGER DEFAULT 0,
        deal_amount      REAL,
        emotion_detected TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS learned_mistakes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        mistake_type    TEXT,
        original_reply  TEXT,
        corrected_reply TEXT,
        admin_id        TEXT,
        created_at      TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS successful_patterns (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern_text  TEXT UNIQUE,
        success_count INTEGER DEFAULT 1,
        customer_mood TEXT,
        last_used     TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS failed_patterns (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern_text  TEXT UNIQUE,
        fail_count    INTEGER DEFAULT 1,
        customer_mood TEXT,
        last_used     TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS pricing_decisions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          TEXT,
        amount_cad       REAL,
        price_toman      REAL,
        source           TEXT,
        customer_accepted INTEGER DEFAULT 0,
        created_at       TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS daily_learning (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        date                     TEXT UNIQUE,
        total_deals              INTEGER DEFAULT 0,
        success_rate             REAL DEFAULT 0,
        avg_negotiation_rounds   REAL DEFAULT 0,
        common_emotions          TEXT,
        notes                    TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS customer_preferences (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          TEXT UNIQUE,
        prefers_voice    INTEGER DEFAULT 0,
        language_style   TEXT,
        best_contact_time TEXT,
        notes            TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS voice_assignments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT UNIQUE,
        voice_id    TEXT,
        assigned_at TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS emotion_history (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id               TEXT,
        emotion               TEXT,
        dominant_emotion      TEXT,
        urgency_score         REAL,
        trust_score           REAL,
        suspicion_score       REAL,
        negotiation_pressure  REAL,
        emotional_energy      REAL,
        emotional_stability   REAL,
        emotion_confidence    REAL,
        source                TEXT DEFAULT 'rule_based',
        detected_at           TEXT,
        conversation_id       INTEGER
    )""")

    conn.commit()


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT DEFAULT "pending",
        type TEXT,
        customer_name TEXT,
        customer_telegram TEXT,
        customer_phone TEXT,
        amount_cad REAL,
        amount_usdt REAL,
        amount_toman REAL,
        interac_email TEXT,
        bank_account TEXT,
        country TEXT DEFAULT "IR",
        created_at TEXT,
        updated_at TEXT,
        notes TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS balance (
        id INTEGER PRIMARY KEY,
        usdt_balance REAL DEFAULT 0,
        updated_at TEXT)''')
    c.execute('INSERT OR IGNORE INTO balance (id, usdt_balance, updated_at) VALUES (1, 0, ?)',
              (datetime.now().isoformat(),))
    c.execute('''CREATE TABLE IF NOT EXISTS message_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name TEXT,
        user_id TEXT,
        username TEXT,
        message TEXT,
        type TEXT,
        amount_cad REAL,
        processed INTEGER DEFAULT 0,
        created_at TEXT)''')
    init_extended_tables(conn)
    conn.commit()
    conn.close()

def add_deal(type, customer_name, customer_telegram, amount_cad, amount_usdt, amount_toman, bank_account="", country="IR"):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('INSERT INTO deals (type,customer_name,customer_telegram,amount_cad,amount_usdt,amount_toman,bank_account,country,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
              (type,customer_name,customer_telegram,amount_cad,amount_usdt,amount_toman,bank_account,country,now,now))
    deal_id = c.lastrowid
    conn.commit()
    conn.close()
    return deal_id

def update_deal_status(deal_id, status, notes=""):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE deals SET status=?,notes=?,updated_at=? WHERE id=?',
              (status,notes,datetime.now().isoformat(),deal_id))
    conn.commit()
    conn.close()

def get_deal(deal_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM deals WHERE id=?',(deal_id,))
    row = c.fetchone()
    conn.close()
    if row:
        cols=['id','status','type','customer_name','customer_telegram','customer_phone','amount_cad','amount_usdt','amount_toman','interac_email','bank_account','country','created_at','updated_at','notes']
        return dict(zip(cols,row))
    return None

def get_pending_deals():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM deals WHERE status="pending" ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    cols=['id','status','type','customer_name','customer_telegram','customer_phone','amount_cad','amount_usdt','amount_toman','interac_email','bank_account','country','created_at','updated_at','notes']
    return [dict(zip(cols,row)) for row in rows]

def get_today_stats():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute('SELECT COUNT(*),SUM(amount_cad) FROM deals WHERE status="completed" AND created_at LIKE ?',(f"{today}%",))
    row = c.fetchone()
    count = row[0] or 0
    total_cad = row[1] or 0
    from config import MARGIN_CAD
    conn.close()
    return {"count":count,"total_cad":total_cad,"profit":count*MARGIN_CAD}

def get_usdt_balance():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT usdt_balance FROM balance WHERE id=1')
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def log_message(group_name,user_id,username,message,msg_type,amount_cad=0):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO message_log (group_name,user_id,username,message,type,amount_cad,created_at) VALUES (?,?,?,?,?,?,?)',
              (group_name,str(user_id),username,message,msg_type,amount_cad,datetime.now().isoformat()))
    conn.commit()
    conn.close()
