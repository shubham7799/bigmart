from fastapi import BackgroundTasks, FastAPI, Request

from app.db.session import init_db
from app.telegram.webhook import process_update

app = FastAPI(title="BigMart API", version="1.0.0")


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Welcome to BigMart API"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    update = await request.json()
    background_tasks.add_task(process_update, update)
    return {"status": "ok"}
