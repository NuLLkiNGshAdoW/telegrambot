"""Generate a Telethon StringSession for Render.

Run locally with TELEGRAM_API_ID and TELEGRAM_API_HASH in the environment:
    python generate_session.py
"""

import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("TELETHON_SESSION_STRING=")
        print(client.session.save())


if __name__ == "__main__":
    asyncio.run(main())
