from fastapi import FastAPI

app = FastAPI(title="BigMart API", version="1.0.0")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Welcome to BigMart API"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
