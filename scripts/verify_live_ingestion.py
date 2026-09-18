"""Live smoke check: run the real ingestion code path against the Mansam public content API.

It uses the same settings, URL validation, fetch gating, extraction and chunking as
``POST /api/v1/documents/site`` — only storage/queueing (Postgres/Celery) is skipped.

Usage: python scripts/verify_live_ingestion.py
"""

import asyncio
import sys

sys.path.insert(0, "backend")

from app.api.routes.documents import _fetch_remote  # noqa: E402
from app.config import Settings  # noqa: E402
from app.ingestion import StructureAwareChunker, extract_document, media_type_for, source_document_name  # noqa: E402
from app.ingestion.sites import normalize_site_paths  # noqa: E402
from app.security import validate_ingestion_url  # noqa: E402

BASE_URL = "https://uatuae.mansamworld.com/home"
PATHS = [
    "api/public/productlines",
    "api/public/collections",
    "api/public/emotions",
]
EXPECTED_SPA_REJECTION = "single-page application"


async def main() -> int:
    settings = Settings()
    chunker = StructureAwareChunker(
        target_chars=settings.chunk_target_chars,
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
    )
    failures = 0
    targets = normalize_site_paths(BASE_URL, PATHS)
    for target in targets:
        try:
            url = validate_ingestion_url(target, settings.url_allowed_hosts)
        except Exception as exc:  # noqa: BLE001
            print(f"{target}: URL validation failed -> {exc}")
            failures += 1
            continue
        try:
            data, content_type = await _fetch_remote(url, settings)
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", str(exc))
            if EXPECTED_SPA_REJECTION in str(detail):
                print(f"{target}: correctly rejected -> {detail}")
                continue
            print(f"{target}: fetch failed -> {detail}")
            failures += 1
            continue
        media_type = media_type_for(content_type)
        name = source_document_name(url, content_type)
        extracted = extract_document(data, name, media_type)
        chunks = chunker.chunk(extracted)
        chars = sum(len(element.text) for element in extracted.elements)
        print(
            f"{target}: {content_type} -> {name} elements={len(extracted.elements)} "
            f"chars={chars} chunks={len(chunks)}"
        )
        if not chunks:
            failures += 1
        if target.endswith("productlines"):
            preview = chunks[0].content[:240].encode("unicode_escape").decode("ascii")
            print(f"  first chunk preview: {preview}")
    print(f"\nsources={len(targets)} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

