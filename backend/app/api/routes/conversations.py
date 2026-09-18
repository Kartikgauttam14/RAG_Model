import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_current_principal
from app.chat.schemas import ConversationDetail, ConversationSummary, MessageResponse
from app.database import get_db
from app.database.models import Conversation, Message

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    principal: Principal = Depends(get_current_principal), db: AsyncSession = Depends(get_db)
) -> list[ConversationSummary]:
    rows = await db.scalars(
        select(Conversation)
        .where(
            Conversation.user_id == principal.user_id,
            Conversation.deleted_at.is_(None),
        )
        .order_by(Conversation.updated_at.desc())
        .limit(100)
    )
    return [ConversationSummary.model_validate(row, from_attributes=True) for row in rows]


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    conversation = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == principal.user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await db.scalars(
        select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at)
    )
    return ConversationDetail(
        **ConversationSummary.model_validate(conversation, from_attributes=True).model_dump(),
        messages=[MessageResponse.model_validate(item, from_attributes=True) for item in messages],
    )


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    conversation = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == principal.user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.deleted_at = datetime.now(UTC)
    await db.commit()
