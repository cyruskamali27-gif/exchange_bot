import asyncio
import re
import logging
from PIL import Image
import pytesseract
import io
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from config import TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE
from price_engine import save_market_price

logging.basicConfig(level=logging.INFO)

PRICE_CHANNELS = [
   "sarafimoneymex",
   "sarafijavdani",
   "NeoExAu",
   "SarafiBahmaniCa"
]

current_usdt_toman = {
   "price": 72000,
   "source": "default",
   "updated_at": None,
   "is_fresh": False
}

def is_price_fresh():
   if not current_usdt_toman["updated_at"]:
       return False
   age = datetime.now() - current_usdt_toman["updated_at"]
   return age < timedelta(minutes=5)

def extract_price_from_text(text):
   patterns = [
       r'تتر[:\s]*([۰-۹0-9,،]+)',
       r'USDT[:\s]*([۰-۹0-9,،]+)',
       r'usdt[:\s]*([۰-۹0-9,،]+)',
       r'([۰-۹0-9]{2,3}[,،][۰-۹0-9]{3})',
       r'([0-9]{2,3},[0-9]{3})',
   ]
   for pattern in patterns:
       matches = re.findall(pattern, text, re.IGNORECASE)
       for match in matches:
           clean = match.replace('،','').replace(',','')
           for fa, en in zip('۰۱۲۳۴۵۶۷۸۹','0123456789'):
               clean = clean.replace(fa, en)
           try:
               price = int(clean)
               if 50000 <= price <= 200000:
                   return price
           except:
               continue
   return None

async def read_price_from_image(image_bytes):
   try:
       img = Image.open(io.BytesIO(image_bytes))
       text_fa = pytesseract.image_to_string(img, lang='fas')
       text_en = pytesseract.image_to_string(img, lang='eng')
       return extract_price_from_text(text_fa + "\n" + text_en)
   except Exception as e:
       logging.error(f"OCR error: {e}")
       return None

async def fetch_latest_from_channels(client):
   """هر ۵ دقیقه آخرین پیام کانالها را چک کن"""
   while True:
       for channel in PRICE_CHANNELS:
           try:
               messages = await client.get_messages(channel, limit=3)
               for msg in messages:
                   msg_time = msg.date.replace(tzinfo=None)
                   age_minutes = (datetime.utcnow() - msg_time).total_seconds() / 60
                   if age_minutes > 5:
                       continue
                   price = None
                   if msg.photo:
                       image_bytes = await msg.download_media(bytes)
                       price = await read_price_from_image(image_bytes)
                   if not price and msg.text:
                       price = extract_price_from_text(msg.text)
                   if price:
                       current_usdt_toman["price"] = price
                       current_usdt_toman["source"] = channel
                       current_usdt_toman["updated_at"] = datetime.now()
                       current_usdt_toman["is_fresh"] = True
                       save_market_price("USDT", channel, sell_price=price)
                       print(f"💱 آپدیت ۵ دقیقه‌ای: {price:,} تومان از @{channel}")
                       break
           except Exception as e:
               logging.error(f"Fetch error {channel}: {e}")
       await asyncio.sleep(300)  # هر ۵ دقیقه

async def run_ocr_monitor():
   client = TelegramClient('exchange_agent', int(TELETHON_API_ID), TELETHON_API_HASH)
   await client.start(phone=TELETHON_PHONE)
   print("✅ OCR Price Monitor شروع شد - آپدیت هر ۵ دقیقه")

   @client.on(events.NewMessage(chats=PRICE_CHANNELS))
   async def handler(event):
       try:
           msg_time = event.message.date.replace(tzinfo=None)
           age_minutes = (datetime.utcnow() - msg_time).total_seconds() / 60
           if age_minutes > 5:
               return
           price = None
           source = getattr(event.chat, 'username', 'unknown')
           if event.photo:
               image_bytes = await event.download_media(bytes)
               price = await read_price_from_image(image_bytes)
           if not price and event.text:
               price = extract_price_from_text(event.text)
           if price:
               current_usdt_toman["price"] = price
               current_usdt_toman["source"] = source
               current_usdt_toman["updated_at"] = datetime.now()
               current_usdt_toman["is_fresh"] = True
               save_market_price("USDT", source, sell_price=price)
               print(f"💱 قیمت جدید: {price:,} تومان از @{source}")
       except Exception as e:
           logging.error(f"Handler error: {e}")

   asyncio.create_task(fetch_latest_from_channels(client))
   await client.run_until_disconnected()

def get_current_price():
   fresh = is_price_fresh()
   if not fresh:
       print("⚠️ قیمت قدیمی — از fallback استفاده می‌شود")
   return {
       "price": current_usdt_toman["price"],
       "source": current_usdt_toman["source"],
       "updated_at": current_usdt_toman["updated_at"],
       "is_fresh": fresh
   }

if __name__ == "__main__":
   asyncio.run(run_ocr_monitor())
