"""
Core configuration for Code Repository Manager.
"""

import os
from pathlib import Path
from typing import List
from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    # API keys
    openai_api_key: str = ""

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_reload: bool = False          # never True in production

    # CORS — comma-separated origins in .env: ALLOWED_ORIGINS=http://localhost:8501,...
    allowed_origins: List[str] = [
        "http://localhost:8501",       # Streamlit default
        "http://localhost:3000",       # React / other front-ends
        "http://127.0.0.1:8501",
        "http://127.0.0.1:3000",
    ]

    # Streamlit
    streamlit_port: int = 8501

    # # SQLite checkpoint DB — always resolved to an absolute path
    # checkpoint_db_path: str = str(PROJECT_ROOT / "data" / "checkpoints.db")

    # PostgreSQL (for LangGraph checkpointing)
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "code_repo_manager"
    postgres_user: str = "postgres"
    postgres_password: str = ""
    postgres_url: str = ""            # overrides constructed URL when set

    @property
    def get_postgres_url(self) -> str:
        if self.postgres_url:
            return self.postgres_url
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # Agent / LLM
    confidence_threshold: float = 0.7
    model_name: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096

    # Logging
    log_level: str = "INFO"
    log_file: str = str(PROJECT_ROOT / "logs" / "app.log")

    # MCP
    mcp_server_port: int = 8100

    # JWT
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24 hours

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}, got '{v}'")
        return upper

    @field_validator("confidence_threshold")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not (0.0 < v <= 1.0):
            raise ValueError("confidence_threshold must be between 0 and 1")
        return v

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()


def ensure_directories() -> None:
    for d in [PROJECT_ROOT / "data", PROJECT_ROOT / "logs", PROJECT_ROOT / "checkpoints"]:
        d.mkdir(parents=True, exist_ok=True)


ensure_directories()
