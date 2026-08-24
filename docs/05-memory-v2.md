# StoryWeaver Memory V2 设计与实现

文档状态：已实现  
最后更新：2026-08-14

## 1. 设计目标

Memory V2 解决的不是“把所有内容都塞进 Prompt”，而是同时保留完整历史、稳定偏好、小说正史和角色主观认知，并在每次模型调用前按预算选择有限上下文。

四类持久化数据保持独立：

```text
data/
├── chat_sessions/{session_id}.jsonl
├── long_term_memory/
│   ├── global/{memory_id}.json
│   └── books/{book_id}/{memory_id}.json
├── books/{book_id}/...
└── simulations/{simulation_id}/character_memories/{owner_id}/{memory_id}.json
```

- Transcript 是完整、追加式会话历史。
- Session Memory 是 Transcript 中的滚动摘要事件。
- Long-term Memory 是可审计的跨会话偏好和作品指令。
- Novel State 仍由 `NovelProjectStore` 独占更新，是小说正史。
- Character Memory 是模拟角色的主观知识，按 owner 隔离。

## 2. Transcript V2

每个会话使用一份 JSONL 文件，每行一个不可变事件。公共字段为：

```text
schema_version, event_id, session_id, sequence,
event_type, created_at, payload
```

支持的事件包括 `session_created`、`book_bound`、`message_added`、动作、工具、摘要和记忆提取诊断。`ChatSession` 是从事件流重建的兼容投影。

Store 保证：

- 单实例并发写锁和连续 sequence。
- 原始事件只追加。
- 损坏的最后一行可忽略，并在下一次追加前修复。
- 旧版 `.json` 会话仍可读取；首次继续写入时生成 `.jsonl`，旧文件保留。
- Bootstrap 只返回会话摘要；完整 Timeline 使用游标分页读取。

## 3. Session Memory 与 Context 预算

默认 Chat Context 预算为 6000 个估算 Token，保留最近 12 条普通聊天原文。超过预算时，对更早的已完成消息生成 `summary_updated`：

```text
covered_through_sequence
current_goal
confirmed_decisions
user_constraints
completed_work
pending_work
important_references
```

摘要模型失败时使用确定性降级，不影响主聊天。摘要中的用户约束会拆成独立 protected 来源；当前用户指令、系统规则、小说权威摘要和未完成动作也不可静默删除。protected 来源自身超限时返回明确错误。

超过 800 个估算 Token 的工具结果不重复进入 Prompt。Agent Runtime 将完整结果原子保存到 `data/tool_results`，模型侧只接收状态、摘要和结果引用。

## 4. Long-term Memory

一条长期记忆对应一个 JSON 文件，包含类型、作用域、目录摘要、完整内容、重要性、来源、指纹、状态和替代关系。

类型：

- `user_preference`
- `feedback`
- `project_directive`
- `reference`

作用域：

- `global/default`：跨作品的通用用户偏好。
- `book/{book_id}`：人物、情节和作品风格指令。

普通聊天成功后，提取器只保存用户明确表达或确认的稳定信息。重复内容按 fingerprint 拒绝；冲突内容形成替代链，旧记录保留为 `superseded`。Memory 面板可以编辑、软停用或恢复记录。

检索先由代码过滤作用域和状态，再把最多 200 条 `name + description` 交给模型选择最多 5 条。辅助调用失败时，按关键词、重要性和新近度降级。

## 5. Context Manager 与 Trace

`SessionContextManager` 使用 `ContextItem`、`ContextPolicy`、`ContextPackage` 和 `ContextAssemblyTrace` 组装 Web Chat 上下文。优先级依次覆盖系统规则、当前指令、未完成动作、小说状态、长期记忆、会话摘要、最近消息和压缩工具结果。

每轮 Trace 记录：

- 实际选入来源。
- protected 来源。
- 选中的长期记忆 ID。
- 被排除和压缩的来源。
- 每个来源的处理原因。
- 估算 Token 与预算。

`ChapterContextBuilder` 保留原有小说上下文接口；写章 Pipeline 在构建章节 Context 前检索全局偏好和当前作品指令，并把非 reference 记忆作为 protected 条目注入。聊天长期记忆不能直接修改小说项目文件。

## 6. HTTP API 与前端

会话接口：

```text
GET  /api/sessions/{id}?before_sequence=&limit=50
POST /api/sessions/{id}/messages
GET  /api/sessions/{id}/context-trace/latest
```

Memory 接口：

```text
GET    /api/memories?scope_type=&scope_id=&status=
PATCH  /api/memories/{memory_id}
DELETE /api/memories/{memory_id}
POST   /api/memories/{memory_id}/restore
```

原生前端首次只加载最近 50 条 Timeline，向上分页时保持滚动位置；发送后只合并新增事件。动作、工具和错误使用折叠卡片，Memory 与 Context Trace 使用移动端兼容抽屉。切换会话使用请求版本号，旧请求结果不会覆盖新会话。

## 7. 故障边界

- 摘要、提取、检索和整理失败不得把已成功的主聊天改为失败。
- 原始 Transcript 不因摘要或压缩改变。
- 小说正史只能由既有创建/写章 Pipeline 更新。
- 角色私有记忆只允许所属 owner 路径读取。
- DELETE 长期记忆只改变状态，不物理删除文件。
- 当前实现面向本地单用户，不提供多进程文件锁、多租户权限或分布式事务。
