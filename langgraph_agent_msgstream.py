"""基于 langgraph 的 agent，用 stream_mode="messages" 实现流式输出（langgraph_agent.py 的副本）。

和「在节点里 print」不同，这里节点保持纯净（call_llm 仍用 llm.invoke），
流式打印完全交给 app.stream(stream_mode="messages") 来做。langgraph 会通过
_StreamingCallbackHandler 让 invoke 内部自动走流式，从而逐 token 吐出 chunk。

stream_mode=["messages", "values"] 混合模式：
- ('messages', (chunk, metadata)) -> 逐 token / 逐消息 chunk
- ('values', state_dict)          -> 每个 super-step 结束时的完整状态（用于拿最终 messages）
"""
import os
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.runnables import Runnable
from deepseek_chat import DeepSeekChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import SecretStr

from langgraph_tools import tools

# ---- 可调参数 ----
MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"
SYSTEM_PROMPT = "你是一个helpful的助手"


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


def build_app(llm):
    """用给定的 LLM 构建并编译 langgraph 图，方便注入假 LLM 做测试。"""
    def call_llm(state: State) -> dict:
        # 这里保持 llm.invoke：stream_mode="messages" 会让它内部自动流式
        response = llm.invoke(state["messages"])
        return {"messages": [response]}

    g = StateGraph(State)
    g.add_node("call_llm", call_llm)
    g.add_node("tools", ToolNode(tools=tools))
    g.add_edge(START, "call_llm")
    g.add_conditional_edges(
        "call_llm",
        should_continue,
        {"tools": "tools", "end": END},
    )
    g.add_edge("tools", "call_llm")
    return g.compile()


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
