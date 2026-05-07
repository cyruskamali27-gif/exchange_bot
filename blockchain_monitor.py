import asyncio
import requests
import logging
from datetime import datetime
from telegram import Bot
from config import BOT_TOKEN, ADMIN_ID, IRAN_AGENT_ID, TRON_WALLET

logging.basicConfig(level=logging.INFO)

def get_recent_transactions(wallet_address):
    try:
        url = f"https://api.trongrid.io/v1/accounts/{wallet_address}/transactions/trc20"
        params = {"limit": 10, "only_confirmed": True}
        r = requests.get(url, params=params, timeout=10)
        return r.json().get("data", [])
    except:
        return []

def parse_transaction(tx):
    try:
        token = tx.get("token_info", {})
        if token.get("symbol") != "USDT":
            return None
        decimals = int(token.get("decimals", 6))
        amount = int(tx.get("value", 0)) / (10 ** decimals)
        return {
            "tx_id": tx.get("transaction_id", ""),
            "from": tx.get("from", ""),
            "to": tx.get("to", ""),
            "amount_usdt": round(amount, 2)
        }
    except:
        return None

async def monitor_wallet():
    bot = Bot(token=BOT_TOKEN)
    seen_txs = set()
    existing = get_recent_transactions(TRON_WALLET)
    for tx in existing:
        parsed = parse_transaction(tx)
        if parsed:
            seen_txs.add(parsed["tx_id"])
    print(f"✅ Blockchain Monitor شروع شد")
    while True:
        try:
            transactions = get_recent_transactions(TRON_WALLET)
            for tx in transactions:
                parsed = parse_transaction(tx)
                if not parsed:
                    continue
                if parsed["tx_id"] not in seen_txs and parsed["to"].lower() == TRON_WALLET.lower():
                    seen_txs.add(parsed["tx_id"])
                    amount_usdt = parsed["amount_usdt"]
                    print(f"💰 تراکنش جدید: {amount_usdt} USDT")
                    await bot.send_message(chat_id=ADMIN_ID, text=(
                        f"✅ تتر دریافت شد!\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"💰 مبلغ: {amount_usdt:,.2f} USDT\n"
                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                    ))
                    await bot.send_message(chat_id=IRAN_AGENT_ID, text=(
                        f"💰 تتر جدید دریافت شد\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"مبلغ: {amount_usdt:,.2f} USDT\n"
                        f"لطفاً اطلاعات واریز را بفرستید"
                    ))
        except Exception as e:
            logging.error(f"Monitor error: {e}")
        await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(monitor_wallet())
