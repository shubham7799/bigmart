from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    google_api_key: str = ""
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/bigmart"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
