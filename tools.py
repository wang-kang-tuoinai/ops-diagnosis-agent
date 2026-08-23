import os
import ast
import operator
import json
from datetime import datetime
from openai.types.chat import ChatCompletionFunctionToolParam
# pyrefly: ignore [missing-import]
from tavily import TavilyClient

_tavily_client: TavilyClient | None = None


def get_tavily_client() -> TavilyClient:
    """惰性创建 Tavily 客户端，首次真正搜索时才初始化。

    缺 key 时抛 RuntimeError，由 search_web 捕获后转成给模型看的错误信息——
    搜索只是三个工具之一，不该因为它没配好就让整个 agent 起不来。
    """
    global _tavily_client
    if _tavily_client is None:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError(
                "环境变量 TAVILY_API_KEY 未设置，无法使用联网搜索。"
                "请先设置后再运行，例如：$env:TAVILY_API_KEY='tvly-xxx'"
            )
        _tavily_client = TavilyClient(api_key=api_key)
    return _tavily_client

# 安全计算器：基于 ast 模块，只允许加减乘除四则运算
def calculate(expression: str) -> str:
    """
    安全地计算数学表达式（仅支持 +、-、*、/）。
    使用 ast 解析语法树，拒绝任何非数学节点，防止代码注入。
    
    Args:
        expression: 数学表达式字符串，例如 "(3 + 5) * 2 / 4"
    Returns:
        计算结果字符串，或错误信息
    """
    # 白名单：只允许这些 AST 节点类型
    ALLOWED_NODES = (
        ast.Expression,  # 顶层表达式
        ast.BinOp,       # 二元运算: a + b
        ast.UnaryOp,     # 一元运算: -a
        ast.Constant,    # 数字常量: 3.14
        ast.Add,         # +
        ast.Sub,         # -
        ast.Mult,        # *
        ast.Div,         # /
        ast.USub,        # 一元负号: -5
        ast.UAdd,        # 一元正号: +5
    )

    # 支持的二元运算符映射
    BIN_OPS = {
        ast.Add:  operator.add,
        ast.Sub:  operator.sub,
        ast.Mult: operator.mul,
        ast.Div:  operator.truediv,
    }

    def _eval(node):
        """递归遍历 AST 节点并计算结果"""
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in BIN_OPS:
                raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise ZeroDivisionError("除数不能为零")
            return BIN_OPS[type(node.op)](left, right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            operand = _eval(node.operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        else:
            raise ValueError(f"不允许的表达式类型: {type(node).__name__}")

    print(f"\n[系统提示: 正在计算表达式 -> {expression}]")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        # 白名单校验：遍历所有节点，确保没有非法节点
        for node in ast.walk(tree):
            if not isinstance(node, ALLOWED_NODES):
                raise ValueError(f"包含不允许的节点类型: {type(node).__name__}，只支持加减乘除")
        result = _eval(tree)
        # 整数结果去掉小数点
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return f"{expression} = {result}"
    except ZeroDivisionError as e:
        return f"计算错误: {e}"
    except (SyntaxError, ValueError) as e:
        return f"表达式无效: {e}"
    except Exception as e:
        return f"计算失败: {e}"

# 获取当前系统时间
def get_current_time() -> str:
    """
    获取当前系统的本地时间。
    Returns:
    包含年月日时分秒的字符串，例如 "2026-08-03 14:47:19"
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 2. 定义搜索工具函数
def search_web(query: str) -> str:
    """供大模型调用的联网搜索工具"""
    print(f"\n[系统提示: 正在用 Tavily 搜索 -> {query}]")
    try:
        search_result = get_tavily_client().search(query, max_results=3)
        # 将结果转为 JSON 字符串返回给模型
        return json.dumps(search_result['results'], ensure_ascii=False)
    except Exception as e:
        return f"搜索失败，错误信息: {str(e)}"


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