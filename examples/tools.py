"""手写 Agent 使用的函数映射和 OpenAI 工具 schema。"""
from openai.types.chat import ChatCompletionFunctionToolParam

from common_tools import calculate, get_current_time, search_web

# Agent可用函数字典
available_functions = {
    "calculate": calculate,
    "get_current_time": get_current_time,
    "search_web": search_web,
}

# 定义大模型的工具描述
tools: list[ChatCompletionFunctionToolParam] = [
     {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "当遇到你不知道的实时信息、新闻、或者需要查阅最新资料时，调用此工具进行联网搜索",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "需要搜索的关键词"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name" : "calculate",
            "description" : "安全的四则运算计算器。当用户需要计算加法(+)、减法(-)、乘法(*)、除法(/)的数学表达式时，调用此工具。支持括号和小数。不支持幂运算、三角函数等高级运算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "要计算的数学表达式，例如 '(3 + 5) * 2 / 4'，只能包含数字、加减乘除运算符和括号。"
                    }
                },
                "required": ["expression"]
            }
        }
    }, 
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