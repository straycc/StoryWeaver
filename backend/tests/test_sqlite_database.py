"""真实文件数据库的并发、生命周期和备份回归测试。"""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from storyweaver.api import create_app
from storyweaver.novel_creation.application import NovelApplicationSettings
from storyweaver.persistence import Database, DatabaseSettings, JobRepository, SQLAlchemyChatSessionRepository
from storyweaver.persistence.database import PROJECT_ROOT, database_url_from_env
from storyweaver.persistence.tables import ChatSessionEventRow, ChatSessionRow, JobEventRow


@pytest.fixture
def database(tmp_path):
    db = Database(DatabaseSettings(f'sqlite+pysqlite:///{tmp_path / "story.db"}'))
    db.create_schema()
    yield db
    db.dispose()


def test_relative_path_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('STORYWEAVER_DATABASE_PATH', 'data/example.db')
    from sqlalchemy.engine import make_url
    assert Path(make_url(database_url_from_env()).database) == PROJECT_ROOT / 'data/example.db'
    monkeypatch.delenv('STORYWEAVER_EVALUATION_DATABASE_PATH', raising=False)
    monkeypatch.delenv('STORYWEAVER_DATABASE_URL', raising=False)
    assert Path(make_url(database_url_from_env(evaluation=True)).database).name == 'evaluation.db'


def test_pragmas_and_foreign_key_enforcement(database):
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql('PRAGMA journal_mode').scalar() == 'wal'
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        assert connection.exec_driver_sql('PRAGMA busy_timeout').scalar() == 5000
    with pytest.raises(IntegrityError):
        with database.session() as session, database.write_transaction(session):
            session.add(JobEventRow(job_id='missing', sequence=1, event_type='test', payload_json={}, created_at='now'))


def test_concurrent_events_are_ordered_and_replayable(database):
    jobs = JobRepository(database)
    job = jobs.create(job_type='test', book_id=None, payload={})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda number: jobs.append_event(job.job_id, 'progress', {'number': number}), range(40)))
    events = jobs.events_after(job.job_id)
    assert [event.sequence for event in events] == list(range(1, 42))
    assert len(jobs.events_after(job.job_id, after_sequence=30)) == 11


def test_concurrent_book_jobs_allow_one_writer(database):
    from storyweaver.novel_creation.exceptions import BookBusyError
    jobs = JobRepository(database)
    def create(_):
        try:
            return jobs.create(job_type='test', book_id='book', payload={}, lock_scope='book_write')
        except BookBusyError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert sum(item is not None for item in results) == 1


def test_rollback_and_backup_include_committed_wal(database, tmp_path):
    jobs = JobRepository(database)
    job = jobs.create(job_type='test', book_id=None, payload={})
    with pytest.raises(RuntimeError):
        with database.session() as session, database.write_transaction(session):
            session.execute(text("UPDATE jobs SET status='failed'"))
            raise RuntimeError('模拟事务失败')
    assert jobs.get(job.job_id).status == 'queued'
    backup = database.backup(tmp_path / 'backup.db')
    restored = Database(DatabaseSettings(f'sqlite+pysqlite:///{backup}'))
    try:
        restored.create_schema()
        assert JobRepository(restored).get(job.job_id).status == 'queued'
    finally:
        restored.dispose()


def test_another_process_cannot_acquire_instance_lock(database):
    database.acquire_instance_lock()
    code = '''
import sys
from storyweaver.persistence import Database, DatabaseSettings
db = Database(DatabaseSettings(sys.argv[1]))
try:
    db.acquire_instance_lock()
except RuntimeError:
    sys.exit(17)
finally:
    db.dispose()
'''
    result = subprocess.run([sys.executable, '-c', code, database.settings.url], capture_output=True, timeout=30)
    assert result.returncode == 17, result.stderr.decode()
    database.dispose()
    result = subprocess.run([sys.executable, '-c', code, database.settings.url], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()


def test_unknown_schema_is_not_silently_modified(tmp_path):
    path = tmp_path / 'unknown.db'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA user_version=999')
    database = Database(DatabaseSettings(f'sqlite+pysqlite:///{path}'))
    try:
        with pytest.raises(RuntimeError, match='999'):
            database.create_schema()
    finally:
        database.dispose()


def test_first_start_restart_and_duplicate_instance(tmp_path):
    url = f'sqlite+pysqlite:///{tmp_path / "new" / "app.db"}'
    settings = NovelApplicationSettings(base_url='https://example.invalid/v1', model='test', api_key='test')
    app = create_app(settings=settings, database_url=url)
    with TestClient(app) as client:
        assert client.get('/api/v1/health').json() == {'status': 'ok'}
        assert client.get('/api/v1/books').json() == {'books': []}
        job = app.state.jobs.create(job_type='test', book_id=None, payload={})
        other = create_app(settings=settings, database_url=url)
        with pytest.raises(RuntimeError, match='数据库正在使用'):
            with TestClient(other):
                pass
        assert app.state.jobs.get(job.job_id).status == 'queued'
    restarted = create_app(settings=settings, database_url=url)
    with TestClient(restarted):
        assert restarted.state.jobs.get(job.job_id).status == 'interrupted'
        assert restarted.state.jobs.events_after(job.job_id)[-1].event_type == 'job_interrupted'


def test_empty_session_reused_and_old_duplicates_pruned(database):
    sessions = SQLAlchemyChatSessionRepository(database)
    original = sessions.create_session()
    assert sessions.create_session().session_id == original.session_id

    # 模拟旧版本留下的第二条原始空对话。
    with database.session() as transaction, database.write_transaction(transaction):
        source = transaction.get(ChatSessionRow, original.session_id)
        source_event = transaction.query(ChatSessionEventRow).filter_by(session_id=original.session_id).one()
        duplicate_id = 'legacy-empty-session'
        transaction.add(ChatSessionRow(
            session_id=duplicate_id, created_at=source.created_at,
            updated_at='9999-01-01', title=source.title, book_id=None,
            last_sequence=1, message_count=0,
        ))
        transaction.flush()
        event_json = dict(source_event.event_json)
        event_json['session_id'] = duplicate_id
        transaction.add(ChatSessionEventRow(
            session_id=duplicate_id, sequence=1, event_json=event_json,
        ))

    assert len(sessions.list_sessions()) == 1
    assert sessions.prune_duplicate_empty_sessions() == 1
    assert sessions.prune_duplicate_empty_sessions() == 0
    assert sessions.create_session().session_id == duplicate_id
    with database.session() as transaction:
        assert transaction.get(ChatSessionRow, original.session_id) is None

    sessions.append_message(duplicate_id, role='user', content='你好')
    fresh = sessions.create_session()
    assert fresh.session_id != duplicate_id
    assert len(sessions.list_sessions()) == 2


def test_empty_session_with_job_is_not_reused_or_deleted(database):
    sessions = SQLAlchemyChatSessionRepository(database)
    old = sessions.create_session()
    JobRepository(database).create(job_type='test', book_id=None, payload={'session_id': old.session_id})
    fresh = sessions.create_session()
    assert fresh.session_id != old.session_id
    assert sessions.prune_duplicate_empty_sessions() == 0
    assert {item.session_id for item in sessions.list_sessions()} == {old.session_id, fresh.session_id}
