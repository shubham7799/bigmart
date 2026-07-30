import logging

from fastapi import BackgroundTasks, FastAPI, Request

from app.agent.runtime import run_agent
from app.telegram import send_message

logger = logging.getLogger(__name__)

app = FastAPI(title="BigMart API", version="1.0.0")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Welcome to BigMart API"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


async def _reply_to_message(chat_id: int, text: str) -> None:
    try:
        reply = await run_agent(text)
        await send_message(chat_id, reply)
    except Exception:
        logger.exception("Failed to handle message for chat %s", chat_id)


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    update = await request.json()
    message = update.get("message")
    if not message or "text" not in message:
        return {"status": "ignored"}

    background_tasks.add_task(_reply_to_message, message["chat"]["id"], message["text"])
    return {"status": "ok"}
