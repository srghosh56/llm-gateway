"""Runtime configuration.

All settings come from environment variables (prefix ``GATEWAY_``) so the same image runs
unchanged in docker compose, CI and Kubernetes (12-factor style).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", extra="ignore")

    # Base URL of Ollama's OpenAI-compatible API (it lives under /v1).
    ollama_base_url: str = "http://localhost:11434/v1"
    # Upstream timeout. LLM calls are slow, so this is generous; connect timeout stays short.
    upstream_timeout_seconds: float = 120.0

    # Step-1 static catalogue: which backend serves requests and which models it exposes.
    # (Replaced by the database-backed model registry later.)
    default_backend: str = "ollama"
    models: list[str] = ["qwen2.5:0.5b"]

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    # Cached so env parsing happens once; tests build their own Settings instead.
    return Settings()
