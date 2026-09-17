# 运维诊断 Agent 后端

基于原有 `langgraph_agent_msgstream.py`，增加 FastAPI、SSE 和会话持久化。
原 CLI 入口保留，HTTP 入口为 `server:app`。本次只完成 Agent 后端，rag-gateway 尚未改为这些接口的代理或前端。

## 启动

在 `ops-diagnosis-agent` 目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 首次配置时复制；如果已有 .env，请直接编辑已有文件。
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn server:app --env-file .env --host 127.0.0.1 --port 8001 --workers 1
```

启动前在 `.env` 填写 `DEEPSEEK_API_KEY`，并确认 MySQL 已运行。
默认连接本项目 Compose 的 `127.0.0.1:3306/ops_agent`，自动创建
`agent_conversations` 和 `agent_runs` 两张表；数据库本身需要事先存在。
这两张表与业务表独立，未使用存放观测日志的 `obs-mysql`。

环境变量：

| 变量 | 默认值 / 作用 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 必填，沿用现有 DeepSeek 模型配置 |
| `AGENT_MYSQL_HOST` | `127.0.0.1` |
| `AGENT_MYSQL_PORT` | `3306` |
| `AGENT_MYSQL_USER` | `root` |
| `AGENT_MYSQL_PASSWORD` | 代码默认空；示例文件使用 Compose 的开发密码 |
| `AGENT_MYSQL_DATABASE` | `ops_agent` |
| `AGENT_CHECKPOINT_PATH` | Agent 目录下 `data/checkpoints.sqlite` |
| `AGENT_KEEP_TURNS` | `20`，包括当前问题在内的最近用户轮次，至少 1 |
| `OBS_API_BASE` | `http://localhost:8082/api/v1` |
| `RAG_API_BASE` | `http://localhost:8000/api/v1` |

观测和知识检索工具仍使用已有地址配置，需要对应服务可访问。当前根目录 Compose
未向宿主机发布 rag-service 的端口；本地运行 Agent 时，需要本地启动 rag-service，
或将 `RAG_API_BASE` 指向实际可访问的地址。

启动后：

- `http://127.0.0.1:8001/health`：应用就绪信息，不会逐个探测下游依赖。
- `http://127.0.0.1:8001/docs`：请求与历史响应模型；流式效果建议使用支持 SSE 的客户端查看。

## 会话与存储

1. 服务端创建 `conversation_id`（UUID），同一个值作为 LangGraph 的 `thread_id`。
2. 后续请求携带该 UUID 和本轮问题，不上传整段历史。
3. 每次提问产生独立的 `run_id`；同一会话同时最多执行一轮，不同会话可以并发。
4. MySQL 保存会话列表、问题、状态和展示事件。前端查看历史从这里读取。
5. SQLite 的 `AsyncSqliteSaver` 保存图状态，包括模型消息、工具调用和结果，用于继续对话。

`agent_conversations.checkpoint_id` 指向最后一次成功完成的图状态。
取消或失败的部分输出仍保存在 MySQL 中供用户查看，但下一轮从上一次成功的
checkpoint 继续，不会自动续跑残留工具调用。刚创建的会话有一个空的初始 checkpoint。

成功时，先完成 SQLite checkpoint，再在一个 MySQL 事务内更新执行记录和 checkpoint 指针。
这不是跨数据库事务：极端崩溃可能遗留未被引用的 SQLite checkpoint；下次启动以 MySQL
中已提交的指针为准，并将遗留的 `running` 记录标记为 `interrupted`。
MySQL 和 SQLite 都需要持久化保存，只保留其中一个无法完整恢复此会话系统。

上下文轮次限制只裁剪本次模型输入，不删除 MySQL 展示历史，也不裁剪已保存的图消息。

## HTTP 接口

所有时间戳均为 Unix 毫秒；所有 ID 路径参数使用 UUID。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/v1/conversations` | 创建会话，返回 201 |
| GET | `/api/v1/conversations?limit=20&offset=0` | 按最近更新时间倒序列出会话 |
| POST | `/api/v1/conversations/{conversation_id}/messages` | 提问，返回 SSE |
| GET | `/api/v1/conversations/{conversation_id}/messages?limit=20&after=0` | 按执行顺序查询展示历史 |
| GET | `/api/v1/conversations/{conversation_id}/runs/{run_id}` | 查询一轮的状态和已有输出 |
| POST | `/api/v1/conversations/{conversation_id}/runs/{run_id}/cancel` | 停止一轮执行，返回最终记录 |

创建会话的 JSON 为 `{"title":"Redis 超时排查"}`，也可传 `{}` 使用默认标题。
响应包含 `conversation_id`、`title`、`created_at`、`updated_at` 和 `active_run_id`。

提问的请求体：

```json
{
  "question": "帮我检查 1789479485 到 1789479545 这个窗口的 Redis 异常",
  "request_id": "2a6ae512-6316-430d-bd30-dd907998ad19"
}
```

`request_id` 可选，建议前端每次提交生成一个 UUID。网络重试同一次提交时复用它，
真正的新一轮使用新值。相同会话内重复提交同一 ID 返回 409 和原 `run_id`，
客户端应查询原执行记录，不能将其当成一个新任务再次生成。
同一会话忙时也返回 409；不存在返回 404；请求参数不合法返回 422；
建立 SSE 之前发生 MySQL 错误返回 503。SSE 开始之后的故障通过事件表达。

历史接口的 `items` 每项是一轮执行，包含：

```text
seq, run_id, conversation_id, question, status,
created_at, updated_at, finished_at, error, events
```

`status` 取值为 `running/completed/failed/cancelled/interrupted`。
使用 `question` 渲染用户消息，使用 `events` 按顺序渲染 Agent 输出。
`has_more=true` 时，将 `next_cursor` 传入下一次的 `after`；这是执行记录的游标，
与 SSE 事件内部的 `seq` 不是同一个字段。会话列表则使用 `next_offset`。

## SSE 事件

HTTP 响应包含 `Content-Type: text/event-stream`、`X-Run-ID`，每约 15 秒可有一条
`: ping` 心跳注释。一次事件示例：

```text
id: <run_id>:3
event: content
data: {"type":"content","conversation_id":"<UUID>","run_id":"<UUID>","seq":3,"timestamp":1789479600000,"data":{"message_id":"<message_id>","delta":"发现 Redis 读超时"}}

```

公共字段由 `backend_models.StreamEvent` 定义。`data` 随事件类型变化：

| 事件 | data 字段 | 前端用途 |
| --- | --- | --- |
| `meta` | `status=running` | 获得公共字段中的 run_id，进入执行状态 |
| `thinking` | `message_id, delta` | 展示模型返回的 reasoning_content 增量；模型未返回时不会出现 |
| `content` | `message_id, delta` | 按 message_id 追加正文，可能包含工具调用前的说明 |
| `tool_start` | `tool_call_id, name, args` | 工具开始执行，显示名称与参数 |
| `tool_end` | 上述字段及 `status, content, artifact`；正常结束另有 `duration_ms` | 展示工具状态、结果和引用元数据 |
| `error` | `status, message` | 本轮失败或取消提示 |
| `done` | `status` | 本轮结束，解除输入框的执行状态 |

工具事件使用 `tool_call_id` 配对，支持并行执行；正常工具状态为 `success/error`，
执行中断时可为 `cancelled/failed`。不要仅凭某个 `tool_end` 就认为整轮结束。
RAG 的完整 `artifact` 保留给前端展示来源，其模型上下文仍由原工具的精简 content 决定。

流中按增量发送文本；MySQL 历史把相邻、同 message_id 的同类文本合并，
因此历史的事件 seq 可以不连续，不适合作为逐 token SSE 重放日志。
文本最多约每秒落库一次，工具事件和最终状态及时落库；异常掉电可能丢失最后一小段尚未保存的文本。

浏览器使用 `fetch` 发起 POST 并读取 `response.body`，原生 `EventSource` 不适用于该 POST 接口。
读取时需要累计缓冲，按 SSE 空行分帧，不能把一个网络 chunk 当作一条完整事件。
网关转发时也要关闭响应缓冲，并透传断连与取消。

本版本断开 SSE 连接会取消该轮生成；页面刷新后通过历史接口展示已有内容，
不会自动继续原生成，也不支持 `Last-Event-ID` 断点续传。
收到 `done` 后可刷新历史确认最终状态；没有收到 `done` 就断流时，应查询该 run。
数据库故障导致终态保存失败时，只发送错误，不伪报持久化完成。

## 实现文件

- `server.py`：应用生命周期、路由、SSE 响应、断连处理。
- `backend_models.py`：请求和历史响应模型。
- `diagnosis_runtime.py`：运行管理、图流到 SSE 的转换、取消及 checkpoint 衔接。
- `conversation_store.py`：MySQL 表和事务、历史记录、启动恢复。
- `graph_events.py`：实际工具执行的开始/结束通知。
- `langgraph_agent_msgstream.py`：共用图构建、模型输入裁剪及原 CLI。

## 验证

不调用真实模型或 MySQL 的测试（SQLite 使用真实临时文件）：

```powershell
.\.venv\Scripts\python.exe -m unittest test_backend test_rag_tools -v
```

真实 MySQL 集成测试需要可连接的 MySQL，以及能够创建/删除测试库的账户：

```powershell
$env:AGENT_TEST_MYSQL='1'
$env:AGENT_MYSQL_HOST='127.0.0.1'
$env:AGENT_MYSQL_PORT='3306'
$env:AGENT_MYSQL_USER='root'
$env:AGENT_MYSQL_PASSWORD='root'
.\.venv\Scripts\python.exe -m unittest test_mysql_backend -v
```

测试自行创建随机命名的 `test_ops_agent_<UUID hex>` 数据库并清理，
不在业务库中运行测试，模型仍为模拟模型。测试进程被强制杀死时可能留下该临时库。

## 当前运行边界

- 使用单进程、单实例（`--workers 1`），不要启动多个实例共享这些会话表。
  启动恢复会清理未完成记录，当前没有多实例任务租约。
- 当前接口面向本地项目演示，没有用户认证和会话归属隔离；后续接入网关时统一处理。
- 取消会停止图继续调度；已有同步 HTTP 工具在线程里执行时，只能等待其自身网络超时，
  不能保证下游请求立即被中止。现有诊断工具只读。
- 暂未实现会话删除、标题修改、SQLite 历史清理和生成断点续传。
