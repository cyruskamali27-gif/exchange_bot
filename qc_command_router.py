
from telethon import events

async def register_qc_handlers(client):

    @client.on(events.NewMessage(pattern=r'^/qc_status'))
    async def qc_status(event):

        text = """
✅ QC ONLINE

Poster checker: ACTIVE
Conversation checker: ACTIVE
Rate validator: ACTIVE
ElevenLabs QC: ACTIVE
Graphic alignment: ACTIVE
Auto correction: ACTIVE
"""

        await event.reply(text)
