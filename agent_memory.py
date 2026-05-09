import sqlite3
from datetime import datetime, date

DB_FILE = "/var/www/exchange_bot/exchange.db"


def _init():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS agent_learning_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT,
        customer_message TEXT,
        agent_reply TEXT,
        channel     TEXT DEFAULT "text",
        situation   TEXT DEFAULT "general",
        tone        TEXT DEFAULT "friendly",
        deal_outcome TEXT DEFAULT "pending",
        admin_feedback TEXT,
        customer_tone TEXT DEFAULT "neutral",
        reply_style TEXT,
        created_at  TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS customer_profiles (
        user_id          TEXT PRIMARY KEY,
        username         TEXT,
        profile_type     TEXT DEFAULT "unknown",
        total_conversations INTEGER DEFAULT 0,
        total_deals      INTEGER DEFAULT 0,
        successful_deals INTEGER DEFAULT 0,
        last_seen        TEXT,
        notes            TEXT,
        created_at       TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS admin_feedback (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        log_id      INTEGER,
        user_id     TEXT,
        feedback_type TEXT,
        admin_note  TEXT,
        created_at  TEXT
    )''')

    conn.commit()
    conn.close()


_init()


# ─── Conversation Logging ────────────────────────────────────────────────────

def log_conversation(user_id, customer_message, agent_reply,
                     channel="text", situation="general",
                     tone="friendly", customer_tone="neutral"):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        '''INSERT INTO agent_learning_log
           (user_id, customer_message, agent_reply, channel,
            situation, tone, customer_tone, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (str(user_id), customer_message, agent_reply,
         channel, situation, tone, customer_tone,
         datetime.now().isoformat())
    )
    log_id = c.lastrowid
    conn.commit()
    conn.close()
    return log_id


def save_experience(user_msg: str, ai_reply: str, success: bool = True, user_id: str = "unknown"):
    """Record an interaction outcome for learning. Wraps log_conversation + update_deal_outcome."""
    outcome = "success" if success else "failed"
    log_id = log_conversation(
        user_id=user_id,
        customer_message=user_msg,
        agent_reply=ai_reply,
        situation="deal" if success else "no_deal",
        tone="friendly",
    )
    update_deal_outcome(log_id, outcome)
    return log_id


def update_deal_outcome(log_id, outcome):
    if not log_id:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE agent_learning_log SET deal_outcome=? WHERE id=?',
              (outcome, log_id))
    conn.commit()
    conn.close()


def get_last_log_id(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT id FROM agent_learning_log WHERE user_id=? ORDER BY id DESC LIMIT 1',
        (str(user_id),)
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


# ─── Admin Feedback ──────────────────────────────────────────────────────────

def save_admin_feedback(log_id, user_id, feedback_type, admin_note=""):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        '''INSERT INTO admin_feedback
           (log_id, user_id, feedback_type, admin_note, created_at)
           VALUES (?, ?, ?, ?, ?)''',
        (log_id, str(user_id), feedback_type, admin_note,
         datetime.now().isoformat())
    )
    c.execute(
        'UPDATE agent_learning_log SET admin_feedback=? WHERE id=?',
        (feedback_type, log_id)
    )
    conn.commit()
    conn.close()


# ─── Customer Profiles ───────────────────────────────────────────────────────

def get_or_create_customer_profile(user_id, username=""):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM customer_profiles WHERE user_id=?', (str(user_id),))
    row = c.fetchone()
    if not row:
        now = datetime.now().isoformat()
        c.execute(
            '''INSERT INTO customer_profiles
               (user_id, username, profile_type, total_conversations,
                total_deals, successful_deals, last_seen, created_at)
               VALUES (?, ?, "unknown", 0, 0, 0, ?, ?)''',
            (str(user_id), username or "", now, now)
        )
        conn.commit()
        c.execute('SELECT * FROM customer_profiles WHERE user_id=?', (str(user_id),))
        row = c.fetchone()
    cols = ['user_id', 'username', 'profile_type', 'total_conversations',
            'total_deals', 'successful_deals', 'last_seen', 'notes', 'created_at']
    conn.close()
    return dict(zip(cols, row))


def update_customer_profile(user_id, **kwargs):
    if not kwargs:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [str(user_id)]
    c.execute(f'UPDATE customer_profiles SET {sets} WHERE user_id=?', vals)
    conn.commit()
    conn.close()


def increment_customer_conversations(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        '''UPDATE customer_profiles SET
           total_conversations = total_conversations + 1,
           last_seen = ?
           WHERE user_id=?''',
        (datetime.now().isoformat(), str(user_id))
    )
    conn.commit()
    conn.close()


def get_all_customer_profiles(limit=20):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        '''SELECT user_id, username, profile_type, total_conversations,
                  successful_deals, last_seen
           FROM customer_profiles
           ORDER BY total_conversations DESC
           LIMIT ?''',
        (limit,)
    )
    rows = c.fetchall()
    conn.close()
    cols = ['user_id', 'username', 'profile_type',
            'total_conversations', 'successful_deals', 'last_seen']
    return [dict(zip(cols, r)) for r in rows]


# ─── Learning / Reply Style ──────────────────────────────────────────────────

def get_best_reply_style(situation):
    """Return the tone that historically gets best feedback for this situation."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        '''SELECT tone, COUNT(*) as cnt
           FROM agent_learning_log
           WHERE situation=?
             AND (admin_feedback="good_reply" OR deal_outcome="success")
           GROUP BY tone
           ORDER BY cnt DESC
           LIMIT 1''',
        (situation,)
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else "friendly"


# ─── Customer Tone Detection ─────────────────────────────────────────────────

def detect_customer_tone(message):
    if not message:
        return "neutral"
    impatient = ["چرا", "کجاست", "کِی", "هنوز", "دیر", "زود", "صبر کن", "معطل"]
    angry = ["کلاهبرد", "دزد", "مزخرف", "دروغ", "نمی‌دم", "پس می‌گیرم"]
    friendly = ["ممنون", "مرسی", "خوب", "عالی", "باشه", "ممنونم", "دست گلت"]
    if any(w in message for w in angry):
        return "angry"
    if any(w in message for w in impatient):
        return "impatient"
    if any(w in message for w in friendly):
        return "friendly"
    return "neutral"


# ─── Daily Report ────────────────────────────────────────────────────────────

def get_daily_report():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = date.today().isoformat()

    def _count(query, *args):
        c.execute(query, args)
        return c.fetchone()[0] or 0

    total = _count(
        "SELECT COUNT(*) FROM agent_learning_log WHERE created_at LIKE ?",
        f"{today}%"
    )
    successful = _count(
        "SELECT COUNT(*) FROM agent_learning_log WHERE deal_outcome='success' AND created_at LIKE ?",
        f"{today}%"
    )
    failed = _count(
        "SELECT COUNT(*) FROM agent_learning_log WHERE deal_outcome='failed' AND created_at LIKE ?",
        f"{today}%"
    )
    robotic = _count(
        "SELECT COUNT(*) FROM admin_feedback WHERE feedback_type='too_robotic' AND created_at LIKE ?",
        f"{today}%"
    )
    good = _count(
        "SELECT COUNT(*) FROM admin_feedback WHERE feedback_type='good_reply' AND created_at LIKE ?",
        f"{today}%"
    )
    bad = _count(
        "SELECT COUNT(*) FROM admin_feedback WHERE feedback_type='bad_reply' AND created_at LIKE ?",
        f"{today}%"
    )

    c.execute(
        '''SELECT customer_message, COUNT(*) as cnt
           FROM agent_learning_log
           WHERE deal_outcome='failed' AND created_at LIKE ?
           GROUP BY customer_message
           ORDER BY cnt DESC
           LIMIT 3''',
        (f"{today}%",)
    )
    objections = c.fetchall()

    new_customers = _count(
        "SELECT COUNT(*) FROM customer_profiles WHERE created_at LIKE ?",
        f"{today}%"
    )

    conn.close()
    return {
        "total_conversations": total,
        "successful_deals": successful,
        "failed_deals": failed,
        "robotic_replies": robotic,
        "good_replies": good,
        "bad_replies": bad,
        "new_customers": new_customers,
        "common_objections": objections,
    }
