"""将 tools.py 中的工具函数包装为 langgraph 可用的工具。

langchain 的 @tool 装饰器会自动生成工具的 JSON schema：
- 函数 docstring        -> 工具的 description
- 参数类型注解           -> schema 里的 type
- Annotated[类型, "描述"] -> schema 里该参数的 description

因此不需要再像 tools.py 里那样手写 tools 列表，只需给参数加上 Annotated 注解即可。
"""
from typing import Annotated

from langchain_core.tools import tool

from tools import calculate as _calculate
from tools import get_current_time as _get_current_time
from tools import search_web as _search_web


@tool
def search_web(query: Annotated[str, "需要搜索的关键词"]) -> str:
    """当遇到不知道的实时信息、新闻，或需要查阅最新资料时，调用此工具进行联网搜索。"""
    return _search_web(query)


@tool
def calculate(
    expression: Annotated[
        str,
        "要计算的数学表达式，例如 '(3 + 5) * 2 / 4'，只能包含数字、加减乘除运算符和括号。",
    ]
) -> str:
    """安全的四则运算计算器。当用户需要计算加法(+)、减法(-)、乘法(*)、除法(/)的数学表达式时调用。支持括号和小数，不支持幂运算、三角函数等高级运算。"""
    return _calculate(expression)


@tool
def get_current_time() -> str:
    """获取当前系统的本地时间。"""
    return _get_current_time()


# langgraph / langchain 直接使用这个工具列表
tools = [search_web, calculate, get_current_time]
