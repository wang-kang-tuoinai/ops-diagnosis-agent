"""CLI 与 HTTP 后端共用的诊断图。

节点提供 invoke / ainvoke 两条路径，由 LangGraph messages 流输出模型增量。
CLI 使用 messages + values 打印；后端使用 messages + updates + custom 生成 SSE，
并注入 AsyncSqliteSaver 持久化状态。上下文裁剪仅影响模型输入。
"""
import os
from typing import Annotated, TypedDict

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import Runnable, RunnableLambda, RunnableConfig
from deepseek_chat import DeepSeekChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import SecretStr

from langgraph_tools import tools
from graph_events import wrap_tool_call, awrap_tool_call
from system_prompt import SYSTEM_PROMPT

# ---- 可调参数 ----
MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"
KEEP_TURNS = 100  # 保留最近几轮完整对话


class State(TypedDict):
    messages: Annotated[list, add_messages]


def create_llm() -> Runnable:
    """创建并返回绑定了工具的 LLM。缺少 API key 时尽早报错。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "环境变量 DEEPSEEK_API_KEY 未设置，无法创建客户端。"
            "请先设置后再运行，例如：$env:DEEPSEEK_API_KEY='sk-xxx'"
        )
    llm = DeepSeekChatOpenAI(
        model=MODEL,
        api_key=SecretStr(api_key),
        base_url=BASE_URL,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
    )
    return llm.bind_tools(tools=tools)


def should_continue(state: State) -> str:
    """根据最后一条消息决定下一步：还有工具要调用就去 tools，否则结束。"""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "end"


def split_into_turns(messages: list) -> list[list]:
    """把消息按 user 消息分成若干轮，每轮从 user 开始到下一个 user 之前。"""
    turns: list[list] = []
    current: list = []
    for msg in messages:
        if isinstance(msg, HumanMessage) and current:
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)
    return turns


def truncate_messages(messages: list, keep_turns: int) -> list:
    """保留 system 消息 + 最近 keep_turns 轮完整对话。"""
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    # 单独拦掉 keep_turns<=0：切片 turns[-0:] 等价于 turns[0:]，
    # 会保留全部历史，跟「一轮都不留」的语义正好相反
    if keep_turns <= 0:
        return system_msgs
    rest = [m for m in messages if not isinstance(m, SystemMessage)]
    turns = split_into_turns(rest)
    kept = turns[-keep_turns:]
    result = list(system_msgs)
    for t in kept:
        result.extend(t)
    return result


def build_app(llm, keep_turns: int = KEEP_TURNS, checkpointer=None, tool_list=None):
    """CLI 与异步后端共用图；只裁剪模型输入，checkpoint 保留完整消息。"""
    if keep_turns < 1:
        raise ValueError("keep_turns 必须至少为 1")

    def context(state):
        # 系统规则由当前部署维护。旧 CLI/checkpoint 中的 system 消息不覆盖新版规则；
        # 仅替换本次模型输入，不修改持久化历史，保留完整 user/assistant/tool 轮次。
        conversation = [message for message in state["messages"] if not isinstance(message, SystemMessage)]
        messages = truncate_messages(conversation, keep_turns)
        return [SystemMessage(content=SYSTEM_PROMPT)] + messages

    def call_llm(state: State, config: RunnableConfig) -> dict:
        response = llm.invoke(context(state), config=config)
        return {"messages": [response]}

    async def acall_llm(state: State, config: RunnableConfig) -> dict:
        response = await llm.ainvoke(context(state), config=config)
        return {"messages": [response]}

    g = StateGraph(State)
    g.add_node("call_llm", RunnableLambda(call_llm, afunc=acall_llm))
    g.add_node("tools", ToolNode(
        tools=tools if tool_list is None else tool_list,
        wrap_tool_call=wrap_tool_call, awrap_tool_call=awrap_tool_call,
        handle_tool_errors=lambda exc: f"工具执行失败（{type(exc).__name__}），请检查服务状态。",
    ))
    g.add_edge(START, "call_llm")
    g.add_conditional_edges(
        "call_llm",
        should_continue,
        {"tools": "tools", "end": END},
    )
    g.add_edge("tools", "call_llm")
    return g.compile(checkpointer=checkpointer)


def run_turn(app, messages: list, user_input: str) -> list:
    """跑完一轮对话：流式打印 AI 输出与工具调用，返回追加本轮消息后的完整列表。"""
    current = list(messages) + [("user", user_input)]
    final_messages = messages
    thinking_started = False  # 本轮是否已开始输出 Thinking（用于控制前缀和换行）
    ai_started = False  # 本轮是否已开始输出 AI 文本（用于控制 "AI: " 前缀和换行）

    for mode, payload in app.stream(
        {"messages": current}, stream_mode=["messages", "values"]
    ):
        if mode == "values":
            final_messages = payload["messages"]
            continue

        msg_chunk, _metadata = payload
        if isinstance(msg_chunk, ToolMessage):
            if thinking_started:
                print()
                thinking_started = False
            if ai_started:
                print()
                ai_started = False
            print(f"[工具 {msg_chunk.name} 返回] {msg_chunk.content}")
        elif isinstance(msg_chunk, AIMessageChunk):
            # 思考内容：DeepSeekChatOpenAI 已把它捞进 additional_kwargs["reasoning_content"]
            reasoning = msg_chunk.additional_kwargs.get("reasoning_content")
            if reasoning:
                if not thinking_started:
                    print("Thinking: ", end="", flush=True)
                    thinking_started = True
                print(reasoning, end="", flush=True)

            # 工具调用在流式里是分片到达的：name/id 只在第一个分片，arguments 逐段拼。
            # 不能读 tool_calls（后续分片会得到 name='' 的半成品），要读 tool_call_chunks 里带 name 的 chunk。
            names = [tc["name"] for tc in msg_chunk.tool_call_chunks if tc.get("name")]
            if names:
                if thinking_started:
                    print()
                    thinking_started = False
                if ai_started:
                    print()
                    ai_started = False
                print(f"  ↳ 请求调用工具 {names}")
            elif msg_chunk.content:
                if thinking_started:
                    print()  # 思考结束，换行后再输出回答
                    thinking_started = False
                if not ai_started:
                    print("AI: ", end="", flush=True)
                    ai_started = True
                print(msg_chunk.content, end="", flush=True)

    if thinking_started or ai_started:
        print()
    return final_messages


def main() -> None:
    llm = create_llm()
    app = build_app(llm)
    messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
    print("对话开始，输入 quit 退出")

    while True:
        user_input = input("你: ")
        if user_input.lower() == "quit":
            print("对话结束。")
            break
        messages = run_turn(app, messages, user_input)


if __name__ == "__main__":
    main()
