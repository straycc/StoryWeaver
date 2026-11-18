# MainAgent 路由评测

`cases.yaml` 包含 50 条人工标注的路由用例，覆盖普通回复、只读查询、创作讨论、创作动作及澄清边界。脚本直接调用 `MainAgent.decide()`，不会执行 ActionSurface、创建作品或写入章节。

从项目根目录运行：

```bash
PYTHONPATH=backend/src python backend/route_evals/evaluate.py --check
PYTHONPATH=backend/src python backend/route_evals/evaluate.py --id reply_01 --id query_04 --id action_05
PYTHONPATH=backend/src python backend/route_evals/evaluate.py
```

在只有 `python3` 命令的环境中，将命令中的 `python` 换成 `python3`。运行真实模型前，需要安装 `requirements.txt` 并在 `~/.storyweaver/models.yaml`、`~/.storyweaver/.credentials.yaml` 配置默认模型。可用 `--provider` 与 `--model` 临时指定其他已配置模型。`--check` 只检查用例格式和能力 ID，不发起模型调用。

逐条结果写入 `runtime/route_evals/`。报告包含路由准确率、关键参数正确数、意外启动工作流数、选错工作流数、模型错误数和各类混淆。`expected.parameters` 只检查确定性字段；讨论目标和自由文本指令还需人工抽查。

这套用例评的是 **MainAgent 的结构化决策**。真实会话的 Context Builder、ActionSurface 校验和工作流执行属于下一层集成评测。用例中的上下文是按 Context Builder 的字段格式整理的固定快照，因此基线可重复，但不能代替端到端结果。修改提示词前应冻结一份用例，避免根据测试结果反向修改标准答案。
