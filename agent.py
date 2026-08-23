import json
import os
from collections.abc import Sequence

from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
)

from tools import available_functions, tools

# ---- 可调参数 ----
MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"
SYSTEM_PROMPT = "你是一个helpful的助手"
MAX_ITERATIONS = 5  # 单轮对话里，模型最多连续请求几次工具调用
KEEP_TURNS = 2  # 每次请求前，保留最近几轮完整对话


class DeepSeekAssistantMessageParam(ChatCompletionAssistantMessageParam, total=False):
    """在官方 assistant 消息基础上，补上 DeepSeek 特有的 reasoning_content 字段。

    thinking 模式下这个字段必须原样回传，否则下一轮请求会被拒绝。
    """

    reasoning_content: str


def create_client() -> OpenAI:
    """创建 DeepSeek 客户端。缺少 API key 时尽早报错，而不是等到发请求。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "环境变量 DEEPSEEK_API_KEY 未设置，无法创建客户端。"
            "请先设置后再运行，例如：$env:DEEPSEEK_API_KEY='sk-xxx'"
        )
    return OpenAI(api_key=api_key, base_url=BASE_URL)


# 执行工具调用
def execute_tool_call(tool_call: ChatCompletionMessageFunctionToolCallParam) -> str:
    func_name = tool_call["function"]["name"]
    try:
        func_args = json.loads(tool_call["function"]["arguments"])
    except json.JSONDecodeError:
        return "参数解析失败，不是合法的 JSON"

    if func_name not in available_functions:
        return f"未知函数: {func_name}"

    try:
        return str(available_functions[func_name](**func_args))
    except Exception as e:
        return f"执行出错: {e}"


def split_into_turns(
    messages: Sequence[ChatCompletionMessageParam],
) -> list[list[ChatCompletionMessageParam]]:
    """把messages按user消息分成若干轮,每轮包含从这个user开始到下一个
    user之前的所有assistant/tool消息"""
    turns: list[list[ChatCompletionMessageParam]] = []
    current: list[ChatCompletionMessageParam] = []
    for msg in messages:
        if msg["role"] == "user" and current:
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)
    return turns


def truncate_messages(
    messages: Sequence[ChatCompletionMessageParam], keep_turns: int
) -> list[ChatCompletionMessageParam]:
    """保留System消息+最近keep_turns轮完整对话"""
    system_msgs: list[ChatCompletionMessageParam] = [
        m for m in messages if m["role"] == "system"
    ]
    # 单独拦掉 keep_turns<=0：切片 turns[-0:] 等价于 turns[0:]，
    # 会保留全部历史，跟"一轮都不留"的语义正好相反
    if keep_turns <= 0:
        return system_msgs
    rest = [m for m in messages if m["role"] != "system"]
    turns = split_into_turns(rest)
    kept = turns[-keep_turns:]
    result: list[ChatCompletionMessageParam] = list(system_msgs)
    for t in kept:
        result.extend(t)
    return result


def run_turn(
    client: OpenAI,
    messages: Sequence[ChatCompletionMessageParam],
    user_input: str,
    *,
    model: str = MODEL,
    max_iterations: int = MAX_ITERATIONS,
) -> list[ChatCompletionMessageParam]:
    """跑完一轮对话：注入 user 消息 → 工具调用循环 → 拿到最终回答。

    返回追加了本轮所有消息的新列表，不会修改传进来的 messages。
    """
    history: list[ChatCompletionMessageParam] = list(messages)
    history.append({"role": "user", "content": user_input})

    for _ in range(max_iterations):
        stream = client.chat.completions.create(
            model=model,
            messages=history,
            stream=True,
            tools=tools,  # 传入工具定义
            tool_choice="auto",  # 让模型自行决定是否调用
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
        )
        collected_content = ""
        collected_reason = ""
        collected_tool_calls: list[ChatCompletionMessageFunctionToolCallParam] = (
            []
        )  # 注意：tool_calls 也是增量拼接的
        usage = None
        thinking_started = False  # 是否已打印 "Thinking:" 前缀
        content_started = False  # 是否已打印 "Assistant:" 前缀
        for chunk in stream:
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                if not thinking_started:
                    print("Thinking: ", end="", flush=True)
                    thinking_started = True
                print(reasoning, end="", flush=True)
                collected_reason += reasoning
            if delta.content:
                if not content_started:
                    if thinking_started:
                        print()  # 思考内容结束后换行
                    print("Assistant: ", end="", flush=True)
                    content_started = True
                print(delta.content, end="", flush=True)
                collected_content += delta.content
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    while len(collected_tool_calls) <= idx:
                        collected_tool_calls.append(
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        )
                    if tc_delta.id:
                        collected_tool_calls[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            collected_tool_calls[idx]["function"][
                                "name"
                            ] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            collected_tool_calls[idx]["function"][
                                "arguments"
                            ] += tc_delta.function.arguments
            if chunk.usage:
                usage = chunk.usage

        if not collected_tool_calls:
            # 说明模型没有调用工具,本轮结束
            print()
            if usage:
                print("本次消耗", usage.total_tokens, "tokens")
            final_message: DeepSeekAssistantMessageParam = {
                "role": "assistant",
                "content": collected_content,
            }
            if collected_reason:
                final_message["reasoning_content"] = collected_reason
            history.append(final_message)
            return history

        # 走到这里,说明有工具调用
        called = [call["function"]["name"] for call in collected_tool_calls]
        print(f"\ntool_call: 模型请求调用工具:{called}")

        # 构造消息
        assistant_message: DeepSeekAssistantMessageParam = {
            "role": "assistant",
            "content": collected_content,
            "reasoning_content": collected_reason,
            "tool_calls": collected_tool_calls,
        }

        # 把模型请求工具调用的消息添加到消息列表
        history.append(assistant_message)

        # 依次执行工具调用
        for tc in collected_tool_calls:
            result = execute_tool_call(tc)
            print(f"  - 调用 {tc['function']['name']}，结果: {result}")
            history.append(
                {"role": "tool", "tool_call_id": tc["id"], "content": result}
            )

    print(f"\n[警告] 达到最大迭代次数 {max_iterations},强制中止")
    return history


def main() -> None:
    client = create_client()
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
    print("对话开始,输入quit退出对话")

    while True:
        user_input = input("你: ")
        if user_input.lower() == "quit":
            print("对话结束。")
            break
        messages = truncate_messages(messages, KEEP_TURNS)
        messages = run_turn(client, messages, user_input)


if __name__ == "__main__":
    main()
