"""后端 HTTP 和展示记录契约；时间戳统一使用 Unix 毫秒。"""
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


RunStatus = Literal["running", "completed", "failed", "cancelled", "interrupted"]


class CreateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="新对话", min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=10000)
    request_id: UUID | None = Field(default=None, description="可选幂等 ID；重试同一次提交时保持不变")

    @field_validator("question")
    @classmethod
    def clean_question(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


class Conversation(BaseModel):
    conversation_id: UUID
    title: str
    created_at: int
    updated_at: int
    active_run_id: UUID | None = None


class ConversationList(BaseModel):
    items: list[Conversation]
    has_more: bool
    next_offset: int | None


class StreamEvent(BaseModel):
    type: Literal["meta", "thinking", "tool_start", "tool_end", "content", "error", "done"]
    conversation_id: str
    run_id: str
    seq: int
    timestamp: int
    data: dict[str, Any]


class RunView(BaseModel):
    seq: int
    run_id: UUID
    conversation_id: UUID
    question: str
    status: RunStatus
    created_at: int
    updated_at: int
    finished_at: int | None
    error: str | None
    events: list[StreamEvent]


class HistoryResponse(BaseModel):
    conversation_id: UUID
    items: list[RunView] = Field(description="本页对话轮次，按 seq 从旧到新排列")
    has_more: bool = Field(description="是否还有更早的历史")
    next_cursor: int | None = Field(description="加载更早历史时传入 before；无更多记录时为 null")
