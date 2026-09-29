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
from rag_tools import search_ops_knowledge

# obs-api 基础地址，可通过环境变量 OBS_API_BASE 覆盖
OBS_API_BASE = os.environ.get("OBS_API_BASE", "http://localhost:8082/api/v1")
TRACE_ENTRY_SERVICE = os.environ.get("TRACE_ENTRY_SERVICE", "ops-agent-backend").strip() or "ops-agent-backend"


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
    route: Annotated[str | None, "按 HTTP 路由过滤，如 '/users'。"] = None,
    method: Annotated[str | None, "按 HTTP 方法过滤，如 GET/POST。"] = None,
) -> str:
    """按服务返回日志统计（Level 0），不知道异常服务时先用它摸底。

    不传 service 返回窗口内有匹配日志的各服务；传 service 只返回该服务，summaries 始终是数组。
    每项包含 service、total、error_count、error_rate、by_level；window 位于响应顶层。
    error_rate 是该服务 ERROR 日志占比，不是请求失败率；同一次请求可能记录多条错误日志。
    不再支持 level/top_n，不返回模板。route/method 会限制统计范围，不代表整个服务的全部日志。
    空 summaries 不代表系统正常，也不是完整服务清单。根据 ERROR/WARN 分布选择服务后调用 query_log_templates。
    """
    return _get(
        "/logs/stats",
        start=start, end=end, service=service,
        route=route, method=method,
    )


@tool
def query_log_templates(
    service: Annotated[str, "必填，目标服务名，可从 query_log_stats 的 summaries 中获取。"],
    start: Annotated[int | None, "开始时间（秒级 Unix 时间戳）。不传默认取 end 前 1 小时。"] = None,
    end: Annotated[int | None, "结束时间（秒级 Unix 时间戳）。不传默认取当前时间。"] = None,
    level: Annotated[str | None, "按日志级别过滤，取值 DEBUG/INFO/WARN/ERROR。"] = None,
    route: Annotated[str | None, "按 HTTP 路由过滤，如 '/users'。"] = None,
    method: Annotated[str | None, "按 HTTP 方法过滤，如 GET/POST。"] = None,
    limit: Annotated[int | None, "返回模板数量上限。默认 200，最大 500。"] = None,
) -> str:
    """按模板聚合日志（Level 1）。

    限定一个服务，返回每个模板的出现次数、首末时间和最新一条代表性样例。
    已知服务及异常线索时可以直接调用；不知道服务时先用 query_log_stats。
    has_more=true 表示当前条件下还有未返回的模板分组，不是原始日志分页，也不提供 next_cursor。
    可提高 limit（最大 500）、按 level/route 筛选或缩小窗口；返回模板的 count 仍统计整个匹配窗口。
    sample 不证明该模板的其他日志均有相同原因。更多原始日志请用 search_logs。
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
    service: Annotated[str, "目标服务名，匹配其 server 入口，可位于链路中间；默认使用配置的单个服务"] = TRACE_ENTRY_SERVICE,
) -> str:
    """统计指定服务的 server 入口及其后代，查看接口耗时和下游错误证据，不包含上游和旁支。

    不知道服务名时可先使用默认服务；一次只查询一个服务，不代表所有服务或后台任务。
    operation 匹配目标服务自己的入口操作名，不要求是全局根。缺少上游时仍可统计已识别入口。
    total_calls 按 (trace_id, entry_span_id) 计数，同一 Trace 多次进入该服务会分别统计。
    entrypoints 按 service + operation 分组，返回调用数、p50/p95/p99、failed/degraded。
    未指定 operation 时自动发现 server 操作并逐个查询，默认各最多 1500 条候选；指定时默认最多 5000 条。
    无需传 limit；实际预算见 meta.per_operation_limit，已解析候选按 trace_id 去重计入 meta.fetched_traces。
    检查 meta.operation_queries：status 为 success/failed/skipped，失败或未执行不代表零调用。
    raw_trace_count 是各操作原始候选数；limit_reached 为 true 表示可能截断，不代表入口调用数。
    概览触顶时指定该 operation 重查，仍触顶则缩小时间窗口；不要把多次查询统计直接相加。
    downstream_error_services 是下游服务及 request_count：同一次入口调用内同一服务只计一次，
    多个服务的计数不可相加，不是下游自身错误率或根因认定。归属取错误 Span 自己的 service，
    不能从调用超时推断被调用服务也有错误。
    failed 表示入口自身有效错误或 HTTP 5xx；degraded 表示入口未失败但后代有有效错误；
    ok 仅表示已采集范围没有有效错误。只排除已确认的 MySQL 重复键业务冲突，4xx 不直接判 ok。
    degraded 不保证执行过业务降级，也可能返回 4xx。耗时包含下游等待，不能累加子 Span 耗时。
    先复制入口 service/operation 给 search_traces 查异常请求；也可根据下游服务名搜索其自身入口，
    此时不要把上游 operation 传给下游。search 的下游查询不限定同一上游，需用 trace_id 关联。
    注意 notices：仅统计已获取样本，候选不足或链路不完整时不能断言系统无异常。
    默认用 minutes_ago；历史窗口传 start/end 秒级时间戳。
    """
    import time

    # 若未提供绝对时间，回退到 minutes_ago
    if not start and not end:
        end = int(time.time())
        start = end - minutes_ago * 60

    return _get(
        "/traces/stats",
        service=service,
        operation=operation or None,  # 空字符串不传，让后端走默认
        start=start,
        end=end,
    )



@tool
def search_traces(
    service: Annotated[str, "目标服务名，匹配该服务的 server 入口，可位于跨服务链路中间"] = TRACE_ENTRY_SERVICE,
    operation: Annotated[str | None, "目标服务自己的接口名，如 GET /api/v1/users；不是上游接口或 Redis/MySQL 子操作"] = None,
    start: Annotated[int | None, "秒级 Unix 开始时间，默认最近一小时"] = None,
    end: Annotated[int | None, "秒级 Unix 结束时间，默认当前时间"] = None,
    status: Annotated[str | None, "目标入口及后代分类：ok/degraded/failed；不受上游和兄弟分支错误影响"] = None,
    min_duration_ms: Annotated[float | None, "目标服务入口 Span 耗时下限，毫秒，含等号"] = None,
    sort: Annotated[str, "duration_desc 耗时降序或 start_desc 最新优先"] = "duration_desc",
    limit: Annotated[int, "最终返回入口调用数，默认 10，上限 50；同一 Trace 可有多次调用"] = 10,
    fetch_limit: Annotated[int, "Jaeger 候选获取上限，默认 200，上限 500"] = 200,
) -> str:
    """查找具体的慢请求或异常请求，返回摘要与 trace_id，不返回 Span 树。

    宽泛诊断先使用 query_trace_stats 定位入口，再按入口、状态、耗时下钻。
    每项是指定服务的一次 server 入口调用，以 trace_id + entry_span_id 标识，不要求入口为全局根。
    耗时包含该入口执行期间的下游等待；状态和错误摘要只看该入口及后代，不含上游或兄弟分支。
    只排除已确认的 MySQL 重复键业务冲突，4xx 不直接判 ok。failed 为入口有效错误或 5xx，
    degraded 为后代有有效错误，ok 为已采集范围未见有效错误，不是业务请求必定成功。
    必须阅读 notices：排序仅针对已获取候选，空结果不能证明整个窗口无异常。
    fetched_count 是候选 Trace 数，matched_count/returned_count 是入口调用数。
    error_summary 为按时间排序子树、先后代后自身深度遍历找到的第一个有效错误；
    service/span_id/operation/message 来自同一错误节点，不保证最早、唯一或根因。
    用 trace_id 获取详情，再根据 entry_span_id 定位目标入口；详情顶层状态可能不同。
    """
    return _get("/traces/search", service=service, operation=operation, start=start,
                end=end, status=status, min_duration_ms=min_duration_ms,
                sort=sort, limit=limit, fetch_limit=fetch_limit)


@tool
def get_trace_detail(
    trace_id: Annotated[str, "从 search_traces 或日志获取的真实 trace_id"],
    max_spans: Annotated[int, "最多展示节点数，默认 50，上限 200；裁剪时可提高"] = 50,
) -> str:
    """查看单次请求的调用树，定位慢操作并检查各节点错误。

    已有 trace_id 可直接调用，无需重复 stats/search。保留正常节点以分析无错误的慢请求。
    start_offset_ms 相对全局根开始时间；没有唯一根时相对最早片段开始时间。
    root 保留完整上游关系，fragments 保留缺失父节点或多个根的独立片段，用 entry_span_id 定位。
    duration_ms 为节点总耗时；self_ms 是未被直接子
    Span 时间区间覆盖的耗时，可能包含未埋点等待，不是 CPU 时间。并行耗时不能直接相加。
    status_desc 表示操作失败描述；error 是记录的异常类型与消息，两者分别保留。
    顶层 status 为 ok/degraded/failed，无唯一全局根时为 unknown；节点 status 为原始归一化状态。
    expected_error 标记被排除的已处理 MySQL 重复键，原始 error/status_desc 仍保留。
    必须阅读 warnings/truncated：裁剪或缺失可能隐藏错误，不能据此断言没有其他异常。
    需要业务上下文时继续用 search_logs(trace_id=...)；错误节点不等于已确认根因。
    """
    from urllib.parse import quote
    return _get(f"/traces/{quote(trace_id, safe='')}", max_spans=max_spans)


# langgraph / langchain 直接使用这个工具列表
tools = [search_web, calculate, get_current_time, query_log_stats, query_log_templates, search_logs, query_trace_stats, search_traces, get_trace_detail, search_ops_knowledge]
