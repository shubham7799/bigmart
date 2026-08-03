from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    google_api_key: str = ""
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/bigmart"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def _normalize_for_asyncpg(cls, v: str) -> str:
        # Managed Postgres providers (Render, Aiven, Heroku, etc.) commonly hand
        # out a plain "postgresql://" — or, like Aiven, the older "postgres://"
        # alias, which SQLAlchemy no longer recognizes at all — connection
        # string. Our engine needs the asyncpg driver explicitly (see
        # app/db/session.py), so normalize here rather than requiring every
        # deploy target's DATABASE_URL to be hand-edited.
        if v.startswith("postgres://"):
            v = "postgresql+asyncpg://" + v[len("postgres://") :]
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)

        # SQLAlchemy's asyncpg dialect passes every URL query param straight
        # through as a keyword argument to asyncpg.connect() (see
        # PGDialect_asyncpg.create_connect_args). asyncpg's connect() has no
        # "sslmode" parameter — that's a libpq/psycopg naming convention; it
        # only understands "ssl" (which does accept the same string values,
        # e.g. "require"). Providers that require SSL (Aiven does) commonly
        # hand out "?sslmode=require" URIs, which would otherwise fail at
        # connect time with "unexpected keyword argument 'sslmode'" — rename
        # rather than requiring a hand-edit.
        parts = urlsplit(v)
        query = dict(parse_qsl(parts.query))
        if "sslmode" in query:
            query.setdefault("ssl", query.pop("sslmode"))
        v = urlunsplit(parts._replace(query=urlencode(query)))
        return v


settings = Settings()
