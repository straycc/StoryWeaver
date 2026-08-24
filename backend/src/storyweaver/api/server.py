"""FastAPI 服务入口。"""

from __future__ import annotations

import os

import uvicorn

from ..novel_creation.application import NovelApplicationSettings, load_env_file
from .app import create_app


def main() -> None:
    load_env_file()
    database_url = os.getenv("STORYWEAVER_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("缺少 STORYWEAVER_DATABASE_URL；请先配置 PostgreSQL 连接并执行 alembic upgrade head")
    settings = NovelApplicationSettings.from_env()
    app = create_app(settings=settings, database_url=database_url)
    uvicorn.run(app, host=os.getenv("STORYWEAVER_API_HOST", "127.0.0.1"), port=int(os.getenv("STORYWEAVER_API_PORT", "8000")), workers=1)


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    main()
