"""将图流转换为 SSE，维护单轮执行及与前端展示无关的 checkpoint 指针。"""
import asyncio
import copy
import json
import logging
import time
from uuid import uuid4

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from conversation_store import now_ms, RunConflict

logger = logging.getLogger(__name__)


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(block.get("text", "") for block in value
                       if isinstance(block, dict) and block.get("type") == "text")
    return ""


class DiagnosisRun:
    def __init__(self, manager, conversation, run_id, question):
        self.manager = manager
        self.conversation_id = conversation["conversation_id"]
        self.checkpoint_id = conversation["checkpoint_id"]
        self.run_id, self.question = run_id, question
        self.queue = asyncio.Queue(maxsize=128)
        self.events = []
        self.seq = 0
        self.last_flush = time.monotonic()
        self.emitted = {}
        self.open_tools = {}
        self.terminal = False
        self.cancel_requested = False
        self.task = None
        self.started = False
    # 生成事件，相同类型合并
    def record(self, kind, data):
        self.seq += 1
        data = json.loads(json.dumps(data, ensure_ascii=False, default=str))
        event = {"type": kind, "conversation_id": self.conversation_id, "run_id": self.run_id,
                 "seq": self.seq, "timestamp": now_ms(), "data": data}
        # SSE 逐增量发送，历史合并相邻同类文本，避免每个 token 都成为一条记录。
        if (kind in {"thinking", "content"} and self.events
                and self.events[-1]["type"] == kind
                and self.events[-1]["data"].get("message_id") == data.get("message_id")):
            self.events[-1]["data"]["delta"] += data["delta"]
        else:
            self.events.append(copy.deepcopy(event))
        return event

    # 落库(节流)+推入队列
    async def emit(self, kind, data):
        event = self.record(kind, data)
        if kind not in {"thinking", "content"} or time.monotonic() - self.last_flush >= 1:
            await self.manager.store.save_events(self.run_id, self.events)
            self.last_flush = time.monotonic()
        await self.queue.put(event)

    async def message(self, message, fallback_id, final=False):
        message_id = message.id or fallback_id
        for kind, value in (("thinking", message.additional_kwargs.get("reasoning_content", "")),
                            ("content", text_content(message.content))):
            if not isinstance(value, str) or not value:
                continue
            key = (message_id, kind)
            previous = self.emitted.get(key, "")
            if final:
                # 无流式能力的模型也能返回完整内容；流式模型的最终消息不能重复输出。
                value = value[len(previous):] if value.startswith(previous) else (value if not previous else "")
            if value:
                self.emitted[key] = previous + value
                await self.emit(kind, {"message_id": message_id, "delta": value})

    def close_tools(self, status):
        events = []
        for data in self.open_tools.values():
            events.append(self.record("tool_end", {**data, "status": status,
                                                  "content": "执行中断，未获得完整结果", "artifact": None}))
        self.open_tools.clear()
        return events

    async def terminate(self, status, error):
        tool_events = self.close_tools(status)
        error_event = self.record("error", {"status": status, "message": error})
        done_event = self.record("done", {"status": status})
        try:
            await self.manager.store.finish(self.conversation_id, self.run_id, status, self.events, error=error)
            self.terminal = True
        except Exception:
            logger.exception("保存诊断失败状态失败，run_id=%s", self.run_id)
            # 不发送伪造的持久化成功通知；遗留 running 在下次启动恢复为 interrupted。
            error_event = {**error_event, "data": {"status": "failed", "message": "历史记录保存失败，请检查 MySQL"}}
            done_event = None
        for event in (*tool_events, error_event, done_event):
            if event is not None:
                try:
                    self.queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    async def execute(self):
        self.started = True
        if self.cancel_requested:
            await self.terminate("cancelled", "客户端断开或主动停止，本轮已取消")
            return
        config = {"configurable": {"thread_id": self.conversation_id, "checkpoint_id": self.checkpoint_id},
                  "recursion_limit": 64}
        try:
            previous = await self.manager.graph.aget_state(config)
            if not previous.created_at:
                raise RuntimeError("会话 checkpoint 不存在，不能静默丢弃上下文")
            await self.emit("meta", {"status": "running"})
            async for mode, payload in self.manager.graph.astream(
                {"messages": [HumanMessage(content=self.question, id=f"{self.run_id}:user")]},
                config=config, stream_mode=["messages", "updates", "custom"], durability="sync",
            ):
                if mode == "messages":
                    message, metadata = payload
                    if metadata.get("langgraph_node") == "call_llm" and isinstance(message, AIMessage):
                        await self.message(message, f"{self.run_id}:{metadata.get('langgraph_step')}",
                                           final=not isinstance(message, AIMessageChunk))
                elif mode == "updates":
                    for message in payload.get("call_llm", {}).get("messages", []):
                        if isinstance(message, AIMessage):
                            await self.message(message, f"{self.run_id}:answer", final=True)
                elif mode == "custom" and payload.get("type") in {"tool_start", "tool_end"}:
                    kind, data = payload["type"], payload["data"]
                    if kind == "tool_start":
                        self.open_tools[data["tool_call_id"]] = data
                    else:
                        self.open_tools.pop(data["tool_call_id"], None)
                    await self.emit(kind, data)
            # 同一会话只有一个执行者，最新 checkpoint 即本轮完整结果。
            snapshot = await self.manager.graph.aget_state({"configurable": {"thread_id": self.conversation_id}})
            if snapshot.next:
                raise RuntimeError("图未执行完成")
            done = self.record("done", {"status": "completed"})
            try:
                await self.manager.store.finish(
                    self.conversation_id, self.run_id, "completed", self.events,
                    checkpoint_id=snapshot.config["configurable"]["checkpoint_id"],
                )
            except BaseException:
                self.events.pop()  # 未提交成功的 done 不能出现在失败历史中。
                raise
            self.terminal = True
            await self.queue.put(done)
        except asyncio.CancelledError:
            if not self.terminal:
                await self.terminate("cancelled", "客户端断开或主动停止，本轮已取消")
        except Exception:
            logger.exception("诊断失败，run_id=%s", self.run_id)
            if not self.terminal:
                await self.terminate("failed", "诊断执行失败，请查看服务日志；可修复后发起新一轮")

    async def cancel(self):
        if self.task and not self.task.done():
            if not self.cancel_requested:
                self.cancel_requested = True
                if self.started:
                    self.task.cancel()
            await asyncio.shield(self.task)

    async def stream(self):
        last_ping = time.monotonic()
        try:
            while True:
                if self.task.done() and self.queue.empty():
                    break
                try:
                    event = await asyncio.wait_for(self.queue.get(), timeout=1)
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"id: {self.run_id}:{event['seq']}\nevent: {event['type']}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    # 防止TCP连接被强行掐断
                    if time.monotonic() - last_ping >= 15:
                        last_ping = time.monotonic()
                        yield ": ping\n\n"
        finally:
            await self.cancel()


class DiagnosisManager:
    def __init__(self, graph, store):
        self.graph, self.store = graph, store
        self.active = {}

    async def create_conversation(self, title):
        conversation_id = str(uuid4())
        config = await self.graph.aupdate_state(
            {"configurable": {"thread_id": conversation_id}}, {"messages": []}, as_node="__start__",
        )
        return await self.store.create(conversation_id, title, config["configurable"]["checkpoint_id"])

    async def start(self, conversation_id, question, request_id=None):
        current = self.active.get(conversation_id)
        if current and not current.task.done():
            raise RunConflict("该会话正在生成，请等待结束或取消", current.run_id)
        run_id = str(uuid4())
        conversation = await self.store.reserve(conversation_id, run_id, request_id or str(uuid4()), question)
        session = DiagnosisRun(self, conversation, run_id, question)
        self.active[conversation_id] = session
        session.task = asyncio.create_task(session.execute(), name=f"diagnosis-{run_id}")
        def release(_):
            if self.active.get(conversation_id) is session:
                self.active.pop(conversation_id, None)
        session.task.add_done_callback(release)
        return session

    async def close(self):
        await asyncio.gather(*(session.cancel() for session in list(self.active.values())))
