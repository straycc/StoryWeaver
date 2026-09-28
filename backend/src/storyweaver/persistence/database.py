"""本地 SQLite 连接、事务、初始化及备份。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_VERSION = 1


class Base(DeclarativeBase):
    """全部表的 Declarative 基类。"""


def database_url_from_env(*, evaluation: bool = False) -> str:
    """相对路径固定以项目根目录为基准，避免启动位置影响数据位置。"""
    key = 'STORYWEAVER_EVALUATION_DATABASE_PATH' if evaluation else 'STORYWEAVER_DATABASE_PATH'
    value = os.getenv(key, '').strip()
    if not value and os.getenv('STORYWEAVER_DATABASE_URL', '').strip():
        raise ValueError(f'旧 STORYWEAVER_DATABASE_URL 已停用，请配置 {key}；旧数据不会自动迁移')
    path = Path(value or ('data/evaluation.db' if evaluation else 'data/storyweaver.db')).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return URL.create('sqlite+pysqlite', database=str(path.resolve())).render_as_string(hide_password=False)


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """单实例 SQLite 数据库配置。"""

    url: str
    echo: bool = False

    def __post_init__(self) -> None:
        if make_url(self.url).get_backend_name() != 'sqlite':
            raise ValueError('本地版本仅支持 SQLite，请配置 STORYWEAVER_DATABASE_PATH')


class Database:
    """同步会话工厂；读事务与短写事务分别管理。"""

    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        name = make_url(settings.url).database
        self.path = Path(name).resolve() if name and name != ':memory:' else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = None
        options = {} if self.path else {'poolclass': StaticPool}
        self.engine = create_engine(settings.url, echo=settings.echo,
                                    connect_args={'check_same_thread': False, 'timeout': 5}, **options)

        @event.listens_for(self.engine, 'connect')
        def configure_connection(connection, _record):
            # 自己发出 BEGIN，避免 sqlite3 旧事务模式与 SQLAlchemy 重复开启事务。
            connection.isolation_level = None
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=5000')
            connection.execute('PRAGMA synchronous=FULL')

        @event.listens_for(self.engine, 'begin')
        def begin(connection):
            mode = 'BEGIN IMMEDIATE' if connection.get_execution_options().get('sqlite_write') else 'BEGIN'
            connection.exec_driver_sql(mode)

        self._sessions = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)

    def session(self) -> Session:
        return self._sessions()

    @contextmanager
    def write_transaction(self, session: Session):
        """先取得写资格再读取待修改状态；调用模型期间不能持有此事务。"""
        with session.begin():
            session.connection(execution_options={'sqlite_write': True})
            yield session

    def create_schema(self) -> None:
        """仅初始化空库；未知结构必须显式升级，不能静默当成新库。"""
        from . import tables  # noqa: F401
        with self.engine.connect() as connection:
            raw = connection.connection.driver_connection
            mode = raw.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if self.path and mode.lower() != 'wal':
                raise RuntimeError('无法启用 SQLite WAL 模式')
            version = raw.execute('PRAGMA user_version').fetchone()[0]
            existing = raw.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if version != SCHEMA_VERSION and (version != 0 or existing):
                raise RuntimeError(f'不支持数据库结构版本 {version}，需要显式升级或使用新的数据库文件')
            with connection.begin():
                if version == 0:
                    Base.metadata.create_all(connection)
                    connection.exec_driver_sql(f'PRAGMA user_version={SCHEMA_VERSION}')
                elif not set(Base.metadata.tables).issubset({row[0] for row in existing}):
                    raise RuntimeError('数据库结构不完整，请从备份恢复')

    def acquire_instance_lock(self) -> None:
        """使用操作系统文件锁，在恢复遗留任务前阻止重复启动。"""
        if not self.path:
            return
        if self._lock_file is not None:
            raise RuntimeError('此数据库实例已持有运行锁')
        handle = open(str(self.path) + '.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(f'数据库正在使用，请关闭另一个 StoryWeaver 实例：{self.path}') from exc
        self._lock_file = handle

    def backup(self, destination: Path) -> Path:
        """通过 SQLite 备份接口包含已提交的 WAL 数据，不直接复制主文件。"""
        destination = destination.expanduser().resolve()
        if destination == self.path or destination.exists():
            raise ValueError('备份路径必须是新的文件，不能覆盖数据库或已有备份')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.connect() as source:
            with closing(sqlite3.connect(destination)) as target:
                source.connection.driver_connection.backup(target)
        return destination

    def dispose(self) -> None:
        self.engine.dispose()
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None
