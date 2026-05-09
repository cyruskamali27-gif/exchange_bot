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
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
ADMIN_APPROVAL_THRESHOLD = int(os.environ.get("ADMIN_APPROVAL_THRESHOLD", 1000))
EXCLUDED_IDS = []

BUY_SPREAD_TOMAN  = int(os.environ.get("BUY_SPREAD_TOMAN",  4000))
SELL_SPREAD_TOMAN = int(os.environ.get("SELL_SPREAD_TOMAN",  500))

# ─── ElevenLabs Conversational AI ────────────────────────────────
ELEVENLABS_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "agent_1101kr77twbhe1p9c7mpf1b7z2e3")

# ─── Office ambience mixing ───────────────────────────────────────
ENABLE_OFFICE_AMBIENCE  = os.environ.get("ENABLE_OFFICE_AMBIENCE", "true").lower() == "true"
OFFICE_AMBIENCE_FILE    = os.environ.get("OFFICE_AMBIENCE_FILE", "/var/www/exchange_bot/assets/office_ambience.mp3")
OFFICE_AMBIENCE_VOLUME  = float(os.environ.get("OFFICE_AMBIENCE_VOLUME", "0.04"))

TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER  = os.environ.get("TWILIO_FROM_NUMBER", "")

# ─── Safe / test mode ────────────────────────────────────────────
SAFE_MODE        = os.environ.get("SAFE_MODE", "false").lower() == "true"
TEST_GROUP_ONLY  = os.environ.get("TEST_GROUP_ONLY", "false").lower() == "true"
TEST_GROUP_ID    = int(os.environ.get("TEST_GROUP_ID", "0"))

# ─── Voice reply flags ────────────────────────────────────────────
# Disabled by default: current ElevenLabs voice sounds non-Iranian.
# Set VOICE_REPLIES_ENABLED=true in .env only after accent is verified.
VOICE_REPLIES_ENABLED       = os.environ.get("VOICE_REPLIES_ENABLED", "false").lower() == "true"
PRICE_REPLIES_ALWAYS_TEXT   = os.environ.get("PRICE_REPLIES_ALWAYS_TEXT", "true").lower() == "true"
DISABLE_VOICE_IF_ACCENT_BAD = os.environ.get("DISABLE_VOICE_IF_ACCENT_BAD", "true").lower() == "true"

# ─── Hume AI emotion intelligence ────────────────────────────────
HUME_API_KEY        = os.environ.get("HUME_API_KEY", "")
HUME_SECRET_KEY     = os.environ.get("HUME_SECRET_KEY", "")
ENABLE_HUME_EMOTION = os.environ.get("ENABLE_HUME_EMOTION", "false").lower() == "true"

# ─── Pricing constants ────────────────────────────────────────────
USD_CAD_RATE         = float(os.environ.get("USD_CAD_RATE", "1.36"))
MARKET_SPREAD_TOMAN  = int(os.environ.get("MARKET_SPREAD_TOMAN", "500"))
MAX_PRICE_AGE_MINUTES = int(os.environ.get("MAX_PRICE_AGE_MINUTES", "60"))

# ─── Telegram price source channels ──────────────────────────────
PRICE_CHANNELS_USDT = ["tetherpriceFa", "tether_dollar71"]
PRICE_CHANNELS_USD  = ["tahran_sabza", "SarafiBahmaniCa", "ApadanaCurrencyExchange", "hanaexchange"]
PRICE_CHANNELS_ALL  = PRICE_CHANNELS_USDT + PRICE_CHANNELS_USD
