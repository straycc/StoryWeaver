# Runtime 运行产物

该目录由本地 FastAPI 与 OpenAI Agents SDK 自动写入运行数据：

- `logs/`：服务和 Agent 阶段日志；
- `traces/`：本地 SDK Trace JSONL；
- `diagnostics/`：模型失败诊断。

除本说明外，其余内容均不会提交到 Git。启动服务或首次模型调用后会自动创建相应子目录。
