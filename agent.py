import json
from tools import available_functions, tools
from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
)
import os

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
            tool_choice="auto"          # 让模型自行决定是否调用
        )
        print("Assistant: ",end="")
        collected = ""
        collected_tool_calls: list[ChatCompletionMessageFunctionToolCallParam] = []  # 注意：tool_calls 也是增量拼接的
        usage = None
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                print(delta.content,end="",flush=True)
                collected += delta.content
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
            messages.append({"role":"assistant","content":collected})
            break

        # 走到这里,说明有工具调用
        print("\nSystem: 模型请求调用工具")

        # 构造消息
        assistant_message: ChatCompletionAssistantMessageParam = {
            "role": "assistant",
            "content": collected,
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