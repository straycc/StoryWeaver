# FastAPI + PostgreSQL 单实例运行

新 API 使用 PostgreSQL 作为作品、章节、计划、候选稿、状态快照、会话、长期记忆、Job 与 Job 事件的唯一运行时事实源。它不会读取、迁移或删除已有 `data/` 内容。

## 启动

配置 `.env` 中原有的模型变量，并新增：

```env
STORYWEAVER_DATABASE_URL=postgresql+psycopg://storyweaver:storyweaver@127.0.0.1:5432/storyweaver
```

首次部署时执行基线迁移：

```bash
PYTHONPATH=backend/src alembic upgrade head
```

当前仓库只保留一份完整初始迁移 `20260824_01_initial_schema.py`，适用于空数据库。
如果本地数据库曾运行旧开发版迁移，先删除并新建开发数据库，再执行上述命令；它不再支持旧迁移链的增量升级。

启动单实例服务：

```bash
PYTHONPATH=backend/src python -m storyweaver.api.server
```

也可使用：

```bash
docker compose up --build
```

服务地址为 `http://127.0.0.1:8000`，健康检查为 `GET /api/v1/health`。

## Job 语义

写操作返回 `202` 和 `job_id`。用以下接口读取结果或订阅进度：

```text
GET /api/v1/jobs/{job_id}
GET /api/v1/jobs/{job_id}/events
POST /api/v1/jobs/{job_id}/retry
```

同一本书同时只允许一个 `queued` 或 `running` 的规划/写作 Job。浏览器断开不会取消任务；服务重启时正在运行的 Job 会被标记为 `interrupted`，用户可显式重试。模型调用本身不会被伪造为可恢复。

## 主要 API

```text
POST /api/v1/books                         创建作品 Job
GET  /api/v1/books
GET  /api/v1/books/{book_id}
POST /api/v1/books/{book_id}/plans          规划 Job
POST /api/v1/books/{book_id}/chapters       确认计划并写作 Job
POST /api/v1/books/{book_id}/chapters/batch 连续创作 Job
POST /api/v1/books/{book_id}/chapters/{n}/rewrite
GET  /api/v1/sessions
POST /api/v1/sessions
GET  /api/v1/memories
```

## React 前端

新前端位于 `frontend/`，仅访问 `/api/v1`，不会读取旧 `data/` 文件。它沿用旧原生工作台的布局、字号、弹窗和交互文案；模型操作则改为持久 Job + SSE，页面刷新不会取消后台创作。开发模式下，Vite 会把 `/api` 代理到 FastAPI。

本地启动（FastAPI 已运行在 8000 端口）：

```bash
cd frontend
npm install
npm run dev
```

浏览器访问 `http://127.0.0.1:5173`。也可以通过 Docker Compose 同时启动：

```bash
docker compose up --build
```

此时前端地址同样为 `http://127.0.0.1:5173`。

旧原生 Web UI 已移除；React 前端是唯一的 Web 入口。
