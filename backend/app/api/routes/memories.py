import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_current_principal
from app.database import get_db
from app.database.models import Memory

router = APIRouter(prefix="/memories", tags=["memories"])


class MemoryResponse(BaseModel):
    id: uuid.UUID
    memory_type: str
    content: str
    confidence: float
    provenance: dict
    created_at: datetime
    expires_at: datetime | None


@router.get("", response_model=list[MemoryResponse])
async def list_memories(
    principal: Principal = Depends(get_current_principal), db: AsyncSession = Depends(get_db)
) -> list[MemoryResponse]:
    rows = await db.scalars(
        select(Memory)
        .where(
            Memory.user_id == principal.user_id,
            Memory.deleted_at.is_(None),
            or_(Memory.expires_at.is_(None), Memory.expires_at > datetime.now().astimezone()),
        )
        .order_by(Memory.updated_at.desc())
    )
    return [MemoryResponse.model_validate(item, from_attributes=True) for item in rows]


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    memory = await db.scalar(
        select(Memory).where(
            Memory.id == memory_id,
            Memory.user_id == principal.user_id,
            Memory.deleted_at.is_(None),
        )
    )
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    memory.deleted_at = datetime.now().astimezone()
    await db.commit()
