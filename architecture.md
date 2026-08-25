# LangChain 与 LangGraph 架构说明

> 本文整理自对 `langchain` / `langgraph` 源码的阅读与实验，结合本项目（`ops-diagnosis-agent`）里的实际代码。

## 1. 一句话分工

- **LangChain**：提供**零件**（模型、消息、工具、提示词）和**简单串行装配**（LCEL `|` 链）。
- **LangGraph**：提供**流水线调度**（状态、循环、条件分支、并行、持久化、流式）。

一句话：**LangChain 管「算啥」，LangGraph 管「啥时候、按什么顺序、在什么条件下算」。**

---

## 2. LangChain 的分层架构

```
┌──────────────────────────────────────────────────┐
│  编排/应用层：LCEL（Runnable │ Runnable）           │
│             Chain、AgentExecutor（旧）…           │
├──────────────────────────────────────────────────┤
│  工具层：@tool 装饰器、bind_tools、ToolNode        │
├──────────────────────────────────────────────────┤
│  消息层：BaseMessage                              │
│         HumanMessage / AIMessage / ToolMessage /  │
│         SystemMessage / AIMessageChunk / …        │
├──────────────────────────────────────────────────┤
│  模型层：BaseChatModel（统一抽象）                  │
│         ChatOpenAI / ChatDeepSeek / ChatAnthropic │
│         ↓ 内部做：_convert_message_to_dict（发）    │
│                   _convert_delta_to_message_chunk（收）│
│                   _generate_with_cache（调用+缓存）  │
├──────────────────────────────────────────────────┤
│  基础设施：Runnable 接口、Callback、缓存、序列化…    │
└──────────────────────────────────────────────────┘
```

### 各层职责

| 层 | 职责 | 对应源码文件 |
|---|---|---|
| 模型层 | 统一各供应商的调用接口；做「消息→payload」「delta→消息」转换 | `langchain_core/language_models/chat_models.py`、`langchain_openai/chat_models/base.py` |
| 消息层 | 统一消息格式（`BaseMessage` 及其子类） | `langchain_core/messages/ai.py` |
| 工具层 | `@tool` 装饰器、schema 推断、`bind_tools` | `langchain_core/tools/` |
| 编排层 | LCEL 链（`\|` 运算符组合 Runnable） | `langchain_core/runnables/` |

**关键点：LangChain 是无状态的。** 一条 LCEL 链本质是「有向无环图（DAG）」，没有循环、没有共享可变状态、没有「记住上次结果」。

---

## 3. LangGraph 的分层架构

```
┌──────────────────────────────────────────────────┐
│  API 层：StateGraph / MessageGraph / prebuilt     │
│         （写节点、边、State 定义的地方）            │
├──────────────────────────────────────────────────┤
│  图编译：compile() → CompiledStateGraph           │
├──────────────────────────────────────────────────┤
│  Pregel 执行引擎（langgraph/pregel/）             │
│   - super-step 模型（Bulk Synchronous Parallel）  │
│   - 每步：并行跑「就绪」的节点 → 用 reducer 合并回 state│
│   - 处理循环、fan-out/fan-in、条件边                │
├──────────────────────────────────────────────────┤
│  State 通道 + Reducer                             │
│   - State.messages: Annotated[list, add_messages] │
│   - add_messages / operator.add / 自定义          │
├──────────────────────────────────────────────────┤
│  横切能力：Streaming、Checkpoint、Interrupt、      │
│            subgraph、retry、time-travel            │
└──────────────────────────────────────────────────┘
```

### 各层职责

| 层 | 职责 | 对应源码文件 |
|---|---|---|
| API 层 | `StateGraph`、`add_node`、`add_edge`、`add_conditional_edges` | `langgraph/graph/state.py` |
| Pregel 引擎 | 执行调度、流式、并行、循环 | `langgraph/pregel/_loop.py`、`main.py` |
| 消息流处理 | `stream_mode="messages"` 的 token 流 | `langgraph/pregel/_messages.py` |
| State/reducer | 消息累积、状态合并 | `langgraph/graph/message.py` |

**关键点：LangGraph 是有状态 + 可循环的。** 它用 Pregel 模型（源自 Google 的图计算框架）驱动节点，核心是「super-step」：每个 super-step 里，所有输入就绪的节点并行执行，执行完用 reducer 把各自的更新合并回共享 state，再进入下一轮。

---

## 4. 两者如何配合（以本项目 agent 为例）

```
                 ┌────────────── LangGraph：控制流 ──────────────┐
                 │                                              │
 用户输入 ──────►│  State.messages: Annotated[list, add_messages] │
                 │                                              │
                 │  [START] → [call_llm] ⇄ [tools] → [END]      │
                 │               │  ▲      │                     │
                 │               └──┘      │                     │
                 │    条件边 should_continue│                     │
                 │    看最后一条消息有没有    │                     │
                 │    tool_calls 决定走哪边   │                     │
                 └───────────────┬──────────────▲─────────────────┘
                                 │              │
                    每个节点内部调用 LangChain 的零件
                                 │
                 ┌───────────────▼──────────────┐
                 │  call_llm 节点：              │
                 │    llm.invoke(state["messages"])│
                 └───────────────┬──────────────┘
                                 │
                 ┌───────────────▼──────────────┐
                 │  LangChain：ChatOpenAI         │
                 │   _convert_message_to_dict     │  ← 把 BaseMessage 转 payload
                 │        ↓ 调 API                │
                 │   _convert_delta_to_message_chunk│ ← 把 delta 转回 AIMessage
                 └───────────────────────────────┘
```

- **LangGraph** 决定：什么时候跑 `call_llm`、跑完去 `tools` 还是 `END`、消息怎么累积进 state、流式怎么往外吐。
- **LangChain** 负责：`call_llm` 节点里「真的和模型对话」——消息转 payload、调 API、响应转回 `AIMessage`。
- `tools` 节点用 `ToolNode`（LangGraph prebuilt），内部只是「读 state 里的 tool_calls → 调工具函数 → 把结果写成 ToolMessage 塞回 state」。

---

## 5. 关键机制要点（从源码阅读中得出的结论）

### 5.1 消息 → payload 是「白名单过滤」

`state` 里存的是完整的 `BaseMessage`（含 `id`、`usage_metadata`、`response_metadata`、`additional_kwargs`），但真正发给模型时，LangChain 的 `_convert_message_to_dict` 只提取白名单字段：

- `role`（从消息类型推导：`HumanMessage`→`user`、`AIMessage`→`assistant`、`ToolMessage`→`tool`）
- `content`
- `tool_calls`（仅 assistant）
- `tool_call_id`（仅 tool）

`id`、`usage_metadata`（token 计费）、`response_metadata` 等**都不会**回传。

> **注意**：这个过滤是 **LangChain** 做的，不是 LangGraph。LangGraph 只做 state 管理，不做「消息→payload」转换。

### 5.2 stream_mode="messages" 的流式机制

`stream_mode="messages"` 会挂一个 `_StreamingCallbackHandler`，使 `llm.invoke` 内部的 `_should_stream()` 返回 True，于是 `invoke` 自动改走流式。每个原始 delta 会**同时**做两件事：

1. 通过 `on_llm_new_token` → 直接 yield 原始 chunk（流式输出）。
2. `chunks.append(chunk)` → 收集后 `generate_from_stream` 拼成完整消息（进 state）。

因此**节点里保持 `llm.invoke` 即可获得 token 级流式**，无需手动 `llm.stream`。

### 5.3 reasoning_content 不会被回传

LangChain 的 `ChatOpenAI` 对 DeepSeek 的 `reasoning_content` 是**双向丢弃**：

- **接收**：`_convert_delta_to_message_chunk` 不提取 `reasoning_content`，直接丢弃。
- **发送**：`_convert_message_to_dict` 不读取 `reasoning_content`，即使手动塞进 `additional_kwargs` 也不回传。

而 DeepSeek 思考模式要求 `reasoning_content` 原样回传，所以需要自定义子类（本项目 `deepseek_chat.py`）来补这两件事。

### 5.4 流式里的 tool_calls 是分片到达的

真实流式里，工具调用的 `name`/`id` 只在**第一个 delta** 出现，`arguments` 逐段拼。所以：

- 拼接「完整工具调用」靠 LangChain 按 `index` 合并 `tool_call_chunks`（框架层通用）。
- 展示「调用了哪个工具」要读 `tool_call_chunks` 里带 `name` 的 chunk，不能读 `tool_calls`（后续分片会得到 `name=''` 的半成品）。

---

## 6. 类比总结

| | LangChain | LangGraph |
|---|---|---|
| 类比 | 发动机、轮胎、螺丝（零件）+ 简单传送带（LCEL） | 工厂的流水线调度系统 |
| 关注 | **what**（和模型怎么交互） | **when / how**（何时、按何顺序、何条件下执行） |
| 状态 | 无状态（DAG，无循环） | 有状态（循环、分支、共享 state） |
| 核心抽象 | `Runnable`、`BaseMessage`、`BaseChatModel` | `StateGraph`、节点、边、reducer、Pregel 引擎 |

**历史背景**：LangGraph 正是为解决 LangChain 的短板诞生——LCEL 链是无环、无状态的，而 agent 本质是「循环 + 状态 + 条件跳转」，LCEL 表达不了，所以有了基于 Pregel 的有状态图引擎 LangGraph。

---

## 7. 源码索引

| 概念 | 文件 |
|---|---|
| 消息层（`AIMessage`/`AIMessageChunk`） | `.venv/Lib/site-packages/langchain_core/messages/ai.py` |
| 模型层抽象（`_generate_with_cache`/`_should_stream`） | `.venv/Lib/site-packages/langchain_core/language_models/chat_models.py` |
| Provider 实现（`_convert_message_to_dict` 等） | `.venv/Lib/site-packages/langchain_openai/chat_models/base.py` |
| 图定义 | `.venv/Lib/site-packages/langgraph/graph/state.py` |
| Pregel 执行引擎 | `.venv/Lib/site-packages/langgraph/pregel/_loop.py`、`main.py` |
| stream_mode="messages" 处理器 | `.venv/Lib/site-packages/langgraph/pregel/_messages.py` |
