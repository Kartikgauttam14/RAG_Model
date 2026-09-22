# Mansam Evidence-First RAG

Production-oriented, hosted-Hugging-Face RAG with hybrid retrieval, source inspection, verified citations, separate conversation memory, and browser voice controls. It does not run an LLM locally: the configured Hugging Face endpoint is the only generation runtime.

## What is implemented

- FastAPI API, PostgreSQL/pgvector schemas and Alembic migration; Redis rate limiting/session memory; Celery ingestion.
- PDF, DOCX, XLSX, TXT, Markdown, HTML, CSV and JSON extraction. XLSX is row-level, with sheet and row provenance, so the Mansam SSOT can be indexed safely.
- Permission-filtered vector + full-text retrieval, reciprocal-rank fusion, optional hosted reranking, and document-version filtering.
- Grounded-answer JSON contract, citation-to-chunk validation, verification/regeneration, and an explicit uncertainty response when evidence is insufficient.
- JWT/Argon2 authentication, RBAC, upload validation, malware-scanner hook, URL SSRF validator, prompt-injection treatment of retrieved text as data, audit logs, metrics, and structured request IDs.
- React chat/admin interface with expandable sources, recording/transcript controls, speech playback, retry/copy/regenerate, and answer feedback.

## Run locally

1. Copy `.env.example` to `.env` and set a 32+ character `JWT_SECRET`, `HF_INFERENCE_URL`, `HF_TOKEN`, and `EMBEDDING_INFERENCE_URL`. Configure optional STT/TTS/reranking URLs only when those features are needed.
2. Start infrastructure with `docker compose up --build`.
3. In another shell, run `docker compose exec backend alembic -c alembic.ini upgrade head` and `docker compose exec backend python scripts/bootstrap_admin.py`.
4. Open `http://localhost:5173`; API docs are under the API prefix at `http://localhost:8000/api/v1/docs`
   (`/api/v1/redoc` for ReDoc, `/api/v1/openapi.json` for the schema).

For local development with the host Python environment, apply migrations with `Push-Location backend; ..\\.venv\\Scripts\\alembic.exe -c alembic.local.ini upgrade head; Pop-Location`.

For development without containers, create a virtual environment, run `pip install -e "./backend[dev]"`, run `npm install` in `frontend`, and supply PostgreSQL/Redis yourself. Start the API with `uvicorn app.main:app --app-dir backend --reload` and the UI with `npm run dev` in `frontend`.

Authentication is temporarily disabled for local development with `AUTHENTICATION_ENABLED=false` and `VITE_AUTH_ENABLED` unset. Set both values to `true` before enabling authentication, staging, or production.

### Voice input and playback

Speech runs on the local `speech` compose service (speaches: faster-whisper for
transcription, Kokoro for playback), so the microphone and the playback controls work
without a hosted speech token:

```dotenv
STT_PROVIDER=openai
STT_MODEL=Systran/faster-whisper-small
STT_INFERENCE_URL=http://127.0.0.1:8083/v1/audio/transcriptions
STT_PROMPT=Mansam fragrance catalogue: Mamlakati, Qanun, Sarhan, ...
TTS_PROVIDER=openai
TTS_MODEL=speaches-ai/Kokoro-82M-v1.0-ONNX
TTS_INFERENCE_URL=http://127.0.0.1:8083/v1/audio/speech
TTS_VOICE=af_heart
```

`STT_PROMPT` is not cosmetic: Whisper biases decoding toward the words in it, and without
the catalogue vocabulary "Mamlakati" was transcribed as "MAM LAKETY". Models are downloaded
into the container on first use with `POST /v1/models/<id>` on port 8083 (`/` encoded as
`%2F`); Kokoro voices are English, so an Arabic answer needs a piper Arabic voice
(`ar_JO-kareem-medium`) selected through `TTS_MODEL`/`TTS_VOICE`. A missing speech server or
model is reported as `503` and the UI keeps text input available.

### Troubleshooting: `503 Language model is unavailable`

Answer generation is the only part of the stack that requires a reachable generation endpoint; retrieval, ingestion and embeddings do not. `/api/v1/chat` returns `503` when every attempt to the configured endpoint fails (timeout, HTTP error, or an empty completion), and the API log records `llm_unavailable_during_planning` or a provider status such as `402 Payment Required`.

Because an exhausted free allowance can still admit the occasional request, this failure is typically **intermittent** — the same question may return `200` on one attempt and `503` on the next, so retry a few times before treating it as a hard outage.

`HuggingFaceLLMProvider` speaks the OpenAI chat-completions API, so any OpenAI-compatible server works without code changes — set `HF_INFERENCE_URL` to the server root, keep `HF_API_MODE=openai`, and set `HF_MODEL` to the model name that server expects:

```dotenv
# local OpenAI-compatible server (Ollama, LM Studio, vLLM) instead of the HF router
HF_INFERENCE_URL=http://127.0.0.1:11434
HF_API_MODE=openai
HF_MODEL=llama3.1:8b
HF_TOKEN=
```

### Troubleshooting: retrieval misses a fact that is definitely in the corpus

Hybrid retrieval fuses a vector arm and a lexical (PostgreSQL full-text) arm. Three defects in that path were fixed and are covered by unit tests:

- **Lexical arm used to return zero rows for every query.** The query was built with `websearch_to_tsquery(<config>, "<raw user text>")`, which ANDs *every* token — including stopwords — so `how many mansam boutiques are there` required all eight words in one chunk and matched nothing. Lexical hits are now built by OR-joining the tokenised query terms (`app/retrieval/hybrid.py`), and the search config is a setting (`LEXICAL_TEXT_SEARCH_CONFIG`, default `english`) applied to **both** the index side (`app/workers/ingestion.py`) and the query side. The two sides must match: a mismatch silently zeroes the arm. Changing this setting requires reindexing, because `search_vector` is stored per chunk.
- **PDF page numbers were being read as counts.** A printed folio like `87` sitting on its own line directly above the running head `boutiques and arts of mansam` was extracted as part of the text, so the model reported "87 boutiques". `remove_page_folios` (`app/ingestion/normalize.py`) now drops bare page-number lines, including multi-number spread markers such as `92 93`. This is applied during PDF extraction, so affected documents need a reindex.
- **The HuggingFace reranker sent the wrong payload.** It posted `{"query": ..., "texts": [...]}`, but the `hf-inference` text-classification route requires `{"inputs": [{"text": ..., "text_pair": ...}]}`; the mismatch meant the precision stage always failed and retrieval silently fell back to RRF alone. The payload is fixed in `app/reranking/huggingface.py`.

To rebuild the index after changing chunking, extraction or the search config, reindex each document (`POST /api/v1/documents/{id}/reindex`), which re-extracts the stored file, re-chunks, re-embeds and rewrites `search_vector`.

Verify the arms are alive by inspecting `scores` on a recent `retrieval_events` row: a healthy event has a non-null `lexical` value and, when reranking is enabled, a `reranker` score.

A fourth, non-defect cause is worth knowing: the planner may attach metadata filters (for example `language=ar`) that restrict retrieval to a subset of chunks. If the filtered evidence cannot be grounded, or comes back empty, the service retries once with the filters removed and prefers the unfiltered answer when it verifies better (`app/chat/service.py`, logged as `retrieval_retry_without_filters` / `retrieval_widened_retry`). This is why a question asked in one language can be answered from a document held in another.

### Known limitation: a field that exists only at product level

Structured sources can legitimately lack the field a question asks for. In the bundled `Mansam_SSOT_Master_v3_6.xlsx`, `Fragrance Family` is recorded **only on product rows**; the collection records carry no such field, and characterise themselves through the collection name alone (`collectionEn: Qanun (Amber & Spices)`). So *"Which scent family does the Qanun collection belong to?"* has no field-backed answer, and a product row that pairs `Collection: QANUN` with `Fragrance Family: Woody Leather` is the most explicit family signal in the corpus.

A small local model answers from that product field, which reads as a collection-level claim. This was measured rather than assumed: adding entity-scope rules to the answer prompt *and* the verifier prompt, annotating each evidence header with a `record_scope` label, appending a trailing scope reminder to the user payload, and re-ordering evidence to put the parent record first all produced the identical wrong answer with `qwen2.5:7b-instruct` at `temperature=0`. A field next to a SKU is a child record's field, and no prompt or ordering change makes a 7B model treat it as the parent's; the reliable fixes are a model that can follow the scope rule, or a deterministic provenance check in application code that refuses to promote a child field to a parent entity.

### Troubleshooting: XLSX sheets that are not knowledge

Every worksheet was historically chunked as brand knowledge, including operational tabs. The workbook's internal engineering changelog (`01_Version_Log`: *"Loader now reads all 20 sheets"*, *"Bot v2.3.8"*, *"drafts pending review"*) therefore entered the index and, because such text summarises the whole document, it outranked real content and was echoed verbatim as an answer. `EXCLUDED_XLSX_SHEETS` (`app/config/settings.py`, default `00_README,01_Version_Log,08_BANTQ_Rubric,09_Conversation_Log_Schema`) now skips those sheets during extraction; changing it requires reindexing the workbook.

## Tuning answer latency

Every question is a chain of sequential model calls, and on a 6 GB laptop GPU the decisive
question is what else is holding VRAM. Three models wanted it here at once — the answer model,
the embedding model and the cross-encoder reranker. When they did not fit, Ollama quietly
spilled the answer model onto the CPU and every generation became 5-10x slower; `ollama ps`
reports this as the `PROCESSOR` split, so read that column rather than the model size.

This deployment runs a "everything under five seconds" profile, measured end to end through the
browser (`.env` values in brackets):

| Stage kept in the request | Cost | Off in this profile |
| --- | --- | --- |
| retrieval (embed + vector + lexical) | ~0.2 s | — |
| draft generation, ~3k-token prompt | 2-3 s | — |
| planner generation | 2-3 s | skipped below `PLANNER_LLM_MIN_WORDS=12` (escalated once if the cheap plan does not verify) |
| verification generation | 1.5-3 s | `RAG_VERIFY_ENABLED=false` |
| memory extraction | 1.3-1.8 s | already off the request path (`LONG_TERM_MEMORY_INLINE=false`) |
| model reload after idle | 12-20 s | `KEEP_MODEL_WARM=true` pings both models every `KEEP_WARM_INTERVAL_SECONDS=240` |

Measured results: the same four questions answer in **2/3/3/5 s** through the API and **4.2 s /
4.8 s** through the UI, all grounded, with `verification_skipped` recorded instead of
`verified`. Re-enabling verification costs 1.5-3 s per answer.

Measured on this workstation, four questions in the same order (delivery, price, collection
scent family, boutique count):

| Configuration | `ollama ps` for the answer model | Latency | Answer quality |
| --- | --- | --- | --- |
| `qwen2.5:7b-instruct`, reranker + bge-m3 resident | `18%/82% CPU/GPU` | 81/114/118/100 s | verified |
| `qwen2.5:3b-instruct`, reranker + bge-m3 resident | `100% GPU` | 27/8/10/14 s | 3 of 4 unverified, invented a 300 SAR delivery threshold, mixed English into Arabic |
| `gemma3:4b`, `RERANK_INFERENCE_URL` empty | `100% GPU` | **22/15/9/10 s** | all four verified, facts match the workbook |

What each piece buys:

- **The answer model must fit next to the embedding model.** `gemma3:4b` (2.8 GB) plus
  `bge-m3` (0.7 GB) fit in 6 GB; adding the reranker's cross-encoder (~1.2 GB) does not, and
  the LLM fell to `96%/4% CPU/GPU` and back to ~50 s answers. `RERANK_INFERENCE_URL` is
  therefore empty and retrieval fuses vector and lexical ranks instead. To keep the precision
  stage, run the reranker on its CPU image
  (`ghcr.io/huggingface/text-embeddings-inference:cpu-1.8`) and set the URL.
- **One model at a time, and the same one for every stage.** Routing the draft to a 7B and the
  planner/verifier to a 3B made the first question take 150 s: two models cannot stay resident,
  so Ollama evicted and reloaded multi-GB weights on each call. `LLM_DRAFT_MODEL` and
  `LLM_FAST_MODEL` exist for a machine with enough VRAM to hold both.
- **Do not use a reasoning model with this JSON contract.** `qwen3:4b` writes to the
  `reasoning` field and leaves `content` empty, and Ollama's OpenAI-compatible endpoint ignores
  `think: false`, so the pipeline sees an empty completion.
- **Idle eviction costs a reload.** Ollama unloads a model after five minutes of inactivity, so
  the first question after a pause pays it — measured 12.3 s for the first call in the table
  above. A per-request `keep_alive` is ignored by the OpenAI-compatible endpoint, and setting
  `OLLAMA_KEEP_ALIVE` did not change the reported `UNTIL` here.
- **Counting questions read the recorded field, not the model's tally.** `Setting Key:
  boutique_count_ksa / Value: 6` answers "how many boutiques" exactly, but that row competes for
  the top-k with dozens of neighbours and was measured to be *absent* from the retrieved set —
  the same question answered 4, 6 and "five" on different runs. `HybridRetriever.count_evidence`
  now fetches such rows directly (same visibility filters) and puts them in front of the model,
  which quotes the value and cites the row (`app/rag/counts.py` holds the shared patterns).
- **`PLANNER_LLM_MIN_WORDS`** (default 6) skips the planner generation for a short first-turn
  question, which has no history to resolve and nothing to disambiguate; the service escalates
  to the model planner once if that cheap plan produces an ungrounded answer.
- **`LONG_TERM_MEMORY_INLINE=false`** extracts memories after the answer is committed instead
  of holding the reply behind one more generation and embedding call. The extraction still uses
  the GPU for a couple of seconds, so a question asked immediately afterwards can queue behind
  it.
- **`RAG_MAX_CONTEXT_CHARS` and `RERANK_TOP_K`** size the draft prompt, and the verifier
  truncates each chunk (`VERIFY_CHUNK_CHARS`) rather than receiving a second full copy of the
  evidence. Dropping `RERANK_TOP_K` to 5 was measured to lose the customer-facing delivery line
  and the rows needed to count boutiques, so 8 is the floor for this corpus.
- **The UI streams.** `sendChat` uses `/chat/stream`, so the footer reports each stage
  ("Searching the knowledge base…", "Writing the answer…") rather than one static label.
- **Per-call timing is logged.** Each generation writes an `llm_generate` line with
  `duration_ms`, `prompt_tokens` and `completion_tokens`, which is how the table above was
  measured and how a regression shows up as a slow call rather than a slow "app".

## Validation commands

### Windows path note (junction)

`K:\projects` is a junction to `K:\project` on this workstation. Node resolves the config
file, the module graph and build output to the real path, while a shell started in the
junction keeps the junction path, so the two spaces disagree. `frontend/vite.config.ts`
therefore pins `root` to the real path (the build otherwise fails with a chunk named
`../../../project/rag-from-scratch/frontend/index.html`) and allows both spaces for the dev
server, which is what fixes `npm test` (every suite previously failed with
`Cannot find module '/@fs/K:/project/.../tests/Sources.test.tsx'`). Start the dev server from
the real path — `K:\project\rag-from-scratch\frontend` — because from the junction path vite's
cold dependency optimizer crashes with `Cannot read properties of undefined (reading
'imports')`.

```powershell
& .\.venv\Scripts\ruff.exe check backend/app backend/tests scripts
Push-Location backend; & ..\.venv\Scripts\mypy.exe app; Pop-Location
Push-Location backend; & ..\.venv\Scripts\pytest.exe -q; Pop-Location
Push-Location frontend; npm run build; npm test; Pop-Location
python scripts/evaluate.py
```

See [architecture](docs/ARCHITECTURE.md), [deployment](docs/DEPLOYMENT.md), [security](docs/SECURITY.md), and [testing](docs/TESTING.md).

Deployment guides: [Render](docs/DEPLOY-RENDER.md) (Blueprint in [`render.yaml`](render.yaml)),
[AWS](docs/DEPLOY-AWS.md) (EC2 + Docker Compose, or ECS Fargate with RDS/ElastiCache/EFS provisioned by
[`infra/aws`](infra/aws) and deployed by [`.github/workflows/deploy-aws.yml`](.github/workflows/deploy-aws.yml)).
