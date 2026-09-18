from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_current_principal
from app.chat.schemas import FeedbackRequest
from app.database import get_db
from app.database.models import Conversation, Feedback, Message

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    payload: FeedbackRequest,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    message = await db.scalar(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.id == payload.message_id, Conversation.user_id == principal.user_id)
    )
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    db.add(
        Feedback(
            user_id=principal.user_id,
            message_id=message.id,
            category=payload.category,
            helpful=payload.category == "helpful" if payload.category in {"helpful", "not_helpful"} else None,
            comment=payload.comment,
        )
    )
    await db.commit()
    return {"status": "recorded"}
