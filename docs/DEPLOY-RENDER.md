# Deploying to Render

This is the step-by-step plan for running the Mansam RAG platform on
[Render](https://render.com) with the Blueprint in [`render.yaml`](../render.yaml). It replaces the
Compose topology in [`docker-compose.yml`](../docker-compose.yml) with managed equivalents:

| Local Compose service | Render resource | Notes |
| --- | --- | --- |
| `postgres` (`pgvector/pgvector:pg16`) | `mansam-db` (Render Postgres 16) | `pgvector` is a supported extension and migration `0001` runs `CREATE EXTENSION IF NOT EXISTS vector` |
| `redis` | `mansam-kv` (Render Key Value) | Celery broker, rate-limit counters, short-term conversation memory |
| `backend` (uvicorn) | `mansam-api` (Docker web service) | Public API, `/health/*`, `/metrics` |
| `worker` (Celery) | **inside `mansam-api`** | See "The two constraints that shape this deployment" |
| `frontend` (nginx + `dist`) | `mansam-ui` (static site) | CDN, SPA rewrite |
| `reranker`, `speech` | *not deployed* | GPU/speech images; use hosted endpoints or disable |
| `uploads/` volume | instance filesystem (or optional disk) | See "Uploaded files" |

Verified platform behaviour this plan relies on: Render Postgres supports `pgvector` through
`CREATE EXTENSION`; a persistent disk is attached to **one instance**, is invisible to other
services, cannot be read during build or pre-deploy, and **disables zero-downtime deploys**; a Docker
web service must bind `0.0.0.0` and Render routes to the `PORT` you set (default `10000`); free
Postgres instances are deleted 30 days after creation; `preDeployCommand` runs on separate compute
just before each deploy, which is where migrations belong.

## The two constraints that shape this deployment

1. **The ingestion worker reads the uploaded file from local disk.** `POST /api/v1/documents` writes
   the upload to `uploads/<tenant>/<uuid>-<name>` and stores that **absolute path** in
   `document_versions.storage_path`; the Celery task then does
   `Path(version.storage_path).read_bytes()`. There is no S3 adapter and no download endpoint, and a
   Render disk cannot be shared between two services, so the API and the worker run **in one
   container** (`dockerCommand` in `render.yaml`). If the worker is not running, uploads stay
   `queued` forever while chat keeps working.
2. **Render has no GPUs.** The cross-encoder reranker, the speech server and a local Ollama model
   cannot run here. Point `RERANK_INFERENCE_URL` at a hosted reranker or leave it empty (retrieval
   then runs on reciprocal rank fusion alone), and leave `STT_PROVIDER`/`TTS_PROVIDER` disabled — the
   UI keeps text input and reports `503` for the microphone and playback controls.

## Step 0 — What to have ready before you start

| You need | Where it comes from | Used by |
| --- | --- | --- |
| The repository on GitHub, on the branch you deploy | this repo | every Render service |
| Render workspace on a paid plan | Render Dashboard → Billing | workers, disks, Key Value persistence; free web services sleep and free Postgres expires |
| `HF_TOKEN` | huggingface.co → Settings → Access tokens (a token allowed to call Inference Providers) | answer generation, verification, embeddings auth |
| `HF_INFERENCE_URL` | e.g. `https://router.huggingface.co` | `app/llm/huggingface.py` appends `/v1/chat/completions` itself, so give the **base URL** (the full `.../v1/chat/completions` URL is also accepted) |
| `HF_MODEL` | a model id your provider serves | draft model; `LLM_DRAFT_MODEL` / `LLM_FAST_MODEL` can split the stages |
| `EMBEDDING_INFERENCE_URL` | a TEI container `/embed`, or any OpenAI-compatible `/v1/embeddings` | ingestion + query embedding; a URL containing `/v1/embeddings` is sent the OpenAI batch payload, anything else the native `{"inputs": [...]}` payload |
| `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION` | must match each other (`intfloat/multilingual-e5-large` → `1024`) | ingestion fails loudly on a dimension mismatch |
| Bootstrap admin email + password | your choice (password ≥ 12 chars when created through the API) | `scripts/bootstrap_admin.py` |
| CI green on the branch | `.github/workflows/ci.yml` (`ruff`, `mypy`, `pytest`, `npm run build`, `npm test`) | `autoDeployTrigger: checksPass` deploys only after the checks pass |
| A local checkout with the repo's virtualenv | `README.md` → Run locally | optional local admin bootstrap |

## Step 1 — Create the Blueprint

1. Commit and push the branch you deploy (the Blueprint lives at the repository root).
2. Render Dashboard → **Blueprints** → **New Blueprint Instance** → connect the repository.
3. Render parses `render.yaml` and shows the four resources it will create. The Blueprint is
   validated against `https://render.com/schema/render.yaml.json`; a schema error stops the sync
   before anything is created.
4. Keep the region identical in every block (`oregon` in the committed file). Resources in the same
   region talk over Render's private network, which is what makes the internal database and Key Value
   URLs reachable from the API.

Names are part of the contract: `mansam-api`, `mansam-ui`, `mansam-db`, `mansam-kv`. The API's CORS
allow-list (`FRONTEND_ORIGINS`) and the UI's build-time API URL are written against
`https://mansam-api.onrender.com` / `https://mansam-ui.onrender.com`, so if Render has to disambiguate
a name (a name already taken in another workspace appends a suffix), update both values afterwards.

```
mansam-ui.onrender.com  --HTTPS-->  mansam-api.onrender.com  --private network-->  mansam-db
      (static, CDN)                 (FastAPI + Celery worker)                   mansam-kv
```

## Step 2 — Fill the values the Blueprint prompts for

`sync: false` variables are requested once, at Blueprint creation, and are stored as secrets afterwards.
Nothing secret is committed: `JWT_SECRET` is generated by Render (`generateValue: true`) and the two
service URLs are wired with `fromService`.

| Variable | Value to enter |
| --- | --- |
| `DATABASE_URL` | `mansam-db` → **Internal Database URL**, with the scheme rewritten — see below |
| `HF_INFERENCE_URL` | `https://router.huggingface.co` (base URL; the client appends `/v1/chat/completions`) |
| `HF_TOKEN` | your Hugging Face token |
| `HF_MODEL` | the served model id, e.g. `meta-llama/Llama-3.1-8B-Instruct` |
| `LLM_DRAFT_MODEL`, `LLM_FAST_MODEL` | optional; leave blank to use `HF_MODEL` for every stage |
| `EMBEDDING_INFERENCE_URL` | your embedding endpoint (see the table in Step 0) |
| `RERANK_INFERENCE_URL` | leave **empty** on Render (no GPU), or a hosted reranker URL |
| `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` | the first operator account |
| `VITE_API_BASE_URL` (UI) | `https://mansam-api.onrender.com/api/v1` — the UI's `API_BASE` expects the prefix included |

**The database URL needs one edit.** Render hands out `postgresql://…`; this application creates an
async engine, so the scheme must name the driver: `postgresql+asyncpg://`. Paste the internal URL and
replace only the scheme:

```text
postgresql://rag:<password>@dpg-xxxxxxxxxxxx-a/rag          <- as shown in the Dashboard
postgresql+asyncpg://rag:<password>@dpg-xxxxxxxxxxxx-a/rag  <- what DATABASE_URL must be
```

Use the **internal** URL (host ends in `-a`, no domain suffix): same-region traffic, no TLS handshake,
no egress. The external URL is only for tools running outside Render, and it requires TLS — add
`?ssl=require` to the query string when you use it from a workstation.

If the Blueprint syncs before the database is ready, set `DATABASE_URL` afterwards from
`mansam-api` → **Environment** → save; that triggers a redeploy. With a `postgresql://` URL SQLAlchemy
refuses the engine (`create_async_engine` resolves the default sync driver `psycopg2`, which is not
installed), which shows up as an import/InvalidRequest error at startup rather than a database error.

## Step 3 — First deploy: build, migrate, start

The deploy runs in this order for `mansam-api`:

1. **Build.** `docker build` with the repository root as context (`dockerContext: .`) and
   `docker/backend.Dockerfile` as the Dockerfile. `.dockerignore` keeps `.venv`, `node_modules`,
   `uploads/`, `tmp/`, `*.log` and the notebook corpora out of the context, so the build only carries
   `backend/`, `migrations/`, `prompts/`, `scripts/` and `frontend/`. Dependencies are built as wheels
   in a builder stage, then installed into a slim runtime image that runs as the non-root user `app`.
2. **Pre-deploy** — `preDeployCommand: alembic -c alembic.ini upgrade head` on separate compute.
   `alembic.ini` resolves `script_location = /app/migrations` and `migrations/env.py` takes the URL
   from `DATABASE_URL`, so this creates the schema: `0001` runs
   `CREATE EXTENSION IF NOT EXISTS vector` and then creates every table, `0002` expands the
   verification status. A failure here aborts the deploy — the old instance keeps serving.
3. **Start** — the container command runs the worker and the API in one process tree:
   `celery -A app.workers.celery_app:celery_app worker --loglevel=INFO --concurrency=2 & exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers`
4. **Health check** — Render polls `/health/live` until it answers `{"status":"ok"}`.

Confirm in the logs:

```text
# pre-deploy
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Initial application, retrieval, memory, and telemetry schema
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, ...

# start command - the task must be listed, or every job is accepted and then discarded
[tasks]
  . ingestion.index_document

# uvicorn
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

`GET /api/v1/docs` is served under the API prefix, and every request is logged as one `http_request`
line with `status`, `duration_ms` and `caller`, which is the fastest way to see whether a slow answer
was retrieval or the model.

## Step 4 — Create the first administrator

`scripts/bootstrap_admin.py` creates one `admin` user from `BOOTSTRAP_ADMIN_EMAIL` /
`BOOTSTRAP_ADMIN_PASSWORD` and does nothing if that email already exists, so it is safe to re-run.
It is the only way to get the first account: `/api/v1/auth/users` requires an existing admin.

It runs inside the deployed image. `docker/backend.Dockerfile` installs the `app` package into
`site-packages` (hatchling `packages = ["app"]`, so `import app.…` resolves from any working
directory) and copies `scripts/` into `/app/scripts` — both are needed for this to work.

**Option A — one-off job on Render (preferred).** `mansam-api` → **Jobs** → **New one-off job**, command:

```bash
/bin/sh -c "cd /app && python scripts/bootstrap_admin.py"
```

It runs the built image with the service's environment, so it reaches the internal database without
TLS or a public URL. One-off jobs run on separate compute, so a mounted disk is not visible to them —
irrelevant here, the script only writes to Postgres. Expected output: `Admin created`, or
`Admin already exists` if you run it twice.

**Option B — from a workstation, against the external database URL.** The repository's virtualenv must
have the backend installed (`pip install -e "./backend[dev]"`), because the script imports `app.*`:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://rag:<password>@<external-host>/rag?ssl=require"
$env:BOOTSTRAP_ADMIN_EMAIL = "admin@example.com"
$env:BOOTSTRAP_ADMIN_PASSWORD = "<at-least-12-characters>"
& .\.venv\Scripts\python.exe scripts\bootstrap_admin.py
```

The external URL requires TLS; `?ssl=require` is what asyncpg expects. Create further users through
`POST /api/v1/auth/users` as an admin (`role: user | editor | admin`), and remember that a user with
`role: editor` is the minimum needed to upload documents.

## Step 5 — Point the UI at the API, and confirm CORS

Two different settings, applied at two different times:

| Setting | Where it is read | What a wrong value looks like |
| --- | --- | --- |
| `VITE_API_BASE_URL` (UI) | **build time** — Vite inlines it into the bundle | the UI calls `http://127.0.0.1:8000` (its fallback) or a 404 path; changing it requires a UI redeploy, not a restart |
| `FRONTEND_ORIGINS` (API) | **startup** — `app/main.py` CORS middleware and the `_cors_headers` fallback for `429` | the browser reports a CORS error although the API logged the request; changing it requires an API restart/redeploy |

`FRONTEND_ORIGINS` must match the browser origin exactly: scheme, host, and **no trailing slash**
(`https://mansam-ui.onrender.com`). Add every origin you serve the UI from — a custom domain, the
`onrender.com` URL, a staging site — as a comma-separated or JSON list; the settings parser accepts
both. If a *custom domain* becomes the primary URL, add it here as well instead of replacing the
`onrender.com` entry if both stay reachable.

## Step 6 — Verify the deployment end to end

Run this in order; each step depends on the previous one. `$API` is
`https://mansam-api.onrender.com`, `$TOKEN` is the access token from step 2.

```bash
# 1. Process and dependencies
curl -sS "$API/health/live"     # {"status":"ok"}
curl -sS "$API/health/ready"    # {"status":"ready","checks":{"database":true,"redis":true}}

# 2. Sign in (the UI calls the same route)
TOKEN=$(curl -sS -X POST "$API/api/v1/auth/token" -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"<password>"}' | python -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
curl -sS "$API/api/v1/auth/me" -H "Authorization: Bearer $TOKEN"

# 3. Upload one source; the response is an ingestion job, not a document
curl -sS -X POST "$API/api/v1/documents" -H "Authorization: Bearer $TOKEN" \
  -F "file=@Mansam_Booklet_Spreads-EN.pdf" -F "access_scope=tenant" -F "category=catalogue"

# 4. Watch the worker: queued -> processing (extracting/chunking/embedding) -> completed
curl -sS "$API/api/v1/documents/jobs/<job_id>" -H "Authorization: Bearer $TOKEN"

# 5. Ask a question that only the uploaded source can answer
curl -sS -X POST "$API/api/v1/chat" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"How many boutiques does Mansam have in KSA?"}'

# 6. Upload the same file twice: the second call must answer 409
```

What to check in the answers: `grounded: true`, a non-empty `citations` array whose excerpts come from
the file you uploaded, and `verification_status` = `verified` (or `verification_skipped` when
`RAG_VERIFY_ENABLED=false`). Ask one question the corpus cannot answer — the service must return the
uncertainty response rather than an invented one, and one follow-up question in the same
`conversation_id` to confirm short-term memory is reaching the model.

Finish with the UI: open `https://mansam-ui.onrender.com`, sign in, send a message, expand **Sources**,
and confirm the request id in the answer matches the `http_request` line in the API logs.

Operational notes for `/metrics`: it is unauthenticated and exposes counters and latencies only (no user
content). Keep it out of public documentation; if the endpoint must be restricted, Render's
`ipAllowList` applies to the whole service and is available on Scale/Enterprise workspaces, so a
separate internal monitoring path is usually the better answer.

## Step 7 — Custom domains and TLS

Render terminates TLS at the edge with a managed certificate and redirects HTTP to HTTPS, so the
application never sees a certificate. The start command already passes `--proxy-headers`, which is what
makes `request.client.host` and the rate limiter see the real client behind the proxy.

1. `mansam-api` → **Settings** → **Custom Domains** → add `api.example.com`; `mansam-ui` → add
   `rag.example.com`.
2. Create the CNAME records Render shows at your DNS provider. Certificates are issued automatically
   once the records resolve — usually minutes, up to an hour.
3. Update the two cross-links, in this order:
   - `mansam-api` → Environment → `FRONTEND_ORIGINS` = `https://mansam-ui.onrender.com,https://rag.example.com`
     (keep the `onrender.com` origin if it stays reachable), save, and let the API restart.
   - `mansam-ui` → Environment → `VITE_API_BASE_URL` = `https://api.example.com/api/v1`, then
     **redeploy the UI** — this value is inlined into the bundle at build time.
4. Optionally set `PUBLIC_BASE_URL` on the API; it is informational in the current code (nothing reads
   it yet), which is why the Blueprint derives it from `RENDER_EXTERNAL_URL`.

Custom domains count against the workspace plan's included allowance. The `onrender.com` URLs keep
working either way.

## Step 8 — Day-two operations

**Deploys.** `autoDeployTrigger: checksPass` deploys a commit only after the repository's GitHub checks
pass (`.github/workflows/ci.yml` runs `ruff`, `mypy`, `pytest`, `python scripts/evaluate.py`,
`pip-audit`, `npm run build`, `npm test`). Every deploy runs the pre-deploy migration and then swaps the
instance. Without a disk the swap is zero-downtime; with the optional disk Render stops the old instance
first, which is a few seconds of unavailability. **Instant rollback** restores a previous image, but it
does not roll the schema back — keep migrations additive (both existing revisions are).

**Logs.** The Log Explorer is the first stop; search for `error`. Three line types carry the diagnosis:

| Line | Read it for |
| --- | --- |
| `http_request` | `status`, `duration_ms`, `caller` (the browser origin, or `direct` for curl and scripts) |
| `llm_generate` | `duration_ms`, `prompt_tokens`, `completion_tokens` per generation — one question is several sequential calls |
| `embedding_request_failed` | endpoint, error type and message when ingestion or retrieval stalls |

Celery and uvicorn logs interleave in one stream because they share the container; the Celery `[tasks]`
banner and the uvicorn startup lines are how you tell which process restarted.

**Scaling.** This service must stay at one instance while uploads live on the instance filesystem and
the worker shares the container: a second instance would run a second worker that cannot see the files
the first instance wrote, and those jobs would fail with `missing_source`. The scaling path is object
storage (below) plus a separate `type: worker` service, after which `numInstances` or a `scaling:` block
become safe.

**Backups and retention.** Render Postgres takes automatic daily backups with point-in-time recovery on
paid plans; deleting a database deletes its backups, so export before destructive changes. Free Postgres
instances are removed 30 days after creation — another reason the Blueprint uses a paid plan. A
persistent disk is snapshotted every 24 hours with at least 7 days of retention, and a restore is
full-disk only.

**Cost.** Four resources are created; check render.com/pricing for current amounts, since plan names and
prices change:

| Resource | Plan in `render.yaml` | Why that size |
| --- | --- | --- |
| `mansam-api` | `starter` | smallest paid tier; the API **and** the Celery worker share it, and PDF/XLSX extraction is the memory spike |
| `mansam-ui` | static (no plan) | static sites are served from the CDN |
| `mansam-db` | `basic-256mb` | small corpus; raise in place (never lower) as chunks grow |
| `mansam-kv` | `starter` | the smallest plan with persistence, so queued jobs survive a restart |

Outbound bandwidth and pipeline minutes (build plus pre-deploy time) are metered per workspace; the
`.dockerignore` and the builder stage's wheel caching in the Dockerfile are what keep build minutes down.

## Uploaded files: ephemeral, a disk, or object storage

The API writes each upload to `uploads/<tenant_id>/<uuid>-<name>` and stores that absolute path on the
document version; the worker re-reads it with `Path(version.storage_path).read_bytes()`. Three options,
in ascending order of correctness:

1. **Ephemeral (the committed default).** No disk: the file lives as long as the instance. Chunks and
   embeddings are already in Postgres, so chat and citations are unaffected; what breaks is
   `POST /api/v1/documents/{id}/reindex` and any re-ingestion that needs the original bytes after a
   redeploy. A deploy or crash between an upload and the worker picking it up fails that one job
   (`missing_source`), and re-uploading the file fixes it.
2. **Persistent disk (uncomment the block in `render.yaml`).** `mountPath: /app/uploads` plus `sizeGB`
   keeps those files across deploys and restarts. The price is explicit in Render's model: the service is
   pinned to **one** instance and loses zero-downtime deploys.
3. **Object storage — the real fix, not implemented here.** Write uploads to S3/R2, store the key instead
   of an absolute path, and read the bytes in the worker. That removes the single-instance constraint,
   allows a separate worker service and horizontal scaling, and is the prerequisite for running more than
   one API instance. There is no storage abstraction and no S3 client in `backend/pyproject.toml` today,
   so this is a code change — budget for it before you need a second instance.

Bounds that apply in every option: `MAX_UPLOAD_SIZE_MB=25`, `ALLOWED_UPLOAD_TYPES` (PDF, DOCX, XLSX, TXT,
MD, HTML, CSV, JSON), content-hash duplicate rejection (`409`), and `URL_INGESTION_ENABLED=false` unless
`URL_ALLOWED_HOSTS` is also configured.

## What cannot run on Render

| Compose service | Why not | Replacement |
| --- | --- | --- |
| `reranker` (TEI + BAAI/bge-reranker-v2-m3) | needs an NVIDIA GPU (`gpus: all`) | a hosted reranker URL in `RERANK_INFERENCE_URL`, or leave it empty: retrieval still runs hybrid vector + full-text with reciprocal-rank fusion |
| `speech` (speaches: Whisper + Kokoro) | no managed service to attach and it holds gigabytes of model files | a hosted speech endpoint (`STT_PROVIDER=openai\|huggingface`) or keep `STT_PROVIDER`/`TTS_PROVIDER=disabled`; the UI keeps text input and gets `503` for audio |
| a local Ollama answer model | needs a GPU and a persistent model cache | any OpenAI-compatible `/v1/chat/completions` endpoint in `HF_INFERENCE_URL` (`KEEP_MODEL_WARM` exists only for the self-hosted case and stays `false` here) |
| `postgres`, `redis` containers | Render provides managed equivalents | `mansam-db`, `mansam-kv` |

## Settings that decide behaviour in production

Everything else is in [`backend/app/config/settings.py`](../backend/app/config/settings.py); these are the
ones that change what a deployment does.

| Setting | Effect |
| --- | --- |
| `APP_ENV=production` | enables `validate_runtime`, which refuses to start without `AUTHENTICATION_ENABLED=true`, `HF_INFERENCE_URL`, `HF_TOKEN` and `EMBEDDING_INFERENCE_URL` |
| `AUTHENTICATION_ENABLED=true` | the API rejects unauthenticated traffic; `VITE_AUTH_ENABLED=true` makes the UI show the login screen. They must agree, or the UI shows a login it cannot complete |
| `RAG_VERIFY_ENABLED` | `true` adds an independent verification generation: the strongest grounding guarantee, roughly doubling the time to an answer. `false` keeps the admission gate, the model's own `grounded` flag and citation resolution, and stores `verification_skipped` |
| `RAG_MIN_SCORE`, `RAG_MIN_EVIDENCE` | the admission gate: below either threshold the service returns the uncertainty response instead of an answer |
| `RERANK_TOP_K` (8) | measured floor for this corpus: dropping to 5 lost the customer-facing delivery line and the rows needed to count boutiques |
| `RAG_MAX_CONTEXT_CHARS` (16000) | size of the draft prompt; raise it only with a model that handles the context |
| `PLANNER_LLM_MIN_WORDS` (6) | a short first-turn question skips the planner generation; raise it to trust the model with shorter turns |
| `LONG_TERM_MEMORY_INLINE=false` | extracts memories after the answer is committed instead of holding the reply behind one more generation and embedding call |
| `RATE_LIMIT_PER_MINUTE` (30) | per client IP per minute, enforced in Redis; a Redis outage answers `503` rather than failing open |
| `INGESTION_EXCLUDED_SHEETS`, `INGESTION_WORKFLOW_COLUMNS`, `INGESTION_UNPUBLISHED_MARKERS` | keeps internal tabs and draft rows out of the index — set these before loading the SSOT workbook |
| `MALWARE_SCANNER_COMMAND` | optional; runs against each stored upload before it is committed |

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Deploy fails at the pre-deploy step | `alembic` could not reach Postgres or `vector` could not be created. Check that `DATABASE_URL` uses the asyncpg scheme and that the database is in the same region; read the migration output in the deploy log |
| The service starts and then exits immediately | `validate_runtime`: one of `AUTHENTICATION_ENABLED`, `HF_INFERENCE_URL`, `HF_TOKEN`, `EMBEDDING_INFERENCE_URL` is empty, or `JWT_SECRET` is shorter than 32 characters |
| `502 Bad Gateway` | nothing is listening on `0.0.0.0:8000`, or `PORT` (pinned to `8000`) disagrees with the process. Check the logs for a crash loop first |
| `/health/ready` returns `503` | Postgres or Redis is unreachable: read `checks.database` and `checks.redis` in the response body |
| An upload is accepted but the job never leaves `queued` | the Celery process died. Restart the service and confirm the `[tasks]` banner lists `ingestion.index_document` |
| A job fails with `missing_source` | the worker could not read `storage_path` — the file is not on this instance (see "Uploaded files") |
| `429 Rate limit exceeded` | `RATE_LIMIT_PER_MINUTE` reached for that client; raise the value or investigate the caller. Each `http_request` line logs the origin |
| The browser reports a CORS failure but the API logged the request | `FRONTEND_ORIGINS` does not contain the exact origin (check scheme, host, trailing slash), or the API has not restarted since the change |
| Answers are slow | read the `llm_generate` durations: several sequential generations per question are expected, and `RAG_VERIFY_ENABLED=false` removes one. A cold model on the provider side adds seconds to the first call |
| Ingestion fails on embeddings | dimension mismatch (`EMBEDDING_DIMENSION` must match the model) or the wrong payload shape — a URL containing `/v1/embeddings` gets the OpenAI batch payload, anything else the native HF/TEI shape |
| The UI calls `127.0.0.1:8000` | `VITE_API_BASE_URL` was empty at build time; set it and redeploy the UI (a restart is not enough) |

## Appendix A — The same deployment without a Blueprint

If `render.yaml` cannot be used (no Blueprint instance, or the resources already exist), create the four
resources by hand in this order and then continue from Step 2:

1. **New → Postgres.** Name `mansam-db`, region = the API's region, PostgreSQL **16**, database `rag`,
   user `rag`, no external IP allow-list entries. Copy the *Internal Database URL*.
2. **New → Key Value.** Name `mansam-kv`, same region, a plan with persistence, max memory policy
   `noeviction`, internal connections only. Copy the *Internal* connection string for `REDIS_URL`.
3. **New → Web Service → Build and deploy from a Git repository**, Language **Docker**:
   - Dockerfile Path `docker/backend.Dockerfile`, Docker Context `.`
   - Docker Command `/bin/sh -c "celery -A app.workers.celery_app:celery_app worker --loglevel=INFO --concurrency=2 & exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"`
   - Health Check Path `/health/live`, Pre-Deploy Command `alembic -c alembic.ini upgrade head`
   - Environment: every variable in the Step 2 table plus the non-secret values from `render.yaml`
     (`APP_ENV`, `API_PREFIX`, `LOG_LEVEL`, `PORT=8000`, the retrieval and ingestion sizing values,
     `RAG_VERIFY_ENABLED`, `LONG_TERM_MEMORY_INLINE=false`, `URL_INGESTION_ENABLED=false`)
   - Auto-Deploy: *After CI Checks Pass*, so a red `ci.yml` cannot reach production
4. **New → Static Site.** Name `mansam-ui`, build command `cd frontend && npm ci && npm run build`,
   publish directory `frontend/dist`, environment `SKIP_INSTALL_DEPS=true`,
   `VITE_API_BASE_URL=https://mansam-api.onrender.com/api/v1`, `VITE_AUTH_ENABLED=true`, and a
   **Rewrite** rule from `/*` to `/index.html` (plus the `Cache-Control` / security headers from
   `render.yaml` if you want them).

Everything else — migrations, the admin bootstrap, CORS, verification — is identical to Steps 3 to 6.

## Appendix B — What this deployment deliberately leaves out

| Not included | Consequence / when to add it |
| --- | --- |
| Object storage for uploads | single instance, no horizontal scaling, source files lost on redeploy (see "Uploaded files") |
| A separate Celery worker service | the worker competes with the API for the same CPU and memory. Splitting it is only safe after uploads move to object storage |
| Self-hosted reranker, speech and LLM | quality/latency depends on the hosted endpoints you supply; without a reranker the ranking is RRF only, and without speech endpoints the voice controls are disabled |
| Malware scanning | `MALWARE_SCANNER_COMMAND` is unset, so `_scan_file` is a no-op; configure it if uploads are not trusted |
| URL ingestion | `URL_INGESTION_ENABLED=false`; enabling it also requires `URL_ALLOWED_HOSTS` (the SSRF validator rejects private, loopback, link-local, multicast and reserved addresses) |
| WAF / bot filtering | only Render's platform DDoS protection; authentication is application-level (JWT + Argon2) |
| Log/metrics shipping | Render keeps service logs; `/metrics` is not scraped. Add an external scrape target or the OTLP/Sentry settings (`OTEL_EXPORTER_OTLP_ENDPOINT`, `SENTRY_DSN`) if you need retention |
| The evaluation and live-provider suites | `python scripts/evaluate.py`, `scripts/verify_live_ingestion.py` and the `live`/`e2e` pytest markers are how a deployment is proven, and they must be run against real infrastructure — see [`docs/TESTING.md`](TESTING.md) |

Related: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md), [`docs/SECURITY.md`](SECURITY.md),
[`docs/DEPLOYMENT.md`](DEPLOYMENT.md) (platform-neutral notes) and
[`docs/DEPLOY-AWS.md`](DEPLOY-AWS.md) for the ECS/RDS/ElastiCache path.






