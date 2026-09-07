# StoryWeaver 小说创作 A/B 评测

这套评测使用同一个底层模型，比较：

- `bare`：共享作品资料、当前章节任务和本组历史正文经过一次模型调用生成；
- `storyweaver`：相同共享资料和逐章任务经过正式创作 Pipeline 生成。

Case 中的 `input` 会进入两组生成上下文，`expectations` 只供评分器读取，不会作为额外提示注入生成模型。

## 运行前提

1. PostgreSQL 已启动并执行当前数据库迁移；
2. 根目录 `.env` 已配置模型和 `STORYWEAVER_DATABASE_URL`；
3. 在包含项目依赖的 Python 环境中运行。

## 先运行三章 Pilot

从仓库根目录执行：

```bash
python -m storyweaver.evaluation \
  --case backend/evals/cases/novel_ab_v1.yaml \
  --chapters 3 \
  --judge-passes 1
```

如果本地没有以 editable 模式安装项目：

```bash
PYTHONPATH=backend/src python -m storyweaver.evaluation \
  --case backend/evals/cases/novel_ab_v1.yaml \
  --chapters 3 \
  --judge-passes 1
```

只生成匿名人工评分材料、不调用模型 Judge：

```bash
PYTHONPATH=backend/src python -m storyweaver.evaluation \
  --chapters 3 \
  --judge-passes 0
```

Pilot 检查无误后运行完整12章，并交换匿名位置复评：

```bash
PYTHONPATH=backend/src python -m storyweaver.evaluation \
  --chapters 12 \
  --judge-passes 2 \
  --judge-model <独立且能力更强的模型名>
```

要重点测试跨章状态、物品归属、知识边界和时间线，使用复杂 Case：

```bash
PYTHONPATH=backend/src python -m storyweaver.evaluation \
  --case backend/evals/cases/snow-harbor-ledger-12.yaml \
  --chapters 12 \
  --judge-passes 2 \
  --judge-model <独立且能力更强的模型名>
```

`snow-harbor-ledger-12.yaml` 包含永久听觉缺陷、阶段性手伤、不会游泳、
上下两半暗码、三次铜钥匙交接、失踪与死亡区分、五日潮汐时间线、
身份延迟揭晓和执行者/幕后主使分层。它用于观察长程状态累积，不能只取前三章
代表最终质量结论。

未指定 `--judge-model` 时会复用生成模型，适合验证流程，不建议把这种 Pilot 结果直接写入简历。也可以通过 `STORYWEAVER_EVAL_JUDGE_MODEL` 指定。

如果章节已经生成完成、只有模型 Judge 失败，可直接复用原结果重新评分，
不连接 PostgreSQL，也不会重新生成正文：

```bash
PYTHONPATH=backend/src python -m storyweaver.evaluation \
  --judge-only backend/evals/results/<run_id> \
  --judge-passes 1
```

Judge 首次调用固定使用低强度推理和独立的 16K 输出额度；结构化结果无效时，
会关闭思考并进行一次 JSON 修复。最终仍失败时，生成状态保持不变，评分状态写入
`manifest.json`、`summary.json` 和 `case/grading/quality-summary.json`，原始模型输出
写入 `.env` 中 `STORYWEAVER_DIAGNOSTICS_DIR` 指定的目录。

运行期间会实时输出组别、章节、内部 Worker 和 Judge 进度，例如：

```text
19:58:01 [bare] 开始生成第 1/3 章
19:58:19 [bare] 完成第 1/3 章 · 18.20s · Token 2,418 · 调用 1 · 重试 0
19:59:02 [storyweaver] [开始] 章节规划
20:01:16 [storyweaver] 完成第 1/3 章 · 134.07s · Token 14,822 · 调用 7 · 重试 0
20:01:16 [judge] 开始评审第 1/3 章 · 第 1/1 轮
```

日志使用强制刷新；如果某一行长时间不变化，表示当前模型调用仍在等待响应或超时，而不是终端没有输出。

CLI 会在发起任何模型调用前检查 PostgreSQL 连接和 `books` 表。预检失败会立即退出，避免先产生裸模型或 Architect 费用后才发现数据库不可用。WSL 中直接运行 Python 时，`.env` 中的数据库地址必须能从 WSL 访问；Compose 容器内使用的 `postgres` 主机名不能直接照搬到宿主进程。

## 结果目录

每次运行写入 `backend/evals/results/<run_id>/`：

```text
manifest.json                         固定模型、参数、Case 和运行状态
summary.json                          成功率、Token、耗时与质量汇总
case/case.json                        本次冻结的完整 Case
case/bare/prompts/                    裸模型实际 Prompt
case/bare/chapters/                   裸模型正文
case/bare/metrics.json                裸模型逐章指标
case/storyweaver/chapters/            Pipeline 最终正文
case/storyweaver/metrics.json         Pipeline 逐章指标
case/blind/sample-A.md                匿名连续正文
case/blind/sample-B.md                匿名连续正文
case/blind/key.json                   A/B 真实映射
case/blind/scorecard.csv              人工盲评表
case/grading/rubric.json              隐藏逐章评分标准
case/grading/model-grades.json        模型逐项原始评分
case/grading/quality-summary.json     质量指标汇总
```

正文与指标在每章完成后原子写入。运行中断时，已完成章节仍保留，但当前版本不会自动从中断章节继续；重新运行会创建新的隔离评测作品。Judge 失败不改变生成结果，可使用 `--judge-only` 单独重试评分。

## 指标边界

自动报告包含：

- 硬约束遵循率；
- 章节计划节点完成率；
- 每千字 Canon 冲突数；
- 匿名章节胜负；
- 生成成功率；
- Token、模型调用次数、重试次数和耗时。

模型 Judge 的结果必须结合 `model-grades.json` 人工抽查。小样本 Pilot 只能验证评测流程，不能作为显著性结论。正式简历指标应来自冻结 Case 后的一次完整运行，不能根据生成结果反向修改 `expectations`。
