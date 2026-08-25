"""基于 langgraph 的 agent，带流式输出和思考模式（langgraph_agent.py 的增强版副本）。

结构仿照 agent.py，拆成独立函数方便单独测试或导入。call_llm 节点用 llm.stream
实现打字机式流式输出，并把 DeepSeek 的 reasoning_content 打印成 "Thinking:" 前缀。
"""
import os
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import SecretStr

from deepseek_chat import DeepSeekChatOpenAI
from langgraph_tools import tools

# ---- 可调参数 ----
MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"
SYSTEM_PROMPT = "你是一个helpful的助手"


class State(TypedDict):
    messages: Annotated[list, add_messages]


def create_llm() -> Runnable:
    """创建并返回绑定了工具、开启了思考模式的 LLM。缺少 API key 时尽早报错。"""
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


def _tool_call_name(tc) -> str:
    """兼容 dict / pydantic 两种 tool_call 形态，取名字。"""
    if isinstance(tc, dict):
        return tc.get("name", "?")
    return getattr(tc, "name", "?")


tool_node = ToolNode(tools=tools)


def run_tools(state: State) -> dict:
    """执行工具调用，并打印每个工具的返回结果。"""
    result = tool_node.invoke(state)
    for msg in result.get("messages", []):
        if isinstance(msg, ToolMessage):
            print(f"[工具 {msg.name} 返回] {msg.content}")
    return result


def build_app(llm):
    """用给定的 LLM 构建并编译 langgraph 图，方便注入假 LLM 做测试。"""
    def call_llm(state: State) -> dict:
        full = None
        thinking_started = False
        content_started = False
        for chunk in llm.stream(state["messages"]):
            reasoning = chunk.additional_kwargs.get("reasoning_content")
            if reasoning:
                if not thinking_started:
                    print("Thinking: ", end="", flush=True)
                    thinking_started = True
                print(reasoning, end="", flush=True)
            if chunk.content:
                if not content_started:
                    if thinking_started:
                        print()  # 思考结束，换行后再输出回答
                    print("AI: ", end="", flush=True)
                    content_started = True
                print(chunk.content, end="", flush=True)
            full = chunk if full is None else full + chunk

        if full is None:
            return {"messages": []}
        if full.tool_calls:
            names = [_tool_call_name(tc) for tc in full.tool_calls]
            print(f"\n  ↳ 请求调用工具 {names}")
        else:
            print()  # 正常回答结束，换行
        return {"messages": [full]}

    g = StateGraph(State)
    g.add_node("call_llm", call_llm)
    g.add_node("tools", run_tools)
    g.add_edge(START, "call_llm")
    g.add_conditional_edges(
        "call_llm",
        should_continue,
        {"tools": "tools", "end": END},
    )
    g.add_edge("tools", "call_llm")
    return g.compile()


def run_turn(app, messages: list, user_input: str) -> list:
    """跑完一轮对话：注入 user 消息 → 执行图（节点内部已流式打印）→ 返回新消息列表。"""
    current = list(messages) + [("user", user_input)]
    final = app.invoke({"messages": current})
    return final["messages"]


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
