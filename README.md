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
4. Open `http://localhost:5173`; API docs are at `http://localhost:8000/docs`.

For local development with the host Python environment, apply migrations with `Push-Location backend; ..\\.venv\\Scripts\\alembic.exe -c alembic.local.ini upgrade head; Pop-Location`.

For development without containers, create a virtual environment, run `pip install -e "./backend[dev]"`, run `npm install` in `frontend`, and supply PostgreSQL/Redis yourself. Start the API with `uvicorn app.main:app --app-dir backend --reload` and the UI with `npm run dev` in `frontend`.

Authentication is temporarily disabled for local development with `AUTHENTICATION_ENABLED=false` and `VITE_AUTH_ENABLED` unset. Set both values to `true` before enabling authentication, staging, or production.

## Validation commands

```powershell
& .\.venv\Scripts\ruff.exe check backend/app backend/tests scripts
Push-Location backend; & ..\.venv\Scripts\mypy.exe app; Pop-Location
Push-Location backend; & ..\.venv\Scripts\pytest.exe -q; Pop-Location
Push-Location frontend; npm run build; npm test; Pop-Location
python scripts/evaluate.py
```

See [architecture](docs/ARCHITECTURE.md), [deployment](docs/DEPLOYMENT.md), [security](docs/SECURITY.md), and [testing](docs/TESTING.md).
