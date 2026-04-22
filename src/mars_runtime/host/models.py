"""Pydantic models for the standalone HTTP host."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateConversationRequest(BaseModel):
    message: str


class SendMessageRequest(BaseModel):
    message: str


class TurnResponse(BaseModel):
    conversation_id: str
    status: str


class ConversationSummary(BaseModel):
    id: str
    org_id: str
    created_by: str
    status: str
    title: str | None = None
    last_message_at: str | None = None
    created_at: str | None = None


class PaginationResponse(BaseModel):
    page: int
    limit: int
    count: int


class ConversationListResponse(BaseModel):
    data: list[ConversationSummary]
    pagination: PaginationResponse


class ConversationDetailResponse(ConversationSummary):
    context: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    system_prompt: str | None = None
