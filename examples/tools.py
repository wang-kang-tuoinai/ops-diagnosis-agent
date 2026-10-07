"""手写 Agent 使用的函数映射和 OpenAI 工具 schema。"""
from openai.types.chat import ChatCompletionFunctionToolParam

from common_tools import get_current_time

available_functions = {"get_current_time": get_current_time}

tools: list[ChatCompletionFunctionToolParam] = [
    {
        "type": "function",
        "function": {
            "name" : "get_current_time",
            "description": "获取当前系统的本地时间",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]