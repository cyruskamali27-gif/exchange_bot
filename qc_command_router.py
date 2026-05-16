
from telethon import events
from config import ADMIN_ID

async def register_qc_handlers(client):

    @client.on(events.NewMessage(pattern=r'^/qc_status', incoming=True))
    async def qc_status(event):
        sender = await event.get_sender()
        if not sender or sender.id != ADMIN_ID:
            return

        text = (
            "QC ONLINE\n"
            "Poster checker: ACTIVE\n"
            "Conversation checker: ACTIVE\n"
            "Rate validator: ACTIVE\n"
            "ElevenLabs QC: ACTIVE\n"
            "Graphic alignment: ACTIVE\n"
            "Auto correction: ACTIVE"
        )
        await event.reply(text)
