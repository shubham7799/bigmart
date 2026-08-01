import os

import httpx

from app.config import settings


async def send_message(chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json={"chat_id": chat_id, "text": text})
        response.raise_for_status()


async def send_document(chat_id: int, file_path: str) -> None:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument"
    filename = os.path.basename(file_path)
    async with httpx.AsyncClient(timeout=60.0) as client:
        with open(file_path, "rb") as f:
            response = await client.post(
                url,
                data={"chat_id": chat_id},
                files={"document": (filename, f)},
            )
        response.raise_for_status()
