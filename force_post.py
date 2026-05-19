#!/usr/bin/env python3
"""Force an immediate rate fetch and post all posters via Bot API (no second Telethon client)."""
import asyncio
import sys
sys.path.insert(0, "/var/www/exchange_bot")

from rate_graphic_publisher import (
    scan_and_post, TORONTO_TZ, TEMPLATES_DIR, TEMPLATE_FILES, OUTPUT_FILES, PUBLISH_ORDER
)
from datetime import datetime

async def main():
    now = datetime.now(TORONTO_TZ)
    print(f"=== Force Post — Cyrus Global Exchange ===")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M %Z')}\n")

    print("Template files:")
    all_ok = True
    for cur, fname in TEMPLATE_FILES.items():
        path = TEMPLATES_DIR / fname
        ok = path.exists()
        status = "OK" if ok else "MISSING"
        size = f"{path.stat().st_size:,} B" if ok else ""
        print(f"  {cur:6s}  {status:7s}  {size}")
        if not ok:
            all_ok = False

    if not all_ok:
        print("\nABORT: one or more templates missing")
        sys.exit(1)

    print("\nPosting (Bot API — no Telethon session conflict)...\n")
    # client param unused inside scan_and_post; posting is via Bot API
    await scan_and_post(None, force=True)
    print("\n=== Done ===")

if __name__ == "__main__":
    asyncio.run(main())
