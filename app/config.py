from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    google_api_key: str = ""
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/bigmart"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def _ensure_asyncpg_driver(cls, v: str) -> str:
        # Managed Postgres providers (Render, Heroku, etc.) commonly hand out a
        # plain "postgresql://" connection string. Our engine needs the asyncpg
        # driver explicitly (see app/db/session.py), so normalize here rather
        # than requiring every deploy target's DATABASE_URL to be hand-edited.
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v


settings = Settings()
