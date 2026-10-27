"""Runtime configuration.

All settings come from environment variables (prefix ``GATEWAY_``) so the same image runs
unchanged in docker compose, CI and Kubernetes (12-factor style).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    # Static admin key for /admin routes. Unset = admin API disabled. Never commit a real one.
    admin_api_key: str | None = None

    # Base URL of Ollama's OpenAI-compatible API (it lives under /v1).
    ollama_base_url: str = "http://localhost:11434/v1"
    # Upstream timeout. LLM calls are slow, so this is generous; connect timeout stays short.
    upstream_timeout_seconds: float = 120.0

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    # Cached so env parsing happens once; tests build their own Settings instead.
    return Settings()
