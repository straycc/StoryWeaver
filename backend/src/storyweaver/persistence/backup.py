"""本地数据库备份命令。"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .database import Database, DatabaseSettings, PROJECT_ROOT, database_url_from_env


def main() -> None:
    parser = argparse.ArgumentParser(description='备份本地作品数据库')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    database = Database(DatabaseSettings(database_url_from_env()))
    try:
        if not database.path or not database.path.exists():
            raise SystemExit('数据库尚不存在，请先启动应用')
        output = args.output or PROJECT_ROOT / 'data' / 'backups' / f'storyweaver-{datetime.now():%Y%m%d-%H%M%S-%f}.db'
        print(database.backup(output))
    finally:
        database.dispose()


if __name__ == '__main__':
    main()
