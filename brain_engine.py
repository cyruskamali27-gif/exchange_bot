import asyncio
from google import genai
from config import GEMINI_API_KEY

_client = genai.Client(api_key=GEMINI_API_KEY)

_KNOWLEDGE_CORE = """
تو سیروس هستی — صراف ایرانی تورنتو. سرت شلوغه.

استراتژی پاسخ:
۱. روانشناسی فروش (Cialdini): ایجاد حس اطمینان، نه فشار.
۲. متد چانه‌زنی (Chris Voss): همدلی تاکتیکی — هرگز مستقیم رد نکن.
۳. لحن: صمیمی، مطمئن، کوتاه — مثل یک صراف حرفه‌ای تهرانی، نه ربات.

قوانین سخت:
- فارسی محاوره‌ای تهرانی مدرن — نه کتابی، نه رسمی
- حداکثر ۱ یا ۲ جمله کوتاه در هر پیام
- هرگز عدد اختراع نکن — اگه نرخ نداری بگو "نرخ تابلوئه"
- هرگز نگو: "عزیز"، "بزرگوار"، "خیالتون راحت باشه"، "یه لحظه"،
  "چک می‌کنم"، "صبر کن"، "مشتری گرامی"، "کاربر گرامی"
- ایموجی نفرست
- تعارف بی‌جا نکن
"""


class CyrusMasterBrain:
    def __init__(self):
        self.knowledge_core = _KNOWLEDGE_CORE

    async def generate_response(self, customer_msg: str, context: str = "") -> str:
        prompt = (
            f"{self.knowledge_core}\n\n"
            f"تاریخچه گفتگو: {context}\n"
            f"پیام جدید مشتری: {customer_msg}\n\n"
            "پاسخ کوتاه و طبیعی:"
        )

        def _call():
            resp = _client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return resp.text.strip()

        return await asyncio.to_thread(_call)


brain = CyrusMasterBrain()
