import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class CitationResponse(BaseModel):
    document_id: uuid.UUID
    document_name: str
    document_version: int
    chunk_id: uuid.UUID
    page: int | None = None
    section: str | None = None
    excerpt: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    conversation_id: uuid.UUID | None = None
    language: str | None = Field(default=None, max_length=16)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    request_id: str
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    answer: str
    confidence: float = Field(ge=0, le=1)
    citations: list[CitationResponse]
    grounded: bool
    verification_status: str
    conflicts: list[str] = Field(default_factory=list)
    retrieval_fallback: str | None = None


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str | None
    language: str | None
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list[dict[str, Any]]
    verification_status: str | None
    created_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[MessageResponse]


class FeedbackRequest(BaseModel):
    message_id: uuid.UUID
    category: Literal[
        "helpful",
        "not_helpful",
        "incorrect",
        "missing_information",
        "wrong_source",
        "speech_recognition_problem",
        "voice_output_problem",
    ]
    comment: str | None = Field(default=None, max_length=2000)
