# Vector database & chunking pipeline

This repo ships a hybrid retrieval stack: Postgres + pgvector is the system of
record, and Qdrant is an optional dedicated dense index alongside it. The
chunking pipeline (`StructureAwareChunker`) feeds both.

## Chunking pipeline

`backend/app/ingestion/` — `extract_document()` → `StructureAwareChunker`
(target 1600 / max 2400 / overlap 200 chars, env `CHUNK_*`) → embed (batch 32)
→ write rows → dual-write Qdrant points.

- `chunking.py` — `StructureAwareChunker(chunk_target_chars, chunk_max_chars,
  chunk_overlap_chars)`: keeps headings as section boundaries, splits
  oversized elements on sentence boundaries (tables split per line, hard
  wrap beyond max), carries overlap (whole rows for tables, word-aligned
  tail otherwise), emits `Chunk` with SHA-256 `content_hash`, `chunk_index`,
  `section`, `page_number`, `parent_key`. Filters chunks shorter than 20
  chars. Pure CPU, milliseconds per document (measured ~0.07 s for the whole
  Mansam corpus).
- `extractors.py` — per-type extractors to `ExtractedElement(kind ∈ heading,
  paragraph, table, list, code)` for PDF / DOCX / XLSX / TXT / MD / HTML /
  CSV / JSON, incl. sheet exclusions, unpublished-row filtering, SPA-shell
  detection, folio/furniture cleanup.
- `normalize.py`, `sites.py`, `ocr.py` — text normalization, site/sitemap
  helpers, cloud OCR for scanned PDFs.

## Vector database

Postgres (`pgvector/pgvector:pg16`, HNSW cosine index
`ix_chunks_embedding_hnsw` + GIN `search_vector`) always holds the chunk text,
embeddings, and filters (`tenant_id`, `access_scope`, `language`, `category`,
`tags`, version joins). Qdrant (compose service `qdrant`, v1.19.1, ports
6333/6334) holds a **dense-index copy**: point id = Postgres chunk UUID,
payload = filter/display attributes only, never text.

### Qdrant vs Milvus (decision)

Qdrant was chosen over Milvus for this codebase:

| Concern | Qdrant | Milvus |
|---|---|---|
| Ops footprint | Single binary, no etcd/MinIO, ~100 MB image | etcd + MinIO + standalone; heavy on a 6 GB GPU box |
| Client weight | None — REST via existing `httpx` | `pymilvus` + gRPC, new native dep |
| Filtered search | Native payload filters + keyword indexes | Partition/expr filters, more setup |
| Auth | Optional single API key | RBAC, more config |
| Crash recovery | `?wait=true` writes + reindex repair | Same, but more moving parts |

Milvus still wins past ~10–100 M vectors (distributed shards, GPU index).
The `VectorStore` protocol (`backend/app/vectorstore/base.py`) keeps that
door open: only the `limit/top-k ↔ RRF` adapter changes, Postgres stays the
source of truth either way.

### Wiring

- `backend/app/vectorstore/` — `VectorStore` protocol (`ensure_collection`,
  `upsert`, `delete_by_version`, `search`), `QdrantVectorStore` (httpx REST:
  `GET/PUT /collections/{name}`, `PUT …/points?wait=true`, `POST
  …/points/query`, `POST …/points/delete?wait=true`), helpers
  `build_point_id` / `match_value` / `match_any` / `point_payload`.
- Settings (`VECTOR_STORE_BACKEND`, `QDRANT_URL`, `QDRANT_API_KEY`,
  `QDRANT_COLLECTION`, `QDRANT_{TIMEOUT_SECONDS,HNSW_M,HNSW_EF_CONSTRUCT,
  FULL_SCAN_THRESHOLD,SEARCH_EF}`) — see `.env.example`.
- Ingestion (`backend/app/workers/ingestion.py::_mirror_to_vector_store`) —
  after Postgres flush: `ensure_collection → delete_by_version →
  upsert(points)`. Delete-first makes crashed-worker reruns idempotent.
  Mirroring is best-effort: any failure is logged and ingestion still
  completes on Postgres/pgvector.
- Retrieval (`backend/app/retrieval/hybrid.py`) — Qdrant searched first with
  tenant + scope pre-filters, point ids hydrated from Postgres under the same
  visibility/version joins (final ACL = Postgres, never Qdrant). Qdrant
  outage → automatic pgvector fallback (`vector_store_unavailable_pgvector_
  fallback`); diagnostics carry `vector_backend: qdrant|pgvector`.
- API — `chat.py::get_vector_store` injects the store into `HybridRetriever`
  for `/chat` + `/chat/stream`; `/health/ready` reports `vector_store`
  without flipping overall readiness (core vote = Postgres + Redis).
- Scripts — `scripts/backfill_qdrant.py` (`--check` dry-runs count
  comparison; otherwise delete + upsert per version; `--tenant`, `--limit`).

### Operate

```bash
# start sidecar
docker compose up -d qdrant
# enable backend (env)
VECTOR_STORE_BACKEND=qdrant
QDRANT_URL=http://qdrant:6333   # http://127.0.0.1:6333 locally
# index existing data
python scripts/backfill_qdrant.py --check
python scripts/backfill_qdrant.py
```

Changing `EMBEDDING_DIMENSION`/model requires a new collection (dimension
guard raises `VectorStoreDimensionError`): create a fresh collection name,
re-embed, cut over. Never delete Postgres rows to "reset" Qdrant — delete by
`document_version_id` filter or drop the collection and backfill.
