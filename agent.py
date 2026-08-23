import json
from tools import available_functions, tools
from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
)
import os


class DeepSeekAssistantMessageParam(ChatCompletionAssistantMessageParam, total=False):
    """在官方 assistant 消息基础上，补上 DeepSeek 特有的 reasoning_content 字段。

    thinking 模式下这个字段必须原样回传，否则下一轮请求会被拒绝。
    """
    reasoning_content: str


client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"),base_url="https://api.deepseek.com")

# 执行工具调用
def execute_tool_call(tool_call):
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

messages: list[ChatCompletionMessageParam] = [
    {"role": "system", "content":"你是一个helpful的助手"}
]
# 模型工具调用的最大迭代次数
MAX_ITERATIONS = 5
print("对话开始,输入quit退出对话")

#TODO 上下文只增不减
while True:
    user_input = input("你: ")
    if user_input.lower() == "quit":
        print("对话结束。")
        break
    
    messages.append({"role": "user", "content":user_input})
    
    for i in range(MAX_ITERATIONS):
        stream = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            stream = True,
            tools=tools,                # 传入工具定义
            tool_choice="auto",          # 让模型自行决定是否调用
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        collected_content = ""
        collected_reason = ""
        collected_tool_calls: list[ChatCompletionMessageFunctionToolCallParam] = []  # 注意：tool_calls 也是增量拼接的
        usage = None
        thinking_started = False  # 是否已打印 "Thinking:" 前缀
        content_started = False   # 是否已打印 "Assistant:" 前缀
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
                        collected_tool_calls.append({
                            "id": "",
                            "type": "function",
                            "function":{"name": "","arguments": ""}
                        })
                    if tc_delta.id:
                        collected_tool_calls[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            collected_tool_calls[idx]["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            collected_tool_calls[idx]["function"]["arguments"] += tc_delta.function.arguments
            if chunk.usage:
                usage = chunk.usage
        
        if not collected_tool_calls:
            # 说明模型没有调用工具,直接返回对话
            print()
            if usage:
                print("本次消耗",usage.total_tokens,"tokens")
            final_message: DeepSeekAssistantMessageParam = {
                "role": "assistant",
                "content": collected_content,
            }
            if collected_reason:
                final_message["reasoning_content"] = collected_reason
            messages.append(final_message)
            break

        # 走到这里,说明有工具调用
        print(f"\ntool_call: 模型请求调用工具:{[call["function"]["name"] for call in collected_tool_calls]}")

        # 构造消息
        assistant_message: DeepSeekAssistantMessageParam = {
            "role": "assistant",
            "content": collected_content,
            "reasoning_content": collected_reason,
            "tool_calls": collected_tool_calls,
        }

        # 把模型请求工具调用的消息添加到消息列表
        messages.append(assistant_message)

        # 依次执行工具调用
        for tc in collected_tool_calls:
            result = execute_tool_call(tc)
            print(f"  - 调用 {tc['function']['name']}，结果: {result}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result
        })
    else:
        print(f"\n[警告] 达到最大迭代次数 {MAX_ITERATIONS},强制中止")