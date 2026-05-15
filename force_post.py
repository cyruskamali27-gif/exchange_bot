#!/usr/bin/env python3
"""Force an immediate rate fetch and post all 5 posters."""
import asyncio
import sys
sys.path.insert(0, "/var/www/exchange_bot")

from rate_graphic_publisher import (
    run_publisher, scan_and_post, TORONTO_TZ, TEMPLATES
)
from telethon import TelegramClient
from config import TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE
from pathlib import Path
from datetime import datetime

SESSION = "rate_publisher"

async def main():
    print("=== Force Post — Cyrus Global Exchange ===")
    print("\nTemplate files:")
    for cur, path in TEMPLATES.items():
        exists = "✅" if path.exists() else "❌ MISSING"
        size = f"{path.stat().st_size:,} bytes" if path.exists() else ""
        print(f"  {cur}: {path}  {exists}  {size}")

    client = TelegramClient(SESSION, int(TELETHON_API_ID), TELETHON_API_HASH)
    await client.start(phone=TELETHON_PHONE)
    print("\nTelethon connected — forcing post...\n")
    await scan_and_post(client, force=True)
    await client.disconnect()
    print("\n=== Done ===")

if __name__ == "__main__":
    asyncio.run(main())
