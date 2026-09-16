import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    storage_dir: Path
    max_upload_bytes: int
    llm_provider: str


def load() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "postgresql://docforge_app@localhost/docforge"),
        storage_dir=Path(os.environ.get("STORAGE_DIR", "./data/files")),
        max_upload_bytes=int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))),
        llm_provider=os.environ.get("LLM_PROVIDER", "mock"),
    )


settings = load()
