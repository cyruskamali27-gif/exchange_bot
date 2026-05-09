"""
TEST MODE — SAFE MODE
━━━━━━━━━━━━━━━━━━━━
Only monitors the TEST group: -5124476784
No trading logic. No pricing. No voice. No private chat replies.
No other groups, channels, or chats are touched.
"""

import asyncio
import logging
from datetime import datetime, timezone

from telethon import TelegramClient, events

from config import (
    TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE,
    SAFE_MODE, TEST_GROUP_ONLY, TEST_GROUP_ID,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TEST] %(message)s",
    datefmt="%H:%M:%S",
)

# ── Hard-coded safety guard ──────────────────────────────────────
assert SAFE_MODE,       "SAFE_MODE must be true to run test_scanner.py"
assert TEST_GROUP_ONLY, "TEST_GROUP_ONLY must be true to run test_scanner.py"
assert TEST_GROUP_ID,   "TEST_GROUP_ID must be set to run test_scanner.py"

print("=" * 55)
print("  ⚠️  SAFE MODE — TEST SCANNER ONLY")
print(f"  Group  : {TEST_GROUP_ID}")
print(f"  Source : https://t.me/+F2LHuaVTA3U5Nzc8")
print("  Live trading  : DISABLED")
print("  Auto pricing  : DISABLED")
print("  Voice replies : DISABLED")
print("  Private chats : IGNORED")
print("  Other groups  : IGNORED")
print("=" * 55)


async def run():
    client = TelegramClient("exchange_agent", int(TELETHON_API_ID), TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)

    me = await client.get_me()
    print(f"✅ Connected as: {me.first_name} (+{me.phone})")
    print(f"✅ Watching test group: {TEST_GROUP_ID}")
    print("─" * 55)

    @client.on(events.NewMessage)
    async def handler(event):
        # ── SAFE GUARD: only process the test group ──────────────
        chat_id = event.chat_id
        if chat_id != TEST_GROUP_ID:
            return  # silently ignore everything else

        msg   = event.message
        text  = msg.text or msg.message or "(media/no text)"
        sender = await event.get_sender()
        name  = getattr(sender, "first_name", None) or getattr(sender, "username", "unknown")
        ts    = datetime.now().strftime("%H:%M:%S")

        print(f"[{ts}] 📨 {name} ({sender.id}): {text[:120]}")

        # ── TEST REPLY: only if message is addressed to the bot ──
        # Reply to any message that starts with /test or پیام آزمایش
        triggers = ["/test", "پیام آزمایش", "تست", "test"]
        if any(t in text.lower() for t in triggers) and not event.out:
            reply = (
                "✅ SAFE MODE فعاله.\n"
                f"گروه تست: `{TEST_GROUP_ID}`\n"
                "هیچ منطق معاملاتی، قیمت‌دهی، یا ارسال پیام خصوصی انجام نمی‌شه."
            )
            await event.reply(reply)
            print(f"[{ts}] ↩️  replied in test group")

    print("👂 Listening for messages in test group only...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(run())
