import json
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Memory
from app.embeddings import EmbeddingProvider
from app.llm import LLMMessage, LLMProvider
from app.rag.prompts import PromptRepository

SENSITIVE_TERMS = {
    "password",
    "passcode",
    "api key",
    "secret key",
    "credit card",
    "cvv",
    "كلمة المرور",
    "بطاقة ائتمان",
}


class MemoryService:
    def __init__(
        self,
        redis: Redis,
        embeddings: EmbeddingProvider,
        llm: LLMProvider,
        prompts: PromptRepository,
        settings: Settings,
    ) -> None:
        self.redis = redis
        self.embeddings = embeddings
        self.llm = llm
        self.prompts = prompts
        self.settings = settings

    async def append_short_term(self, conversation_id: uuid.UUID, role: str, content: str) -> None:
        key = f"conversation:{conversation_id}:recent"
        item = json.dumps({"role": role, "content": content}, ensure_ascii=False)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, item)
            pipe.ltrim(key, -20, -1)
            pipe.expire(key, self.settings.memory_ttl_seconds)
            await pipe.execute()

    async def get_short_term(self, conversation_id: uuid.UUID) -> list[dict[str, str]]:
        pending = cast(
            Awaitable[list[Any]],
            self.redis.lrange(f"conversation:{conversation_id}:recent", 0, -1),
        )
        rows = await pending
        result = []
        for row in rows:
            try:
                payload = json.loads(row)
                result.append({"role": str(payload["role"]), "content": str(payload["content"])})
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return result

    async def extract_and_store(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        tenant_id: str,
        source_message_id: uuid.UUID,
        text: str,
    ) -> list[Memory]:
        if not self.settings.long_term_memory_enabled:
            return []
        lower = text.lower()
        if any(term in lower for term in SENSITIVE_TERMS):
            return []
        result = await self.llm.generate(
            [
                LLMMessage("system", self.prompts.load("memory/extract.md")),
                LLMMessage("user", text),
            ],
            temperature=0,
            max_tokens=500,
            response_format="json",
            model=self.settings.llm_fast_model,
        )
        payload = _parse_json(result.text)
        stored = []
        for candidate in payload.get("memories", [])[:5]:
            if not isinstance(candidate, dict):
                continue
            confidence = float(candidate.get("confidence", 0))
            explicit = bool(candidate.get("explicitly_requested", False))
            if confidence < self.settings.long_term_memory_min_confidence and not explicit:
                continue
            content = " ".join(str(candidate.get("content", "")).split())[:2000]
            if not content or any(term in content.lower() for term in SENSITIVE_TERMS):
                continue
            embedding = await self.embeddings.embed_query(content)
            expires_in = candidate.get("expires_in_days")
            expires_at = (
                datetime.now(UTC) + timedelta(days=max(1, min(3650, int(expires_in))))
                if expires_in is not None
                else None
            )
            memory = Memory(
                user_id=user_id,
                tenant_id=tenant_id,
                memory_type=str(candidate.get("type", "context"))[:32],
                content=content,
                confidence=confidence,
                source_message_id=source_message_id,
                provenance={"source": "conversation", "explicitly_requested": explicit},
                embedding=embedding,
                expires_at=expires_at,
            )
            db.add(memory)
            stored.append(memory)
        await db.flush()
        return stored

    async def retrieve_long_term(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> list[Memory]:
        embedding = await self.embeddings.embed_query(query)
        now = datetime.now(UTC)
        stmt = (
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.tenant_id == tenant_id,
                Memory.deleted_at.is_(None),
                or_(Memory.expires_at.is_(None), Memory.expires_at > now),
            )
            .order_by(Memory.embedding.cosine_distance(embedding))
            .limit(limit)
        )
        return list((await db.scalars(stmt)).all())

    async def delete(self, db: AsyncSession, *, memory_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        memory = await db.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
        if not memory or memory.deleted_at is not None:
            return False
        memory.deleted_at = datetime.now(UTC)
        await db.flush()
        return True


def _parse_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {"memories": []}
    try:
        payload = json.loads(text[start : end + 1])
        return payload if isinstance(payload, dict) else {"memories": []}
    except json.JSONDecodeError:
        return {"memories": []}
