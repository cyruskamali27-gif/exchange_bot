"""
contacted_users.py — Anti-spam with 24-hour per-user cooldown.
Cooldown persists in SQLite (same exchange.db).
"""
import sqlite3
from datetime import datetime, timedelta

DB_FILE = "/var/www/exchange_bot/exchange.db"
COOLDOWN_HOURS = 24


def _init():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacted_users (
            user_id       INTEGER PRIMARY KEY,
            contacted_at  TEXT,
            cooldown_until TEXT
        )
    """)
    # Migrate: add cooldown_until if missing
    try:
        conn.execute("ALTER TABLE contacted_users ADD COLUMN cooldown_until TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()


_init()


def is_contacted(user_id: int) -> bool:
    """
    Return True if the user was contacted recently (within cooldown window).
    Returns False after 24 h so they can be re-contacted.
    """
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT cooldown_until FROM contacted_users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if not row:
        return False
    cooldown_until = row[0]
    if not cooldown_until:
        return True  # legacy rows with no cooldown = treat as permanent
    try:
        until = datetime.fromisoformat(cooldown_until)
        return datetime.now() < until
    except Exception:
        return True


def add(user_id: int):
    """Record a contact. Resets the 24-hour cooldown window."""
    now     = datetime.now()
    until   = (now + timedelta(hours=COOLDOWN_HOURS)).isoformat()
    conn    = sqlite3.connect(DB_FILE)
    conn.execute(
        """INSERT INTO contacted_users (user_id, contacted_at, cooldown_until)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET contacted_at=excluded.contacted_at,
                                               cooldown_until=excluded.cooldown_until""",
        (user_id, now.isoformat(), until)
    )
    conn.commit()
    conn.close()
