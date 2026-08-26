from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEASEQUEUE_", extra="ignore")

    database_path: Path = Path("leasequeue.db")
    environment: str = "development"
    max_request_bytes: int = Field(default=131_072, ge=4096, le=1_048_576)


settings = Settings()
