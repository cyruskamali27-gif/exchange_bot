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

BUY_SPREAD_TOMAN = int(os.environ.get("BUY_SPREAD_TOMAN", 4000))
