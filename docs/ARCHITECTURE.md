# Architecture

`Browser → FastAPI → query planner → hybrid retrieval → optional reranker → grounded generator → verifier → text/TTS`.

PostgreSQL holds users, conversations, documents, versions, chunks, retrieval events, verification events, feedback and audit records. `pgvector` holds document and long-term-memory embeddings. Qdrant is an optional dedicated dense index alongside pgvector — see `docs/VECTOR-DB.md`. Redis holds rate-limit counters and short-term conversation state. Celery performs asynchronous extraction, chunking, hosted embedding and indexing.

The Hugging Face Bucket URL in the project context is storage, not assumed to be an inference endpoint. `HF_INFERENCE_URL` selects a compatible hosted HF deployment; the provider interfaces make replacement possible without changing RAG business logic.

Knowledge and memory remain separate: document chunks are tenant/access-scoped evidence; user memories are stored only after an explicit confidence policy and have user-scoped retrieval/deletion.

## Ingestion sources

Documents reach the pipeline through three routes, all of which converge on the same extract → chunk → embed → index worker:

- `POST /api/v1/documents` — file upload (`PDF`, `DOCX`, `XLSX`, `TXT`, `MD`, `HTML`, `CSV`, `JSON`).
- `POST /api/v1/documents/url` — one remote source by URL (HTML page, plain text, or JSON API response).
- `POST /api/v1/documents/site` — many sources from one allow-listed host in a single call, with optional
  `paths` and a `sitemap.xml` fallback; each source is reported individually as `queued`, `duplicate`, `skipped`, or `failed`.

Remote ingestion is gated by `URL_INGESTION_ENABLED`, `URL_ALLOWED_HOSTS` (host allow-list), `URL_INGESTION_MAX_PAGES`
and `URL_INGESTION_USER_AGENT`, and every URL is re-validated against private/link-local/reserved address ranges before fetching.

JSON responses are flattened into `path: value` lines (nested objects use dotted keys, arrays use `[n]` indexes,
`null` and boolean fields are dropped) so catalogue APIs index as clean, retrievable text.

List-valued settings (`FRONTEND_ORIGINS`, `ALLOWED_UPLOAD_TYPES`, `URL_ALLOWED_HOSTS`) accept either a JSON array or a
comma-separated value in `.env`, so `URL_ALLOWED_HOSTS=a.example.com,b.example.com` works as well as
`URL_ALLOWED_HOSTS=["a.example.com","b.example.com"]`.

## Worker runtime notes

Ingestion runs in Celery, and two invariants keep the queue healthy:

- **The task module must be loaded.** The task lives in `app/workers/ingestion.py`, while
  `autodiscover_tasks(["app.workers"])` only looks for `app/workers/tasks.py`. `celery_app.py` therefore declares
  `include=["app.workers.ingestion"]`; without it every job is accepted and then discarded with
  `KeyError: 'ingestion.index_document'`. A worker that started correctly prints `ingestion.index_document` in its
  `[tasks]` banner, and `backend/tests/unit/test_site_ingestion.py` probes a fresh interpreter to catch regressions.
- **One event loop per worker process.** Tasks run through `_run_async` in `app/workers/ingestion.py` rather than
  `asyncio.run`. The SQLAlchemy engine pools asyncpg connections bound to the loop that created them, so a new loop per
  task reuses dead connections and the second job fails with
  `AttributeError("'NoneType' object has no attribute 'send'")` or `RuntimeError: Event loop is closed`.

## Mansam website note

`https://uatuae.mansamworld.com` is an Angular single-page application: the served HTML is an empty `<app-root>`
shell with no server-rendered text, so ingesting `https://uatuae.mansamworld.com/home` returns an actionable
`ExtractionError` instead of an empty document. The site's real content is served by its public JSON API, which
should be ingested instead:

```text
https://uatuae.mansamworld.com/api/public/productlines   # 13 product lines with EN/AR descriptions
https://uatuae.mansamworld.com/api/public/collections    # collections per product line
https://uatuae.mansamworld.com/api/public/emotions       # scent emotion groupings
```

Example:

```bash
curl -X POST http://localhost:8000/api/v1/documents/site \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"url":"https://uatuae.mansamworld.com/home","paths":["api/public/productlines","api/public/collections","api/public/emotions"],"category":"catalogue"}'
```

`python scripts/verify_live_ingestion.py` re-runs extraction and chunking against these live endpoints as a smoke check.

