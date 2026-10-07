# 会话存储与 HTTP / SSE 协议

本文描述 `server:app` 的接口契约；启动与配置见 [项目 README](../README.md)。

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
| GET | `/api/v1/conversations/{conversation_id}/messages?limit=20` | 默认加载最近 20 轮，传 before 向前翻页 |
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
首次不传 `before`，默认加载最近 20 轮；每页 `items` 都按从旧到新排列，最新消息在底部。
`has_more=true` 表示还有更早的历史，将 `next_cursor` 传入下一次的 `before`（正整数），
例如 `?limit=20&before=42`，只查询 `seq < 42` 的记录。前端将该页插入现有列表顶部。
无更多历史时 `next_cursor` 为 `null`。`limit` 的单位是一轮执行，不是单条聊天气泡。
这里是执行记录的游标，与 SSE 事件内部的 `seq` 不是同一个字段。
会话列表使用 `next_offset` 分页。

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

