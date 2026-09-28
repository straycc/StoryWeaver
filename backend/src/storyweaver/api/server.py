"""FastAPI 服务入口。"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from ..novel_creation.application import NovelApplicationSettings
from ..model_config import ModelCatalog
from ..observability import configure_logging, shutdown_logging
from .app import create_app
from ..persistence.database import database_url_from_env


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def main() -> None:
    catalog = ModelCatalog()
    database_url = database_url_from_env()
    settings = NovelApplicationSettings(base_url="http://127.0.0.1:11434/v1", model="unconfigured", api_key=None)
    app = create_app(settings=settings, database_url=database_url, model_catalog=catalog)
    configure_logging(
        log_directory=PROJECT_ROOT / "runtime" / "logs",
        level=os.getenv("STORYWEAVER_LOG_LEVEL", "INFO"),
    )
    try:
        uvicorn.run(
            app,
            host=os.getenv("STORYWEAVER_API_HOST", "127.0.0.1"),
            port=int(os.getenv("STORYWEAVER_API_PORT", "8000")),
            workers=1,
        )
    finally:
        shutdown_logging()


if __name__ == "__main__":  # pragma: no cover - 模块执行入口
    main()
