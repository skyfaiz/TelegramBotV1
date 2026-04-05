"""
config.py
Loads all settings from the .env file.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram bot
    TELEGRAM_BOT_TOKEN: str

    # RunPod
    INFINITETALK_ENDPOINT_ID: str
    RUNPOD_API_KEY: str

    # S3-compatible object storage (RunPod)
    S3_ENDPOINT_URL: str
    S3_ACCESS_KEY_ID: str
    S3_SECRET_ACCESS_KEY: str
    S3_BUCKET_NAME: str
    S3_REGION: str = "eu-ro-1"

    # Video cleanup settings
    video_retention_seconds: int = 3600
    cleanup_interval_seconds: int = 600

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
