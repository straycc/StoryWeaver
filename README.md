# StoryWeaver

StoryWeaver 是一个面向长篇连载创作的 Agent 小说工作台。模型负责规划、写作、审查与状态提取；确定性业务层负责质量门禁、正史归约、Hook 治理、事务提交与任务生命周期。

> 当前主路径：**React + FastAPI + PostgreSQL + OpenAI Agents SDK + Pydantic**。

## 核心能力

- **章节流水线**：`Planner → Writer → Reviewer → Reviser → Analyzer → StateReducer`；章节只有通过质量门禁后才会写入正史。
- **受限自治检索**：Planner 与 Reviewer 通过只读工具按需检索正史，研究阶段与最终交付阶段隔离，工具预算由服务端硬限制。
- **一致性与伏笔治理**：StateReducer 原子提交事实、人物、地点和伏笔变化；HookManager 控制推进、回收、合并与新增预算。
- **结构化恢复**：Pydantic 校验工具参数和模型输出；格式错误先做一次无工具修复，再有限重试。
- **持久 Job + SSE**：规划、写作、重写、连续创作以 Job 运行；浏览器断开不取消后台任务。
- **主 Agent Action Surface**：自然语言入口只理解意图、查询状态或提出动作；Dispatcher 创建 Job，Pipeline 独占正史写入权。
- **创作控制与 Skill**：作者意图长期生效；当前焦点可单章或持续；Skill 按需加载并冻结内容哈希。
- **本地可观测性**：终端显示阶段、模型回合、工具、耗时和 Token；详细 Trace 与失败诊断写入本地。

## 架构

```text
React Studio Chat / 作品工作台
              │ REST + SSE
              ▼
FastAPI Action Surface
  Main Agent ──► Query Reply / ActionProposal
              │
              ▼
ActionDispatcher ──► Persistent Job ──► JobSupervisor
                                            │
                                            ▼
Planner → Writer → Reviewer → Reviser → Analyzer
                                            │
                                            ▼
                     QualityGate / HookManager / StateReducer
                                            │
                                            ▼
                                      PostgreSQL
```

- **OpenAI Agents SDK**：模型调用、工具循环、结构化交付与 Trace。
- **Pydantic**：工具参数、主 Agent 决策和 Worker 输出校验。
- **StoryWeaver 领域层**：上下文、质量规则、正史归约、Hook 治理与提交事务。
- **PostgreSQL**：作品、章节、计划、候选稿、会话、记忆、Job 和事件的唯一运行时事实源。

## 快速开始

### 1. 配置模型

在项目根目录创建 `.env`：

```env
STORYWEAVER_LLM_BASE_URL=https://api.deepseek.com
STORYWEAVER_LLM_MODEL=deepseek-v4-flash
STORYWEAVER_LLM_API_KEY=your-api-key

# 小说创作通常建议关闭思考模式，降低延迟与 Token 消耗。
STORYWEAVER_LLM_THINKING=disabled
STORYWEAVER_LLM_JSON_MODE=auto
```

`.env` 已被 Git 忽略，禁止提交真实 API Key。

### 2. Docker Compose 启动

需要 Docker Desktop：

```bash
docker compose up --build
```

- React 工作台：<http://127.0.0.1:5173>
- FastAPI：<http://127.0.0.1:8000>
- 健康检查：<http://127.0.0.1:8000/api/v1/health>

Compose 会启动 PostgreSQL、API 与前端；API 容器启动时自动执行数据库迁移。

### 3. 本地开发启动

先启动 PostgreSQL：

```bash
docker compose up -d postgres
```

配置环境变量并迁移：

```bash
export PYTHONPATH=backend/src
export STORYWEAVER_DATABASE_URL='postgresql+psycopg://storyweaver:storyweaver@localhost:5432/storyweaver'
python -m alembic upgrade head
python -m storyweaver.api.server
```

另开终端启动前端：

```bash
cd frontend
npm install
npm run dev
```

PowerShell：

```powershell
$env:PYTHONPATH = "backend/src"
$env:STORYWEAVER_DATABASE_URL = "postgresql+psycopg://storyweaver:storyweaver@localhost:5432/storyweaver"
```

## 数据库迁移

仓库只保留一份当前完整 Schema 的基线迁移：

```text
alembic/versions/20260824_01_initial_schema.py
```

它适用于空数据库：

```bash
PYTHONPATH=backend/src python -m alembic upgrade head
```

早期开发版数据库不再支持按旧 Revision 增量升级。若本地数据库仍记录旧版本，请重建开发库：

```bash
docker compose down -v
docker compose up -d postgres
PYTHONPATH=backend/src python -m alembic upgrade head
```

> `docker compose down -v` 会删除本地 PostgreSQL 数据卷。

## 创作流程

1. 创建作品，生成基础资料、人物和初始正史。
2. Planner 使用只读工具检索近期摘要、人物状态和开放伏笔，生成候选计划。
3. 用户确认计划后，Writer 生成正文，Reviewer 核验证据，Reviser 在必要时定向修订。
4. Analyzer 提取状态增量，HookManager 治理伏笔，StateReducer 在事务边界提交正史。
5. 质量门禁拒绝时，候选稿保留，不污染已提交章节。

连续创作会在每章提交后基于最新正史重新规划；质量拒绝、不可恢复模型错误或人工暂停会暂停 Job。

## 对话、查询与确认

Studio Chat 支持自然语言和快捷按钮：

- 查询作品进度、已完成章节、指定章节、待确认计划、审稿结果、人物与伏笔：直接返回结果，不创建 Job。
- 生成或调整计划：直接创建候选计划 Job，不修改正史。
- 确认写作与重写：先创建 `ActionProposal`，用户确认后才创建 Job。
- 快捷按钮已有明确意图，直接进入 Dispatcher，不经 Main Agent。

示例：

```text
下一章让主角发现师父隐瞒真相，但暂时不要揭穿。
查看第 3 章审稿结果。
当前还有哪些未解伏笔？
```

## Skill

项目 Skill 位于 `skills/<skill-id>/SKILL.md`：

```text
skills/
└── wuxia-serial-writing/
    └── SKILL.md
```

输入框键入 `/` 可选择 Skill，或直接使用：

```text
/skills
/skill wuxia-serial-writing 下一章加强江湖压迫感与人物试探，但不要揭露反派身份。
```

Skill 仅影响候选计划；计划卡会显示实际注入的 Skill ID 与内容哈希前缀，确认写作后沿用该冻结版本。

## API 概览

所有接口位于 `/api/v1`：

```text
GET  /health
GET  /bootstrap
GET  /books
POST /books                         创建作品 Job
POST /books/{book_id}/plans         生成计划 Job
POST /books/{book_id}/chapters      确认计划并写作 Job
POST /books/{book_id}/chapters/batch
POST /books/{book_id}/chapters/{n}/rewrite

POST /sessions
GET  /sessions/{session_id}
POST /sessions/{session_id}/messages
POST /action-proposals/{proposal_id}/confirm
POST /action-proposals/{proposal_id}/cancel

GET  /jobs/{job_id}
GET  /jobs/{job_id}/events          SSE
POST /jobs/{job_id}/retry
```

模型型操作返回 `202 Accepted + job_id`，前端通过 Job SSE 接收模型回合、工具调用、格式修复、阶段结果和终态事件。

## 质量与可靠性策略

- Planner：最多 3 个研究回合、4 次只读工具调用。
- Reviewer：最多 3 个研究回合、10 次只读工具调用；报告阶段无查询能力。
- 最终交付通过 Pydantic DTO 校验；格式失败时先无工具修复一次。
- 默认门禁：审稿分数 ≥ 80；正文低于目标 0.5 倍要求扩写，1.5～1.8 倍记录观察，超过 1.8 倍触发压缩。
- 同一本书同时仅允许一个 `book_write` Job；普通聊天不占写锁。

## 可观测性

`runtime/` 由服务自动创建且不会提交 Git：

```text
runtime/
├── logs/          # 服务与 Agent 阶段日志
├── traces/        # 本地 SDK Trace JSONL
└── diagnostics/   # 脱敏模型失败诊断
```

## 测试

后端测试使用 unittest 与 SDK Fake/Scripted 模型，不调用真实模型：

```bash
export PYTHONPATH=backend/src:backend/tests
python -m unittest discover -s backend/tests
```

前端构建校验：

```bash
cd frontend
npm install
npm run build
```

## 目录结构

```text
StoryWeaver/
├── backend/
│   ├── src/storyweaver/
│   │   ├── api/              # FastAPI、Action Surface、JobSupervisor
│   │   ├── application/      # 会话、Timeline、工作区编排
│   │   ├── novel_creation/   # Pipeline、Agent、Hook、质量门禁、状态归约
│   │   ├── llm/              # OpenAI Agents SDK 与结构化输出
│   │   ├── persistence/      # PostgreSQL 表、Repository、事务
│   │   ├── memory/  skills/  evaluation/  observability/
│   └── tests/
├── frontend/                 # React + TypeScript + Vite
├── skills/                   # 项目级创作 Skill
├── alembic/                  # 单一数据库基线迁移
├── docs/
├── runtime/                  # 本地运行产物（Git 忽略）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

`data/` 是已忽略的历史文件型 CLI 数据，不属于当前 PostgreSQL 运行时。

## 技术栈与边界

- Python 3.11、asyncio、OpenAI Agents SDK、Pydantic 2、Tenacity
- FastAPI、Uvicorn、SSE、PostgreSQL 16、SQLAlchemy 2、Alembic、psycopg
- React、TypeScript、Vite、Docker Compose
- 单用户、单 FastAPI 实例、进程内 JobSupervisor；不包含认证、多租户、分布式队列或多实例 Worker。
- 服务重启会将运行中的 Job 标记为 `interrupted`；已原子提交章节不会重复写入，模型调用本身不承诺 exactly-once。
