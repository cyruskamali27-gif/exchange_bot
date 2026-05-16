import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
IRAN_AGENT_ID = int(os.environ["IRAN_AGENT_ID"])
TURKEY_AGENT_ID = int(os.environ["TURKEY_AGENT_ID"]) if os.environ.get("TURKEY_AGENT_ID") else None
CHINA_AGENT_ID = int(os.environ["CHINA_AGENT_ID"]) if os.environ.get("CHINA_AGENT_ID") else None
TRON_WALLET = os.environ.get("TRON_WALLET", "")
TARGET_GROUPS = [
    int(g) if g.lstrip("-").isdigit() else g
    for g in os.environ.get("TARGET_GROUPS", "").split(",")
    if g
]
MARGIN_CAD = int(os.environ.get("MARGIN_CAD", 4))
TELETHON_API_ID = os.environ["TELETHON_API_ID"]
TELETHON_API_HASH = os.environ["TELETHON_API_HASH"]
TELETHON_PHONE = os.environ["TELETHON_PHONE"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TRON_API_KEY = os.environ.get("TRON_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "")
ADMIN_APPROVAL_THRESHOLD = int(os.environ.get("ADMIN_APPROVAL_THRESHOLD", 1000))
EXCLUDED_IDS = []

ALLOWED_TELEGRAM_CHAT = os.environ.get("ALLOWED_TELEGRAM_CHAT", "@cyrusGlobalExchange")

BUY_SPREAD_TOMAN  = int(os.environ.get("BUY_SPREAD_TOMAN",  4000))
SELL_SPREAD_TOMAN = int(os.environ.get("SELL_SPREAD_TOMAN",  500))

# ─── Per-currency spread rules ────────────────────────────────────
# USDT: our sell = channel price (0 spread), our buy = channel price - 4000
USDT_BUY_SPREAD      = int(os.environ.get("USDT_BUY_SPREAD", "4000"))
# USD/CAD: our sell = competitor avg sell - 200, our buy = competitor avg buy + 200
USD_CAD_SELL_MARGIN  = int(os.environ.get("USD_CAD_SELL_MARGIN", "200"))
USD_CAD_BUY_MARGIN   = int(os.environ.get("USD_CAD_BUY_MARGIN",  "200"))

# ─── Business hours (Toronto) — USD/CAD only ─────────────────────
TIMEZONE             = os.environ.get("TIMEZONE", "America/Toronto")
USD_CAD_OPEN_HOUR    = int(os.environ.get("USD_CAD_OPEN_HOUR",  "9"))
USD_CAD_CLOSE_HOUR   = int(os.environ.get("USD_CAD_CLOSE_HOUR", "17"))


# ─── Safe / test mode ────────────────────────────────────────────
SAFE_MODE        = os.environ.get("SAFE_MODE", "false").lower() == "true"
TEST_GROUP_ONLY  = os.environ.get("TEST_GROUP_ONLY", "false").lower() == "true"
TEST_GROUP_ID    = int(os.environ.get("TEST_GROUP_ID", "0"))


# ─── Hume AI emotion intelligence ────────────────────────────────
HUME_API_KEY        = os.environ.get("HUME_API_KEY", "")
HUME_SECRET_KEY     = os.environ.get("HUME_SECRET_KEY", "")
ENABLE_HUME_EMOTION = os.environ.get("ENABLE_HUME_EMOTION", "false").lower() == "true"

# ─── Pricing constants ────────────────────────────────────────────
USD_CAD_RATE         = float(os.environ.get("USD_CAD_RATE", "1.36"))
MARKET_SPREAD_TOMAN  = int(os.environ.get("MARKET_SPREAD_TOMAN", "500"))
MAX_PRICE_AGE_MINUTES = int(os.environ.get("MAX_PRICE_AGE_MINUTES", "60"))

# ─── Telegram price source channels ──────────────────────────────
# USDT: only @tetherpriceFa — posts the live SELL price
PRICE_CHANNELS_USDT    = ["tetherpriceFa"]
# USD/CAD: tether_dollar71 posts structured text with USD/CAD hourly
# (SarafiBahmaniCa, ApadanaCurrencyExchange, hanaexchange removed — photo-only, no price text)
PRICE_CHANNELS_USD_CAD = ["tether_dollar71"]
PRICE_CHANNELS_USD     = PRICE_CHANNELS_USD_CAD   # backward compat
PRICE_CHANNELS_CAD     = PRICE_CHANNELS_USD_CAD
PRICE_CHANNELS_ALL     = PRICE_CHANNELS_USDT + PRICE_CHANNELS_USD_CAD
