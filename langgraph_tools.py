"""将 tools.py 中的工具函数包装为 langgraph 可用的工具。

langchain 的 @tool 装饰器会自动生成工具的 JSON schema：
- 函数 docstring        -> 工具的 description
- 参数类型注解           -> schema 里的 type
- Annotated[类型, "描述"] -> schema 里该参数的 description

因此不需要再像 tools.py 里那样手写 tools 列表，只需给参数加上 Annotated 注解即可。
"""
import os
from typing import Annotated, Any

import requests
from langchain_core.tools import tool

from tools import calculate as _calculate
from tools import get_current_time as _get_current_time
from tools import search_web as _search_web

# obs-api 基础地址，可通过环境变量 OBS_API_BASE 覆盖
OBS_API_BASE = os.environ.get("OBS_API_BASE", "http://localhost:8082/api/v1")


def _get(endpoint: str, **params: Any) -> str:
    """GET 一个 obs-api 只读接口，过滤掉 None 参数，返回 JSON 文本。"""
    query = {k: v for k, v in params.items() if v is not None}
    resp = requests.get(f"{OBS_API_BASE}{endpoint}", params=query, timeout=30)
    if resp.status_code >= 400:
        return f"请求失败（HTTP {resp.status_code}）：{resp.text}"
    return resp.text


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


@tool
def query_log_stats(
    start: Annotated[int | None, "开始时间（秒级 Unix 时间戳）。不传默认取 end 前 1 小时，须小于 end。"] = None,
    end: Annotated[int | None, "结束时间（秒级 Unix 时间戳）。不传默认取当前时间。"] = None,
    service: Annotated[str | None, "按服务名过滤，如 'ops-agent-backend'。不传查所有服务。"] = None,
    level: Annotated[str | None, "按日志级别过滤，取值 DEBUG/INFO/WARN/ERROR。不传查所有级别。"] = None,
    route: Annotated[str | None, "按 HTTP 路由过滤，如 '/users'。"] = None,
    method: Annotated[str | None, "按 HTTP 方法过滤，如 GET/POST。"] = None,
    top_n: Annotated[int | None, "返回出现次数最多的模板数量。默认 10，最大 50。"] = None,
) -> str:
    """获取日志的整体统计（Level 0）。

    **诊断时应该首先调用这个工具**，它返回错误率、各级别计数、
    出现最多的日志模板，token 消耗最小。
    大多数"最近有没有异常"的问题在这一步就能回答。
    """
    return _get(
        "/logs/stats",
        start=start, end=end, service=service, level=level,
        route=route, method=method, top_n=top_n,
    )


@tool
def query_log_templates(
    start: Annotated[int | None, "开始时间（秒级 Unix 时间戳）。不传默认取 end 前 1 小时。"] = None,
    end: Annotated[int | None, "结束时间（秒级 Unix 时间戳）。不传默认取当前时间。"] = None,
    service: Annotated[str | None, "按服务名过滤，如 'ops-agent-backend'。"] = None,
    level: Annotated[str | None, "按日志级别过滤，取值 DEBUG/INFO/WARN/ERROR。"] = None,
    route: Annotated[str | None, "按 HTTP 路由过滤，如 '/users'。"] = None,
    method: Annotated[str | None, "按 HTTP 方法过滤，如 GET/POST。"] = None,
    limit: Annotated[int | None, "返回模板数量上限。默认 200，最大 500。"] = None,
) -> str:
    """按模板聚合日志（Level 1）。

    在 stats 发现异常后使用，可以看到每个模板的出现次数、
    首末时间和一条代表性样例。
    不要直接调用这个工具，应先调 query_log_stats 确认有异常。
    """
    return _get(
        "/logs/templates",
        start=start, end=end, service=service, level=level,
        route=route, method=method, limit=limit,
    )


@tool
def search_logs(
    start: Annotated[int | None, "开始时间（秒级 Unix 时间戳）。原始日志检索，建议尽量缩小时间窗。"] = None,
    end: Annotated[int | None, "结束时间（秒级 Unix 时间戳）。"] = None,
    service: Annotated[str | None, "按服务名过滤，如 'ops-agent-backend'。"] = None,
    level: Annotated[str | None, "按日志级别过滤，取值 DEBUG/INFO/WARN/ERROR。"] = None,
    route: Annotated[str | None, "按 HTTP 路由过滤，如 '/users'。"] = None,
    method: Annotated[str | None, "按 HTTP 方法过滤，如 GET/POST。"] = None,
    trace_id: Annotated[str | None, "只查某个 trace 的日志（关联某个请求的完整日志）。"] = None,
    template: Annotated[str | None, "只查某个模板的日志（模板指纹，从 query_log_templates 结果里拿）。"] = None,
    keyword: Annotated[str | None, "对日志消息做子串模糊搜索。会全表扫，须配合时间窗缩小范围。"] = None,
    limit: Annotated[int | None, "每页条数。默认 50，最大 100。"] = None,
    cursor: Annotated[str | None, "分页游标。取上页返回的 next_cursor 继续翻页，不传则从第一页开始。"] = None,
) -> str:
    """查询原始日志行（Level 2）。

    **只在前两步无法回答问题时使用**，返回的是未聚合的原始记录，
    token 消耗大。必须指定过滤条件（trace_id / template / level）缩小范围。
    """
    return _get(
        "/logs/search",
        start=start, end=end, service=service, level=level,
        route=route, method=method, trace_id=trace_id, template=template,
        keyword=keyword, limit=limit, cursor=cursor,
    )


@tool
def query_trace_stats(
    minutes_ago: Annotated[int, "查询最近多少分钟。默认 60。若指定了 start/end 则忽略此参数"] = 60,
    start: Annotated[int, "起始时间，秒级 Unix 时间戳。仅在查询历史特定时间段时使用"] = 0,
    end: Annotated[int, "结束时间，秒级 Unix 时间戳。仅在查询历史特定时间段时使用"] = 0,
    operation: Annotated[str, "按接口筛选，传接口名如 'POST /api/v1/users'。留空表示统计所有接口"] = "",
    limit: Annotated[int, "最多拉取多少条 trace 用于统计，默认 200，上限 500"] = 200,
) -> str:
    """从调用链（trace）角度统计各 HTTP 接口的耗时分布和健康状态。

    **这个工具能回答日志无法回答的问题**：一个请求表面上成功（HTTP 200），
    但内部某个环节（缓存、数据库、消息队列）其实出错了——这类"被降级掩盖的故障"
    只有 trace 能识别。

    返回内容：
    - by_status：请求按三种状态分类
      · ok       —— 全链路正常
      · degraded —— 根请求成功，但内部有环节报错（如 Redis 挂了降级到数据库，
                     用户拿到 200 但实际变慢了 100 倍）
      · failed   —— 请求本身失败（5xx）
    - entrypoints：每个接口的请求数、p50/p95/p99 耗时、failed/degraded 计数

    典型使用场景：
    - "系统有没有隐藏的问题" → 看 degraded 数量
    - "哪个接口慢" → 看 entrypoints 的 p95/p99
    - "哪个接口在报错" → 看 entrypoints 的 failed

    注意：只统计 HTTP 入口请求，不统计内部操作耗时。
    想知道某个接口的时间具体花在哪一层，需要下钻单条 trace。
    时间范围有两种指定方式：
    - 默认用 minutes_ago 查询最近一段时间（推荐，大多数诊断场景用这个）
    - 需要查询历史特定时间段时，传 start/end 绝对时间戳（需先用 get_current_time 确认当前时间）
    """
    import time

    # 若未提供绝对时间，回退到 minutes_ago
    if not start and not end:
        end = int(time.time())
        start = end - minutes_ago * 60

    return _get(
        "/traces/stats",
        service="ops-agent-backend",
        operation=operation or None,  # 空字符串不传，让后端走默认
        start=start,
        end=end,
        limit=limit,
    )



# langgraph / langchain 直接使用这个工具列表
tools = [search_web, calculate, get_current_time, query_log_stats, query_log_templates, search_logs, query_trace_stats]

