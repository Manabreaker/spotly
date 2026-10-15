from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_revision: str = "local"
    database_url: str = "postgresql+psycopg://postgres@localhost/spotly"
    data_dir: Path = Path("data")
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
