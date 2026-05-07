import sqlite3
from datetime import datetime

DB_FILE = "/var/www/exchange_bot/exchange.db"

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
