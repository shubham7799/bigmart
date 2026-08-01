import logging
import os

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent.runtime import run_agent
from app.db.models import ProcessedUpdate
from app.db.session import async_session_maker
from app.telegram.client import send_document, send_message

logger = logging.getLogger(__name__)


async def _try_mark_processed(update_id: int) -> bool:
    """Atomically record that we're handling this update_id.

    Returns True the first time an update_id is seen (caller should process it),
    False if it was already recorded (a duplicate delivery — caller should skip).

    This is a single INSERT ... ON CONFLICT DO NOTHING rather than a SELECT
    followed by an INSERT, so the "have we seen this?" check and the write happen
    as one atomic statement: two near-simultaneous deliveries of the same
    update_id can't both observe "not present yet" and both proceed. Postgres
    serializes the two INSERTs against the primary key and only one of them
    actually inserts a row — the loser's rowcount is 0.
    """
    stmt = pg_insert(ProcessedUpdate).values(id=update_id).on_conflict_do_nothing()
    async with async_session_maker() as session:
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0


async def process_update(update: dict) -> None:
    update_id = update.get("update_id")
    if update_id is not None:
        is_new = await _try_mark_processed(update_id)
        if not is_new:
            logger.info("Skipping duplicate Telegram update_id=%s", update_id)
            return

    message = update.get("message")
    if not message or "text" not in message:
        return

    chat_id = message["chat"]["id"]
    try:
        reply = await run_agent(message["text"], thread_id=str(chat_id))
        if reply.text:
            await send_message(chat_id, reply.text)
        for file_path in reply.files:
            try:
                await send_document(chat_id, file_path)
            finally:
                # Generated PDFs/PPTX are one-shot temp files (see
                # app/services/invoice_pdf.py, analysis_pptx.py) — clean up once
                # sent regardless of whether the send itself succeeded.
                if os.path.exists(file_path):
                    os.remove(file_path)
    except Exception:
        logger.exception("Failed to handle message for chat %s", chat_id)
