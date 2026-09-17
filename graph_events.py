"""通过 LangGraph custom 流输出实际工具执行的开始/结束事件。"""
import asyncio
import time

from langgraph.config import get_stream_writer


def start(request):
    writer = get_stream_writer()
    call = request.tool_call
    data = {"tool_call_id": call["id"], "name": call["name"], "args": call["args"]}
    writer({"type": "tool_start", "data": data})
    return writer, data, time.monotonic()


def end(writer, data, started, result=None, status=None):
    writer({"type": "tool_end", "data": {
        **data, "status": status or getattr(result, "status", "success"),
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
        "content": getattr(result, "content", ""),
        "artifact": getattr(result, "artifact", None),
    }})


def wrap_tool_call(request, execute):
    writer, data, started = start(request)
    try:
        result = execute(request)
    except Exception:
        end(writer, data, started, status="error")
        raise
    end(writer, data, started, result)
    return result


async def awrap_tool_call(request, execute):
    writer, data, started = start(request)
    try:
        result = await execute(request)
    except asyncio.CancelledError:
        end(writer, data, started, status="cancelled")
        raise
    except Exception:
        end(writer, data, started, status="error")
        raise
    end(writer, data, started, result)
    return result
