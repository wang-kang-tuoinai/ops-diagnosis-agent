# Agent 学习示例

保留项目从手写循环到 LangGraph 的学习过程。正式 HTTP 服务使用仓库根目录的 `server.py` 和 `langgraph_agent_msgstream.py`，不从这里导入实现。

| 文件 | 内容 |
| --- | --- |
| `agent.py` | 手动管理模型请求、工具执行和多轮消息的 Agent 循环 |
| `tools.py` | 手写工具 schema 和函数映射，仅供手写 Agent 使用 |
| `langgraph_agent.py` | 基础 LangGraph 循环示例 |
| `langgraph_agent_streaming.py` | 在节点内处理流式输出的早期示例 |

基础工具函数统一复用根目录 `common_tools.py`。两个 LangGraph 示例仍使用当前 `langgraph_tools.py` 工具列表，因此运行观测/知识工具也需要配置相应服务；示例保留原来的图与提示词，不等同于正式诊断行为。

在 **ops-diagnosis-agent 根目录**安装项目依赖并设置 `DEEPSEEK_API_KEY` 后，以模块方式运行：

```powershell
.\.venv\Scripts\python.exe -m examples.agent
.\.venv\Scripts\python.exe -m examples.langgraph_agent
.\.venv\Scripts\python.exe -m examples.langgraph_agent_streaming
```

三条命令分别启动交互示例，按需要选择其中一条。使用联网搜索还需 `TAVILY_API_KEY`。请从仓库根目录使用 `-m`，不要直接运行子目录脚本，以保证共用模块能够正确导入。

正式诊断 CLI 运行方式：

```powershell
.\.venv\Scripts\python.exe langgraph_agent_msgstream.py
```

示例不负责 HTTP 会话存储，历史仅保留在当前运行进程中。
