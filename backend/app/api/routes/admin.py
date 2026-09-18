from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_roles
from app.database import get_db
from app.database.models import (
    AnswerVerification,
    Conversation,
    Document,
    Feedback,
    IngestionJob,
    JobStatus,
    Message,
    RetrievalEvent,
    Role,
    User,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/metrics")
async def metrics(
    principal: Principal = Depends(require_roles(Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    async def count(model, *conditions) -> int:
        return int(await db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)

    return {
        "users": await count(User, User.tenant_id == principal.tenant_id, User.deleted_at.is_(None)),
        "documents": await count(Document, Document.tenant_id == principal.tenant_id, Document.deleted_at.is_(None)),
        "failed_ingestion_jobs": await count(
            IngestionJob,
            IngestionJob.status == JobStatus.failed,
            IngestionJob.document_id == Document.id,
            Document.tenant_id == principal.tenant_id,
        ),
        "feedback_items": await count(
            Feedback,
            Feedback.message_id == Message.id,
            Message.conversation_id == Conversation.id,
            Conversation.tenant_id == principal.tenant_id,
        ),
        "verification_failures": await count(
            AnswerVerification,
            AnswerVerification.grounded.is_(False),
            AnswerVerification.message_id == Message.id,
            Message.conversation_id == Conversation.id,
            Conversation.tenant_id == principal.tenant_id,
        ),
    }


@router.get("/retrieval")
async def retrieval_statistics(
    principal: Principal = Depends(require_roles(Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(RetrievalEvent)
            .join(User, User.id == RetrievalEvent.user_id)
            .where(User.tenant_id == principal.tenant_id)
        )
        or 0
    )
    average = await db.scalar(
        select(func.avg(RetrievalEvent.latency_ms))
        .select_from(RetrievalEvent)
        .join(User, User.id == RetrievalEvent.user_id)
        .where(User.tenant_id == principal.tenant_id)
    )
    return {"requests": count, "average_latency_ms": float(average or 0)}


@router.get("/ingestion")
async def ingestion_statistics(
    principal: Principal = Depends(require_roles(Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = await db.execute(
        select(IngestionJob, Document.name)
        .join(Document, Document.id == IngestionJob.document_id)
        .where(Document.tenant_id == principal.tenant_id)
        .order_by(IngestionJob.created_at.desc())
        .limit(100)
    )
    return [
        {
            "job_id": str(job.id),
            "document_id": str(job.document_id),
            "document_name": name,
            "status": job.status.value,
            "stage": job.stage,
            "progress": job.progress,
            "error_code": job.error_code,
            "error_message": job.error_message,
        }
        for job, name in rows
    ]
