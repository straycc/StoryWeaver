"""FastAPI 服务入口。"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from ..novel_creation.application import NovelApplicationSettings, load_env_file
from ..observability import configure_logging, shutdown_logging
from .app import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def main() -> None:
    load_env_file()
    database_url = os.getenv("STORYWEAVER_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("缺少 STORYWEAVER_DATABASE_URL；请先配置 PostgreSQL 连接并执行 database/schema.sql")
    settings = NovelApplicationSettings.from_env()
    app = create_app(settings=settings, database_url=database_url)
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
