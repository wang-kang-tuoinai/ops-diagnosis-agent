"""诊断后端：uvicorn server:app --port 8001 --workers 1。"""
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import anyio
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pymysql import MySQLError

from backend_models import (AskRequest, Conversation, ConversationList, CreateConversation,
                            HistoryResponse, RunView)
from conversation_store import MissingConversation, MySQLConversationStore, RunConflict
from diagnosis_runtime import DiagnosisManager
from langgraph_agent_msgstream import build_app, create_llm

logger = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent


@dataclass
class Settings:
    mysql_host: str = field(default_factory=lambda: os.getenv("AGENT_MYSQL_HOST", "127.0.0.1"))
    mysql_port: int = field(default_factory=lambda: int(os.getenv("AGENT_MYSQL_PORT", "3306")))
    mysql_user: str = field(default_factory=lambda: os.getenv("AGENT_MYSQL_USER", "root"))
    mysql_password: str = field(default_factory=lambda: os.getenv("AGENT_MYSQL_PASSWORD", ""), repr=False)
    mysql_database: str = field(default_factory=lambda: os.getenv("AGENT_MYSQL_DATABASE", "ops_agent"))
    checkpoint_path: Path = field(default_factory=lambda: Path(os.getenv("AGENT_CHECKPOINT_PATH", str(BASE / "data/checkpoints.sqlite"))))
    keep_turns: int = field(default_factory=lambda: int(os.getenv("AGENT_KEEP_TURNS", "20")))


class DiagnosisStreamResponse(StreamingResponse):
    def __init__(self, session):
        self.session = session
        super().__init__(session.stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-ID": session.run_id,
        })

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # 包括响应生成器尚未开始、发送失败及 ASGI disconnect 的情况。
            with anyio.CancelScope(shield=True):
                await self.session.cancel()


def create_app(manager=None, settings=None):
    @asynccontextmanager
    async def lifespan(app):
        if manager is not None:
            app.state.manager = manager
            try:
                yield
            finally:
                await manager.close()
            return
        config = settings or Settings()
        llm = create_llm()
        async with AsyncExitStack() as stack:
            store = await MySQLConversationStore.connect(config)
            stack.push_async_callback(store.close)
            await store.setup()
            await store.recover()
            config.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(config.checkpoint_path)))
            await saver.setup()
            graph = build_app(llm, keep_turns=config.keep_turns, checkpointer=saver)
            app.state.manager = DiagnosisManager(graph, store)
            stack.push_async_callback(app.state.manager.close)
            yield

    app = FastAPI(title="Ops Diagnosis Agent", lifespan=lifespan)

    @app.exception_handler(MissingConversation)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": "会话或执行记录不存在"})

    @app.exception_handler(RunConflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc), "run_id": exc.run_id})

    @app.exception_handler(MySQLError)
    async def database_error(request, exc):
        logger.error("MySQL 请求失败", exc_info=exc)
        return JSONResponse(status_code=503, content={"detail": "历史存储暂不可用，请检查 MySQL"})

    @app.get("/health")
    async def health():
        return {"status": "ok", "ready": hasattr(app.state, "manager")}

    @app.post("/api/v1/conversations", response_model=Conversation, status_code=201)
    async def create_conversation(body: CreateConversation):
        return await app.state.manager.create_conversation(body.title)

    @app.get("/api/v1/conversations", response_model=ConversationList)
    async def list_conversations(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        rows = await app.state.manager.store.list(limit + 1, offset)
        more = len(rows) > limit
        return {"items": rows[:limit], "has_more": more, "next_offset": offset + limit if more else None}

    @app.get("/api/v1/conversations/{conversation_id}/messages", response_model=HistoryResponse)
    async def history(conversation_id: UUID, limit: int = Query(20, ge=1, le=100),
                      before: int | None = Query(None, ge=1, description="查询此 seq 之前的历史；不传则加载最近的轮次")):
        rows = await app.state.manager.store.history(str(conversation_id), limit + 1, before)
        more = len(rows) > limit
        items = rows[:limit]
        # 数据库先取最近记录，页内按从旧到新返回，供聊天界面直接展示。
        return {"conversation_id": conversation_id, "items": list(reversed(items)), "has_more": more,
                "next_cursor": items[-1]["seq"] if more else None}

    @app.post("/api/v1/conversations/{conversation_id}/messages", response_class=StreamingResponse,
              responses={200: {"description": "SSE：meta/thinking/tool_start/tool_end/content/error/done",
                               "content": {"text/event-stream": {}}},
                         409: {"description": "会话忙或 request_id 已使用"}})
    async def ask(conversation_id: UUID, body: AskRequest):
        session = await app.state.manager.start(str(conversation_id), body.question,
                                                str(body.request_id) if body.request_id else None)
        return DiagnosisStreamResponse(session)

    @app.get("/api/v1/conversations/{conversation_id}/runs/{run_id}", response_model=RunView)
    async def get_run(conversation_id: UUID, run_id: UUID):
        return await app.state.manager.store.get_run(str(conversation_id), str(run_id))

    @app.post("/api/v1/conversations/{conversation_id}/runs/{run_id}/cancel", response_model=RunView)
    async def cancel(conversation_id: UUID, run_id: UUID):
        service = app.state.manager
        record = await service.store.get_run(str(conversation_id), str(run_id))
        session = service.active.get(str(conversation_id))
        if session and session.run_id == str(run_id):
            await session.cancel()
        elif record["status"] == "running":
            raise HTTPException(409, "运行状态与本进程不一致，请确认只启动了一个服务实例")
        return await service.store.get_run(str(conversation_id), str(run_id))

    return app


app = create_app()
