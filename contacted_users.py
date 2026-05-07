import sqlite3
from datetime import datetime

DB_FILE = "/var/www/exchange_bot/exchange.db"


def _init():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacted_users (
            user_id INTEGER PRIMARY KEY,
            contacted_at TEXT
        )
    """)
    conn.commit()
    conn.close()


_init()


def is_contacted(user_id):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT 1 FROM contacted_users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None


def add(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT OR IGNORE INTO contacted_users (user_id, contacted_at) VALUES (?, ?)",
        (user_id, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
