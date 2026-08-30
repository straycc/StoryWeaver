# StoryWeaver

StoryWeaver 是一个面向长篇连载创作的 Agent 小说工作台。模型负责规划、写作、审查与状态提取；确定性业务层负责质量门禁、正史归约、Hook 治理、事务提交与任务生命周期。

> 当前主路径：**React + FastAPI + PostgreSQL + OpenAI Agents SDK + Pydantic**。

## 核心能力

- **章节流水线**：`Planner → Writer → Reviewer → Reviser → Analyzer → StateReducer`；章节只有通过质量门禁后才会写入正史。
- **受限自治检索**：Planner、Reviewer 与 Analyzer 通过只读工具按需检索正史，研究阶段与最终交付阶段隔离，工具预算由服务端硬限制。
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
  Main Agent ──► Capability Manifest ──► Query / Creative / Workflow
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

Compose 会启动 PostgreSQL、API 与前端；API 容器启动时会自动执行 `database/schema.sql`，为新数据库创建当前表结构。

### 3. 本地开发启动

先启动 PostgreSQL：

```bash
docker compose up -d postgres
```

配置环境变量并初始化：

```bash
export PYTHONPATH=backend/src
export STORYWEAVER_DATABASE_URL='postgresql+psycopg://storyweaver:storyweaver@localhost:5432/storyweaver'
docker compose exec -T postgres psql -U storyweaver -d storyweaver -v ON_ERROR_STOP=1 < database/schema.sql
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

## 数据库初始化

仓库发布一份当前完整的 PostgreSQL Schema：

```text
database/schema.sql
```

它适用于空数据库，也可以重复执行：

```bash
docker compose exec -T postgres psql -U storyweaver -d storyweaver -v ON_ERROR_STOP=1 < database/schema.sql
```

PowerShell：

```powershell
Get-Content database/schema.sql | docker compose exec -T postgres psql -U storyweaver -d storyweaver -v ON_ERROR_STOP=1
```

`schema.sql` 只负责创建缺失表和索引，不会自动将旧结构升级为新结构。开发期间如需重建空库：

```bash
docker compose down -v
docker compose up -d postgres
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
- 章节计划按 `pending → approved → confirmed` 推进：批准计划不会自动写作，之后可单独按计划生成正文。
- 创建作品、按计划写作、批量与重写等高影响动作：先创建 `ActionProposal`，用户确认后才创建 Job。
- 借助 Skill 的创作讨论由 `creative_discussion` 专业 Worker 完成，只返回建议，不修改正史或计划。
- 快捷按钮已有明确意图，直接进入 Dispatcher，不经 Main Agent。

示例：

```text
下一章让主角发现师父隐瞒真相，但暂时不要揭穿。
查看第 3 章审稿结果。
当前还有哪些未解伏笔？
```

## Skill

首版本地 Skill 只从仓库根目录 `skills/builtin/<skill-id>/SKILL.md` 发现：

```text
skills/
├── builtin/
│   ├── chapter-planning/
│   ├── fiction-quality-review/
│   ├── natural-fiction-prose-zh/
│   ├── novel-conception/
│   ├── story-continuity-review/
│   └── wuxia-serial-writing/
└── vendor/
    └── story-skills/          # 上游参考副本，不参与 Discover
```

Backend 启动时只扫描 metadata；用户提交创作任务后，Runtime 按
`Discover → Activate → Resolve → Materialize` 处理 Skill。Activate 会冻结完整
`SKILL.md` 与内容哈希到 `CreativeTaskContext`，后续 Pipeline 不读取磁盘。
Job 创建后以 Job Snapshot 为准；创建章节 Proposal 后，该 Proposal Snapshot 是
修订、确认和写作的唯一 Skill 输入来源。

Skill 默认只对本次用户提交的 `CreativeTask` 生效；前端提交成功后清空选择。若该
任务产生 Job 或 Proposal，冻结 Snapshot 会继续传递给同一长任务的所有内部节点、
确认步骤和重试，不会要求 Pipeline 再次读取或选择 Skill。
未选择 Skill 时不会从 Registry 自动激活；一旦 UI 显式选择，Main Agent 的普通
创作回复不得忽略它，而会确定性转交 `creative_discussion`。Pipeline 内的 Resolver
只能在已冻结集合中决定本次 invocation 全文加载哪些 Skill。

输入框键入 `/` 可选择 Skill，或直接使用：

```text
/skills
/skill wuxia-serial-writing 下一章加强江湖压迫感与人物试探，但不要揭露反派身份。
```

输入框只保存用户原始要求，Skill 通过结构化 `skill_ids` 提交。每次实际模型调用会
依据本次语义目标独立 Resolve：所有已激活 Skill 都暴露 metadata，相关 Skill 的
完整正文按预算原子装入，不会截断半个 `SKILL.md`。Canon、业务硬约束、用户要求和
Confirmed Plan 的优先级始终高于 Skill。计划卡会显示冻结的 Skill ID 与内容哈希前缀。

V1 不读取或执行 `references/`、`scripts/`、`assets/`；因此内置 `SKILL.md` 必须独立
可执行。新增或修改内置 Skill 后需要重启 Backend，本版不提供 refresh、上传或 watcher。

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

- Planner：初始动态上下文 4K，最多 2 个研究回合、4 次只读工具调用，EvidencePackage 最多 4K。
- Writer：初始动态上下文 6K；正文输出额度按目标字数动态计算，默认下限 6144、上限 12000 Token。
- Reviewer：初始上下文 10K，最多 2 个研究回合、6 次只读工具调用，EvidencePackage 最多 6K；报告阶段无查询能力。
- 定向复查：最多 1 个研究回合、3 次只读工具调用，EvidencePackage 最多 3K。
- Analyzer：初始上下文 15K，最多 2 个研究回合、4 次只读工具调用，EvidencePackage 最多 4K。
- 最终交付通过 Pydantic DTO 校验；格式失败时先无工具修复一次。
- 工具参数错误、空结果、超时、临时失败、重复调用、预算耗尽和结果过大由统一只读 Tool Runtime 处理。
- 默认门禁：审稿分数 ≥ 80；正文低于目标 0.5 倍要求扩写，1.5～1.8 倍记录观察，超过 1.8 倍触发压缩。
- 同一本书同时仅允许一个 `book_write` Job；普通聊天不占写锁。

上述 Token、工具次数和研究回合都是初始配置，应根据实际 Token 估算偏差、Evidence 使用率、截断率、修复率、质量门禁结果、延迟与成本持续调整，不应视为模型能力上限。

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
├── database/schema.sql       # 空 PostgreSQL 的完整初始 Schema
├── docs/
├── runtime/                  # 本地运行产物（Git 忽略）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

`data/` 是被忽略的历史本地文件保留区；服务不会读取、迁移或修改其中内容。

## 技术栈与边界

- Python 3.11、asyncio、OpenAI Agents SDK、Pydantic 2、Tenacity
- FastAPI、Uvicorn、SSE、PostgreSQL 16、SQLAlchemy 2、Alembic、psycopg
- React、TypeScript、Vite、Docker Compose
- 单用户、单 FastAPI 实例、进程内 JobSupervisor；不包含认证、多租户、分布式队列或多实例 Worker。
- 服务重启会将运行中的 Job 标记为 `interrupted`；已原子提交章节不会重复写入，模型调用本身不承诺 exactly-once。
