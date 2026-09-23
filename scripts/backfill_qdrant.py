"""Backfill (or verify) the Qdrant dense-index copy from Postgres chunk rows.

Postgres is the system of record: this script reads every ready-version
chunk with its stored embedding, deletes the old Qdrant copy per document
version, and upserts the fresh points. Safe to re-run; use --check to only
compare counts without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database.models import Document, DocumentChunk, DocumentStatus, DocumentVersion  # noqa: E402
from app.database.session import SessionFactory  # noqa: E402
from app.vectorstore import QdrantVectorStore, point_payload  # noqa: E402
from app.vectorstore.base import VectorPoint  # noqa: E402


async def _counts(store: QdrantVectorStore, version_ids: list[uuid.UUID]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for version_id in version_ids:
        response = await store.client.post(
            f"{store.base_url}/collections/{store.collection}/points/count",
            headers=store._headers(),
            json={"filter": {"must": [{"key": "document_version_id", "match": {"value": str(version_id)}}]}},
            timeout=store.settings.qdrant_timeout_seconds,
        )
        response.raise_for_status()
        counts[str(version_id)] = int((response.json().get("result") or {}).get("count", 0))
    return counts


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Qdrant points from Postgres chunks.")
    parser.add_argument("--check", action="store_true", help="Compare counts without writing.")
    parser.add_argument("--tenant", default=None, help="Only backfill one tenant id.")
    parser.add_argument("--limit", type=int, default=0, help="Only backfill N versions (0 = all).")
    args = parser.parse_args()

    settings = get_settings()
    if settings.vector_store_backend != "qdrant" or not settings.qdrant_url:
        print("Set VECTOR_STORE_BACKEND=qdrant and QDRANT_URL first.")
        return 2
    store = QdrantVectorStore(settings)
    await store.ensure_collection()

    async with SessionFactory() as db:
        stmt = (
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.status == DocumentStatus.ready, Document.deleted_at.is_(None))
        )
        if args.tenant:
            stmt = stmt.where(DocumentVersion.tenant_id == args.tenant)
        versions = list((await db.scalars(stmt)).all())
        if args.limit:
            versions = versions[: args.limit]
        before = await _counts(store, [v.id for v in versions])
        pg_counts: dict[str, int] = {}
        for version in versions:
            rows = list(
                (
                    await db.scalars(
                        select(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
                    )
                ).all()
            )
            pg_counts[str(version.id)] = len(rows)
            status = "ok" if before.get(str(version.id), -1) == len(rows) else "drift"
            print(f"{version.id} pg={len(rows)} qdrant={before.get(str(version.id), 0)} {status}")
            if args.check or not rows:
                continue
            await store.delete_by_version(document_version_id=str(version.id))
            await store.upsert(
                [
                    VectorPoint(
                        point_id=str(chunk.id),
                        vector=list(chunk.embedding),
                        payload=point_payload(
                            tenant_id=chunk.tenant_id,
                            access_scope=chunk.access_scope,
                            document_id=str(chunk.document_id),
                            document_version_id=str(chunk.document_version_id),
                            chunk_index=chunk.chunk_index,
                            language=chunk.language,
                            category=chunk.category,
                            section=chunk.section,
                            page_number=chunk.page_number,
                            embedding_model=settings.embedding_model,
                        ),
                    )
                    for chunk in rows
                ]
            )
    if args.check:
        drifted = sum(1 for vid, n in pg_counts.items() if before.get(vid, -1) != n)
        print(f"versions={len(pg_counts)} drifted={drifted}")
    else:
        print(f"backfilled {len(pg_counts)} versions")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
