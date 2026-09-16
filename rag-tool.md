# 运维知识工具

`rag_tools.py` 定义 `search_ops_knowledge`，由 `langgraph_tools.py` 注册。三个 LangGraph 入口共用该工具列表：`langgraph_agent.py`、`langgraph_agent_streaming.py` 和 `langgraph_agent_msgstream.py`。旧手写 `agent.py` 不使用此工具列表。

## 连接配置

先启动已完成入库的 rag-service。Agent 默认请求 `http://localhost:8000/api/v1/knowledge/search`，可在启动 Agent 前覆盖：

```powershell
$env:RAG_API_BASE = "http://localhost:8000/api/v1"
python langgraph_agent.py
```

容器中应填写 Agent 可访问的 rag-service 地址，例如 `http://rag-service:8000/api/v1`。该地址与观测接口的 `OBS_API_BASE` 分开配置。

## 调用与返回

参数为 `query`、可选 `doc_type`、`top_k`（默认 3，范围 1–5）。query 保留组件、关键操作和真实错误，围绕一个问题组织；不确定文档类型时省略过滤条件。

模型接收的字段：

- 公共字段：`ref/title/doc_type/content_mode/content`。
- 全文额外保留：`matched_sections`。
- 技术切片额外保留：`source_url`。章节路径已包含在正文中，不重复输出 `section`。
- 保留顶层 `notices`。正文按原样返回，不摘要或截断。

`doc_id/source/score/snapshot_id/chunk_id/chunk_index/component` 不额外传给模型。完整 HTTP 响应保存在 `ToolMessage.artifact["response"]`，`artifact["ref_to_item"]` 将引用映射到原始 `items` 下标，前端可通过此映射获取来源、内部 ID 和分数。

引用采用 `K-<16位哈希>`，依据文档身份、版本及模型可见内容生成。相同资料在多次或并行检索中保持相同引用，正文或版本变化则生成新引用；不依赖跨会话共享的自增计数器。模型使用 `[K-...]` 引用资料。

工具使用 `content_and_artifact` 返回格式。在 LangGraph ToolNode 执行下，模型消息只含精简 content，artifact 留在程序状态中。直接以普通参数调用 `tool.invoke` 时只返回 content；需要完整结果时应按 ToolCall 形式调用或从图的 ToolMessage 获取 artifact。

连接超时、HTTP 错误及响应格式错误转换为失败的工具消息，不当成成功的空结果。连接超时为 5 秒，读取超时为 60 秒；没有自动重试。

## 验证

```powershell
python -m unittest test_rag_tools -v
```

测试覆盖请求校验、字段投影、完整 artifact、发送给模型的消息结构、稳定引用、并行工具调用、服务异常及 Agent 图中调用工具后继续回答的流程。图测试使用模拟 LLM，不代表真实 LLM 的选工具或诊断质量评测。

本次 7 项测试通过，并通过新工具请求正在运行的本地 rag-service，验证 technology 和 runbook 均返回非空结果、模型字段已精简、完整 artifact 已保留。未调用真实 LLM，检索质量仍需用诊断场景评测。
