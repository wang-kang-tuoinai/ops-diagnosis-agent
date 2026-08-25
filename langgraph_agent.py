"""基于 langgraph 的 agent。

结构仿照 agent.py：把「创建 LLM」「构建图」「跑一轮」「打印消息」拆成独立函数，
每段都能单独测试或导入到其他代码；用 stream_mode="values" 逐节点打印中间过程。
"""
import os
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
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
    llm = ChatOpenAI(
        model=MODEL,
        api_key=SecretStr(api_key),
        base_url=BASE_URL,
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


def _tool_call_name(tc) -> str:
    """兼容 dict / pydantic 两种 tool_call 形态，取名字。"""
    if isinstance(tc, dict):
        return tc.get("name", "?")
    return getattr(tc, "name", "?")


def _tool_call_args(tc):
    if isinstance(tc, dict):
        return tc.get("args", {})
    return getattr(tc, "args", {})


def print_message(msg) -> None:
    """把一条消息打印成人眼易读的格式，展示中间过程。"""
    if isinstance(msg, HumanMessage):
        return  # 用户输入已经在输入时展示过，不重复打印
    if isinstance(msg, ToolMessage):
        print(f"[工具 {msg.name} 返回] {msg.content}")
        return
    if isinstance(msg, AIMessage):
        if msg.content:
            print(f"AI: {msg.content}")
        for tc in msg.tool_calls or []:
            print(f"  ↳ 请求调用工具 {_tool_call_name(tc)}，参数: {_tool_call_args(tc)}")
        return


def run_turn(app, messages: list, user_input: str) -> list:
    """跑完一轮对话：注入 user 消息 → 逐节点流式打印过程 → 返回追加后的消息列表。

    不修改传入的 messages，返回的是包含本轮所有消息的新列表。
    """
    current = list(messages) + [("user", user_input)]
    printed = len(messages)  # 已经展示过的消息数，只打印本轮新增的
    final_state: dict | None = None

    for state in app.stream({"messages": current}, stream_mode="values"):
        final_state = state
        msgs = state["messages"]
        for msg in msgs[printed:]:
            print_message(msg)
        printed = len(msgs)

    # 理论上不会走到这里：图至少会执行 call_llm 节点，stream 必有输出
    if final_state is None:
        return current
    return final_state["messages"]


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
