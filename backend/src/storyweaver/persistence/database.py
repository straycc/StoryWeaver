"""SQLAlchemy 数据库连接和元数据。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """全部 PostgreSQL 表的 Declarative 基类。"""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """单实例服务使用的数据库配置。"""

    url: str
    echo: bool = False

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("数据库 url 不能为空")


class Database:
    """同步 SQLAlchemy 会话工厂。

    小说领域 Store 本来是同步接口；保留这个边界可让 Pipeline 不感知 ORM。
    """

    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        self.engine: Engine = create_engine(
            settings.url,
            echo=settings.echo,
            pool_pre_ping=True,
        )
        self._sessions = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    def session(self) -> Session:
        return self._sessions()

    def create_schema(self) -> None:
        """仅供本地开发与测试初始化；生产部署由 Alembic 执行迁移。"""

        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()
