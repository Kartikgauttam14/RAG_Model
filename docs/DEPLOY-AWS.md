# Deploying to AWS

Two tracks are documented here, and they use the same image, the same migrations and the same
environment contract:

| | **Option A — one EC2 instance** | **Option B — ECS Fargate + managed services** |
| --- | --- | --- |
| Shape | the repository's `docker-compose.yml`, productionised (Postgres, Redis, API, worker on one box) | API and worker as ECS services, RDS Postgres, ElastiCache, EFS, ALB, S3 + CloudFront |
| Effort | an afternoon; nothing new to learn | a day of infrastructure work; Terraform in [`infra/aws`](../infra/aws) |
| Cost | one instance + EBS + bandwidth | ALB + Fargate tasks + RDS + ElastiCache + EFS + CloudFront, per hour |
| Scaling | vertical only; a restart is downtime | horizontal API scaling, rolling deploys, no downtime |
| Data safety | your responsibility (EBS snapshots, `pg_dump`) | RDS automated backups/PITR, managed failover options |
| Best for | staging, demos, a single-tenant internal tool, proving the pipeline end to end | production with real users, or when a second instance is needed |
| GPU services | possible on a `g`-series instance (reranker, speech, Ollama in the same Compose project) | not on Fargate; use hosted endpoints, or a separate GPU EC2 box for the reranker |

Both tracks are Step-by-step below; a `local service → AWS service` map is at the end.

## What is the same on both tracks

**The production configuration gate.** `app/config/settings.py::validate_runtime` refuses to start when
`APP_ENV` is `staging` or `production` unless `AUTHENTICATION_ENABLED=true` **and** `HF_INFERENCE_URL`,
`HF_TOKEN` and `EMBEDDING_INFERENCE_URL` are all set. `JWT_SECRET` must be at least 32 characters.
Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Migrations.** `alembic -c alembic.ini upgrade head` run from `/app` inside the image
(`alembic.ini` → `script_location = /app/migrations`; `migrations/env.py` takes the URL from
`DATABASE_URL`). Revision `0001` runs `CREATE EXTENSION IF NOT EXISTS vector` and creates every table,
`0002` expands the verification status. Run migrations **before** the API and worker of the new version
start, and never roll the schema back with the code — both revisions are additive.

**Uploads are read by the worker from the filesystem.** `POST /api/v1/documents` stores the file under
`uploads/<tenant_id>/` and writes that absolute path to `document_versions.storage_path`; the Celery task
does `Path(version.storage_path).read_bytes()`. There is no S3 adapter and no download endpoint. Every
AWS topology therefore has to give the API and the worker a shared `uploads/` path:

| Track | How the API and worker share the file |
| --- | --- |
| Option A | they are containers on the same instance, sharing the `./uploads` bind mount from `docker-compose.yml` |
| Option B | an EFS access point mounted at `/app/uploads` in **both** task definitions (Terraform does this) |
| Alternative on either track | a separate worker service with its own copy of the file is *not* an option; object storage is the only way to decouple them, and it needs a code change |

**Bootstrap the first administrator** with `python scripts/bootstrap_admin.py` (idempotent; needs
`BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`). The image contains `scripts/` and installs the
`app` package into `site-packages`, so the script runs from any working directory inside a container.

**GPU services are optional and replaceable.** `docker-compose.yml` runs a cross-encoder reranker
(Text Embeddings Inference, `gpus: all`), a speech server and, in some setups, Ollama. Without them set
`RERANK_INFERENCE_URL` empty (retrieval then runs hybrid vector + full-text with reciprocal-rank fusion)
and `STT_PROVIDER`/`TTS_PROVIDER=disabled`; with a hosted endpoint, point the URL at it. This document
assumes hosted model endpoints on both tracks and treats self-hosting as an add-on (Option A, step A8).

**Two Redis details that decide the ElastiCache configuration.** The application builds its client with
`Redis.from_url(settings.redis_url)`, and redis-py 5 defaults `ssl_cert_reqs` to `required`, so a
`rediss://` URL fails certificate verification against ElastiCache's own CA unless the code is changed to
pass `ssl_ca_certs` (the Amazon trust bundle). And ElastiCache only accepts an AUTH token when
in-transit encryption is enabled ("AUTH can only be enabled for encryption in-transit enabled Valkey or
Redis OSS clusters"), so TLS and a password cannot be chosen independently. Terraform therefore creates
the replication group with **encryption in transit off, no AUTH token**, at-rest encryption on, a private
subnet group and a security group that only the ECS tasks can reach: network isolation is what protects
Redis in this topology, and the whole URL is `redis://<host>:6379/0`. If compliance requires TLS in
transit, make the code change in `app/dependencies.py` and in the Celery broker URL first — then
`rediss://:<auth-token>@<host>:6379/0` becomes available as well.

## Option A — one EC2 instance with Docker Compose

This is the repository's own `docker-compose.yml`, moved to a server. Everything below uses
`rag.example.com` = the hostname you serve.

### A1 — Choose the instance

| Topology | Instance | Why |
| --- | --- | --- |
| API + worker + Postgres + Redis, **hosted** model endpoints | `t3.large` (2 vCPU / 8 GB) | Postgres, Redis, uvicorn and the Celery worker share this RAM; PDF/XLSX extraction is the spike |
| The same, plus the GPU services (reranker / speech / Ollama) in the Compose project | `g5.xlarge` (4 vCPU / 16 GB / 1 × A10G 24 GB) | one GPU is shared by the reranker and the model server; the speech service is deliberately CPU-only |

Disk: **gp3, 50 GB**. The images (`text-embeddings-inference`, `speaches`, `pgvector/pgvector`, plus the
two build images) are several GB each before any data, and `uploads/` grows with the corpus.

### A2 — Create the instance and the security group

Open **22** (from your IP only, or not at all if you use SSM Session Manager), **80** and **443** (from
anywhere). Do **not** open `5432`, `6379`, `8000`, `8082` or `8083`: `docker-compose.yml` publishes
Postgres, Redis, the reranker and the speech server on the host, so the security group is the only thing
keeping them off the internet. Attach an **Elastic IP** so the address survives a stop/start.

```bash
# AMI ids differ per region and architecture; look one up rather than copying
aws ssm get-parameter \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text

aws ec2 run-instances --image-id <ami-id> --instance-type t3.large \
  --key-name <key-pair> --security-group-ids <sg-id> \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=50,VolumeType=gp3}' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=mansam-rag}]'

aws ec2 allocate-address --domain vpc
aws ec2 associate-address --instance-id <instance-id> --allocation-id <eipalloc-id>
```

### A3 — Install Docker

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"      # log out and back in
docker compose version
```

### A4 — Clone the repository and write `.env`

```bash
sudo mkdir -p /srv && sudo chown "$USER" /srv
git clone https://github.com/<owner>/rag-from-scratch.git /srv/rag
cd /srv/rag && cp .env.example .env && chmod 600 .env
```

`.env` is gitignored, and `docker-compose.yml` loads it with `env_file: .env` for both the API and the
worker. Change at least these values:

| Variable | Value on this server |
| --- | --- |
| `APP_ENV` | `production` |
| `AUTHENTICATION_ENABLED` | `true` |
| `JWT_SECRET` | output of `python -c "import secrets; print(secrets.token_urlsafe(48))"` (≥ 32 chars) |
| `DATABASE_URL` | `postgresql+asyncpg://rag:<new-password>@postgres:5432/rag` — `postgres` is the Compose service name |
| `DATABASE_ADMIN_URL` | not read by any code today; leave it or delete it |
| `REDIS_URL` | `redis://redis:6379/0` |
| `FRONTEND_ORIGINS` | `https://rag.example.com` (the browser origin; no trailing slash) |
| `HF_INFERENCE_URL` | base URL only, e.g. `https://router.huggingface.co` — the client appends `/v1/chat/completions` |
| `HF_TOKEN`, `HF_MODEL` | your provider token and served model id |
| `EMBEDDING_INFERENCE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION` | a TEI `/embed` endpoint (native payload) or an OpenAI-compatible `/v1/embeddings`; the dimension must match the model |
| `RERANK_PROVIDER`, `RERANK_INFERENCE_URL` | `disabled` + empty, or `huggingface` + a hosted reranker (a URL ending in `/rerank` is treated as TEI) |
| `STT_PROVIDER`, `TTS_PROVIDER` | `disabled`, or a hosted speech endpoint |
| `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` | the first admin account |
| `LONG_TERM_MEMORY_INLINE` | `false` — takes one generation off the answer's critical path |
| `RAG_VERIFY_ENABLED` | `true` for the strongest grounding (about double the latency) or `false` |
| `INGESTION_EXCLUDED_SHEETS`, `INGESTION_WORKFLOW_COLUMNS`, `INGESTION_UNPUBLISHED_MARKERS` | set these **before** loading the SSOT workbook, so internal tabs and draft rows never enter the index |
| `MAX_UPLOAD_SIZE_MB`, `RATE_LIMIT_PER_MINUTE` | `25` and `30` are the sane defaults |
| `URL_INGESTION_ENABLED` | `false` unless `URL_ALLOWED_HOSTS` is configured |

**Also change the database password in `docker-compose.yml`.** It ships `POSTGRES_PASSWORD: rag` for
local work; on a server it must match `DATABASE_URL`.

### A5 — Build and start the stack, then migrate

Start only the services this topology needs; the explicit list leaves the GPU and speech images alone:

```bash
docker compose up -d --build postgres redis backend worker
docker compose ps
docker compose exec backend alembic -c alembic.ini upgrade head
docker compose exec backend python scripts/bootstrap_admin.py
curl -fsS http://127.0.0.1:8000/health/ready   # {"status":"ready","checks":{"database":true,"redis":true}}
```

`docker-compose.yml` has no `restart:` policy, so nothing comes back after a reboot or a Docker daemon
restart. Add one with an override — Compose *replaces* scalar values from an override, so this is safe:

```yaml
# docker-compose.prod.yml
services:
  backend:
    restart: unless-stopped
  worker:
    restart: unless-stopped
  postgres:
    restart: unless-stopped
  redis:
    restart: unless-stopped
```

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Note what an override **cannot** do: Compose *concatenates* list values such as `ports`, so the published
`5432` / `6379` / `8082` / `8083` mappings cannot be removed by an override. Keep them private at the
security group, or maintain a complete production Compose file that omits them.

### A6 — The UI, TLS, and the one build-time value that bites

Run the Compose `frontend` service as-is and the browser will call `http://127.0.0.1:8000/api/v1` — that is
the compiled-in fallback in `frontend/src/services/api.ts`. The reason is specific: `docker-compose.yml`
passes `VITE_API_BASE_URL` as a **runtime** `environment:` entry, but Vite inlines
`import.meta.env.VITE_API_BASE_URL` during `docker build`, and `docker/frontend.Dockerfile` declares no
`ARG`/`ENV` for it. The value therefore never reaches the bundle.

**Recommended: build the UI on the instance and serve it with Caddy**, proxying the API on the same
origin. Same origin means no CORS at all, one certificate covers both, and `FRONTEND_ORIGINS` is simply
`https://rag.example.com`.

```bash
# Node 22 (the frontend image builds with node:22-alpine)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs
cd /srv/rag/frontend
VITE_API_BASE_URL=https://rag.example.com/api/v1 VITE_AUTH_ENABLED=true npm ci && npm run build
```

```caddyfile
# /etc/caddy/Caddyfile  (certificate issued and renewed automatically)
rag.example.com {
    encode gzip
    handle /api/*    { reverse_proxy 127.0.0.1:8000 }
    handle /health/* { reverse_proxy 127.0.0.1:8000 }
    handle /metrics  { reverse_proxy 127.0.0.1:8000 }
    handle {
        root * /srv/rag/frontend/dist
        try_files {path} /index.html
        file_server
    }
}
```

```bash
sudo apt-get install -y caddy && sudo systemctl reload caddy
```

**Alternative: keep the `frontend` container.** Add two lines to `docker/frontend.Dockerfile` before
`RUN npm run build`, and pass the value as a build argument:

```dockerfile
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
```

```bash
docker compose build --build-arg VITE_API_BASE_URL=https://rag.example.com/api/v1 frontend
```

Two further details if you take that route: `docker/nginx.conf` sets a CSP of
`connect-src 'self' http://localhost:8000 ws://localhost:8000`, so a UI served by that image cannot call a
different API origin without editing the header; and nginx buffers proxied responses by default, so a
`/chat/stream` proxy needs `proxy_buffering off;`. Caddy needs neither change.

### A7 — Verify

Run the shared [verification checklist](#verification-checklist-both-tracks) with
`$API=https://rag.example.com`, then two more checks that only exist on this track:

```bash
docker compose exec backend python scripts/evaluate.py          # golden-data contract validation
docker compose exec backend python -c "import app, celery; print('imports ok')"
docker compose ps                                               # every needed service Up, none restarting
```

### A8 — Optional: self-host the model services on the same instance

Only on a `g`-series instance with the NVIDIA driver and `nvidia-container-toolkit` installed (the
reranker image is a CUDA build; without `gpus: all` it exits with
`libcuda.so.1: cannot open shared object file`):

```bash
sudo apt-get install -y nvidia-driver-550 nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker compose up -d reranker            # BAAI/bge-reranker-v2-m3 on port 8082
docker compose up -d speech              # Whisper + Kokoro on port 8083
```

Then set in `.env`: `RERANK_PROVIDER=huggingface` with
`RERANK_INFERENCE_URL=http://127.0.0.1:8082/rerank` (a URL ending in `/rerank` is treated as Text
Embeddings Inference) and `RERANK_BATCH_SIZE=32`; and `STT_PROVIDER=openai` / `TTS_PROVIDER=openai` with
`STT_INFERENCE_URL=http://127.0.0.1:8083/v1/audio/transcriptions` and
`TTS_INFERENCE_URL=http://127.0.0.1:8083/v1/audio/speech`. Speech models are downloaded into the
container on first use (`POST /v1/models/<id>` on port 8083), and `STT_PROMPT` is worth setting to the
catalogue vocabulary — Whisper biases decoding toward it, which is what keeps product names spelled
correctly. Set `KEEP_MODEL_WARM=true` only when the answer model is a local Ollama on this box (it pings
the model every `KEEP_WARM_INTERVAL_SECONDS` so the first question after a pause does not pay a reload);
against a hosted endpoint it is pure cost.

GPU sizing is the real constraint: the project notes record load failures when an embedding model and an
answer model had to fit together on a 6 GB card. A 24 GB A10G (`g5.xlarge`) has room for the reranker and
one model server at a time.

### A9 — Backups, updates and housekeeping

```bash
# Portable logical backup (the container's postgres superuser is `rag`, the database is `rag`)
mkdir -p /srv/backups
docker compose exec -T postgres pg_dump -U rag rag | gzip > "/srv/backups/rag-$(date +%F).sql.gz"
```

Also attach an EBS snapshot schedule (Data Lifecycle Manager) to the instance volume, and copy the dumps
off the instance — a snapshot in the same account is not a disaster-recovery plan.

Update procedure, in this order, accepting a short outage (one box, one API container):

```bash
cd /srv/rag && git pull
docker compose build backend                      # new image, nothing restarted yet
docker compose exec backend alembic -c alembic.ini upgrade head   # schema before code
docker compose up -d backend worker
```

Keep the OS patched (`unattended-upgrades`) and watch `df -h`: `uploads/`, the Docker image layers and
Postgres data all live on the one 50 GB volume. Docker's own logs grow without bound unless you cap them —
either add `logging: driver: json-file, options: {max-size: "10m", max-file: "3"}` per service in the
override file or prune regularly (`docker system prune -f`).

### A10 — Hardening the box

- **Prefer SSM Session Manager over public SSH.** Attach an instance profile with
  `AmazonSSMManagedInstanceCore` and close port 22; `aws ssm start-session --target <instance-id>` gives
  shell access with no key management and an audited session.
- The security group is the only thing hiding Postgres, Redis, the reranker and the speech server (all four
  are published on the host by `docker-compose.yml`). Keep 5432/6379/8000/8082/8083 closed.
- `.env` holds `JWT_SECRET` and the provider tokens: `chmod 600`, owned by the deploy user, never in git.
- Serve `/metrics` only if something scrapes it — drop the `handle /metrics` block from the Caddyfile
  otherwise. The API's own `/metrics` is unauthenticated by design.
- Add `ufw` as defence in depth (allow 80/443 and the SSM path only), and rotate the Hugging Face token and
  the database password on a schedule; both are read from `.env` at container start, so rotation is a
  restart.

## Option B — ECS Fargate with managed data services

```text
                    ┌──────────────── CloudFront + S3 (private, OAC) ── mansam-ui static build
browser ──HTTPS──►  │
                    └──► ALB (ACM cert, /health/live target group, idle timeout 300 s)
                              │
                              ▼
                    ECS service <project>-<env>-api    (Fargate task: uvicorn, port 8000)
                    ECS service <project>-<env>-worker (Fargate task: celery, no port)
                              │   both tasks mount EFS at /app/uploads
                              ▼
                    RDS PostgreSQL 16 (pgvector, private subnets)
                    ElastiCache Redis (private subnets, at-rest encryption)
                    Secrets Manager: JWT_SECRET, HF_TOKEN, DATABASE_URL, REDIS_URL
                    CloudWatch Logs: /ecs/<project>-api, /ecs/<project>-worker…
```

The whole stack is described in [`infra/aws`](../infra/aws). Read
[`infra/aws/README.md`](../infra/aws/README.md) for the file-by-file layout.

| Terraform file | What it creates |
| --- | --- |
| `versions.tf` | provider pinning, optional S3/DynamoDB state backend (commented) |
| `variables.tf` | every knob, with defaults; `terraform.tfvars.example` shows a filled-in set |
| `network.tf` | VPC, two public + two private subnets across two AZs, IGW, NAT, route tables, security groups (ALB, API/worker, RDS, Redis, EFS) |
| `ecr.tf` | one image repository, reused by the API, the worker and the migration task |
| `rds.tf` | PostgreSQL 16 with `pgvector`, private subnet group, SSL-enforcing parameter group, backups, credentials in Secrets Manager |
| `elasticache.tf` | Redis replication group, private subnet group, at-rest encryption; no in-transit encryption and therefore no AUTH token (see B4) |
| `efs.tf` | filesystem, access point rooted at `/uploads`, mount targets in the private subnets |
| `secrets.tf` | `JWT_SECRET`, `HF_TOKEN`, `DATABASE_URL`, `REDIS_URL` (+ the values for the two data services) |
| `ecs.tf` | cluster, `api`/`worker`/`migrate` task definitions, the two services, autoscaling, IAM roles |
| `alb.tf` | ALB, target group on `/health/live`, HTTPS listener, optional Route53 record |
| `frontend.tf` | private S3 bucket, CloudFront distribution with OAC, SPA fallback (403/404 → `/index.html`) |
| `observability.tf` | log groups with retention, CPU/memory and 5xx alarms, optional SNS email |
| `outputs.tf` | ECR URL, ALB DNS name, bucket name, CloudFront domain, cluster/service names, secret ARNs |

### B1 — Apply the infrastructure

```bash
cd infra/aws
cp terraform.tfvars.example terraform.tfvars     # then edit: region, project, image tag, domain/cert, sizes
terraform init
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
terraform output                                  # ECR URL, ALB DNS name, bucket, CloudFront domain
```

Expect the first `apply` to take 10–15 minutes: RDS and ElastiCache are the slow resources. `terraform
validate` and `plan` are the two commands to run on every change; the state backend is commented out by
default, so **enable `versions.tf`'s S3 backend before sharing this with anyone else** (an unshared local
state file cannot be used by a CI runner).

Domain and certificate are optional: set `domain_name` / `frontend_domain_name` and
`certificate_arn` (an ACM certificate in the same region as the ALB, **us-east-1** for CloudFront) to get
Route53 records created; leave them empty and use the ALB DNS name and the CloudFront domain for testing.

### B2 — Build and push the image

One image serves the API, the worker and the migration task; only the command differs. Fargate runs
`linux/amd64` by default (the Terraform default matches), so build accordingly even from an Apple Silicon
or ARM workstation.

```bash
REGION=$(terraform -chdir=infra/aws output -raw region)
IMAGE=$(terraform -chdir=infra/aws output -raw ecr_repository_url)
TAG=$(git rev-parse --short HEAD)

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${IMAGE%%/*}"

# context is the repository root: the Dockerfile COPYs backend/, migrations/, prompts/ and scripts/
docker buildx build --platform linux/amd64 \
  -f docker/backend.Dockerfile -t "$IMAGE:$TAG" --push .
```

`.dockerignore` keeps the context small (no `.venv`, `node_modules`, `uploads/`, `tmp/`, logs or
notebooks), which matters more here than on Render because every ECS deploy pulls the image over the
network. Set `image_tag` in `terraform.tfvars` (or pass `-var image_tag=$TAG`) and re-apply so the task
definitions point at the new image — this is exactly what `.github/workflows/deploy-aws.yml` automates in
B10.

### B3 — The database, and the one privilege that matters

Terraform creates RDS for PostgreSQL 16 in private subnets, encrypted, with a multi-AZ-capable subnet
group, automated backups, and the generated master password in Secrets Manager. RDS ships **pgvector**
(0.8.x on current 15/16/17 engines), which revision `0001` enables with
`CREATE EXTENSION IF NOT EXISTS vector`.

Two consequences worth stating plainly:

- **`pgvector` is not a trusted extension on RDS.** `CREATE EXTENSION` therefore requires `rds_superuser`
  membership, which only the master user has — so `DATABASE_URL` uses the RDS master user
  (`db_username`, default `rag`). If you want a least-privilege application user later, run the extension
  once as the master and grant the app user access:
  ```sql
  CREATE EXTENSION IF NOT EXISTS vector;         -- as the master user, once
  GRANT USAGE ON SCHEMA public TO app_user;
  ```
- **RDS for PostgreSQL 15+ enforces SSL by default** (`rds.force_ssl=1` in the default parameter group).
  Terraform composes `DATABASE_URL` with `?ssl=require`, which is what asyncpg needs: it encrypts the
  connection without requiring a CA bundle on the client.

Sizing: `db.t4g.micro` carries the metadata, chunks and embeddings of a small corpus. Watch connections —
`app/database/session.py` uses `pool_size=10` with `max_overflow=20`, and a micro instance allows far
fewer than 30 concurrent connections per task, so keep `api_desired_count` and `worker_desired_count` at 1
until you raise the instance class.

### B4 — ElastiCache

A single-node Redis replication group in private subnets, at-rest encryption on, and a security group
reachable only from the ECS task security group. `REDIS_URL` is composed as
`redis://<primary-endpoint>:6379/0` and serves three roles at once: the Celery broker, the Celery result
backend and the rate-limit/conversation-memory store.

**In-transit encryption and AUTH are off together, and that pairing is not optional.** ElastiCache only
accepts an AUTH token when TLS in transit is enabled, and the application builds its client with
`Redis.from_url(settings.redis_url)`, where redis-py 5 defaults `ssl_cert_reqs` to `required` — so a
`rediss://` URL fails certificate verification against ElastiCache's own CA unless `app/dependencies.py`
and the Celery broker URL are changed to pass the Amazon trust bundle via `ssl_ca_certs`. Until that code
change is made, network isolation (private subnet group plus the task security group) is the control that
protects Redis. Change the code first, then set `transit_encryption_enabled = true` and an `auth_token`
in `elasticache.tf` and switch `REDIS_URL` to `rediss://:<token>@<endpoint>:6379/0`.

### B5 — EFS: the shared `uploads/` path

This is the piece that makes the split API/worker topology work at all. Terraform creates a filesystem,
mount targets in each private subnet, and an access point whose root directory is `/uploads`; both task
definitions mount it at **`/app/uploads`**, so the API's
`storage_path = /app/uploads/<tenant>/<uuid>-<name>` exists verbatim for the worker.

- `efs.tf` grants the execution role (and the task role, belt and braces)
  `elasticfilesystem:ClientMount`, `ClientWrite` and `ClientRootAccess` — Fargate authorizes EFS access
  points through IAM, and a missing action shows up as a task that cannot start with
  `ResourceInitializationError`.
- The image runs as the non-root user `app`. The access point is created owned by `0:0` with permissions
  `0777` inside the access point root, which is the usual Fargate + non-root fix. To tighten it, find the
  image's UID (`docker run --rm --entrypoint id <image> app`) and set `efs_posix_uid` / `efs_posix_gid` /
  `efs_root_permissions` in `terraform.tfvars`.
- A local `write_bytes()` now goes to a network filesystem. Uploads are small and infrequent, so this is
  acceptable; ingestion reads and re-reads are sequential, not random.

Alternatives, and why they are worse here: a single Fargate task running both processes (works, but drops
the independent scaling and the per-service log group), or object storage (the correct long-term answer,
and a code change — see the Render document's "Uploaded files").

### B6 — Configuration and secrets

Terraform splits configuration into two places, and the split is deliberate: anything secret lives in
Secrets Manager and is injected with the task definition's `secrets` block (`valueFrom`), everything else
is plain `environment` in the same definition, so it is reviewable in the console and changeable without a
secret rotation.

| Secrets Manager (injected, never in tfvars except `hf_token`) | Container `environment` |
| --- | --- |
| `JWT_SECRET` (generated, ≥ 32 chars) | `APP_ENV=production`, `AUTHENTICATION_ENABLED=true`, `API_PREFIX=/api/v1`, `LOG_LEVEL=INFO`, `LOG_USER_CONTENT=false`, `PORT=8000` |
| `HF_TOKEN` (from `terraform.tfvars`, marked sensitive) | `LLM_PROVIDER=huggingface`, `HF_API_MODE=openai`, `HF_INFERENCE_URL`, `HF_MODEL`, `LLM_DRAFT_MODEL`, `LLM_FAST_MODEL`, `LLM_TIMEOUT_SECONDS=60` |
| `DATABASE_URL` (composed, `?ssl=require`) | `EMBEDDING_PROVIDER=huggingface`, `EMBEDDING_INFERENCE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION` |
| `REDIS_URL` (composed from the replication group's primary endpoint) | `RERANK_PROVIDER=disabled`, `RERANK_INFERENCE_URL` (empty or hosted), `RERANK_MODEL`, `RERANK_TOP_K=8` |
| `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` (used once, then removable) | `STT_PROVIDER=disabled`, `TTS_PROVIDER=disabled`, `OCR_PROVIDER=disabled` |
| | `FRONTEND_ORIGINS=https://<CloudFront domain or custom domain>` |
| | `RAG_VECTOR_TOP_K=20`, `RAG_LEXICAL_TOP_K=20`, `RAG_MIN_SCORE=0.35`, `RAG_MAX_CONTEXT_CHARS=16000`, `RAG_VERIFY_ENABLED=true` |
| | `RAG_MIN_EVIDENCE=1`, `PLANNER_LLM_MIN_WORDS=6`, `CHUNK_TARGET_CHARS=1600`, `CHUNK_MAX_CHARS=2400`, `CHUNK_OVERLAP_CHARS=200` |
| | `LONG_TERM_MEMORY_ENABLED=true`, `LONG_TERM_MEMORY_INLINE=false`, `MEMORY_TTL_SECONDS=86400` |
| | `MAX_UPLOAD_SIZE_MB=25`, `RATE_LIMIT_PER_MINUTE=30`, `URL_INGESTION_ENABLED=false`, `KEEP_MODEL_WARM=false` |

Notes that save an hour of debugging:

- `FRONTEND_ORIGINS` must be the **browser's** origin — the CloudFront domain
  (`https://dxxxxx.cloudfront.net`) or your custom domain — exactly, with scheme and no trailing slash.
  A wrong value produces a CORS error in the browser while the ALB access logs show a healthy `200`.
- `PORT=8000` is pinned for the same reason as on Render: the container, the Dockerfile `EXPOSE` and the
  target group must agree. Fargate does not require `$PORT`.
- `VITE_*` variables never appear here. They are compiled into the frontend bundle in B9.
- Rotating `HF_TOKEN` or `JWT_SECRET` means publishing a new secret version and forcing a new deployment
  (`aws ecs update-service --force-new-deployment`) — tasks read secrets at start.

### B7 — ECS services, and the ALB settings that matter

Both services run the same image with different commands:

| | API service (`<project>-<environment>-api`) | worker service (`<project>-<environment>-worker`) |
| --- | --- | --- |
| Command | the image default: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers` | `celery -A app.workers.celery_app:celery_app worker --loglevel=INFO --concurrency=2` |
| Port | 8000, registered in the `<project>-<environment>` target group | none — it must never be registered anywhere |
| Behind the ALB | yes, `/health/live`, matcher `200` | no |
| Scaling | target tracking on average CPU (60%) between `api_min_capacity` and `api_max_capacity` | manual, or a queue-depth metric; CPU is a poor proxy for "jobs waiting" |
| Health | `healthCheckPath` plus the ECS deployment circuit breaker, which rolls the service back automatically when the new tasks never become healthy | the container's own exit code |

ALB and listener settings that matter for this application:

- **`idle_timeout = 300`.** `/chat/stream` is server-sent events and a verified answer is several
  sequential model calls; the ALB default of 60 s can drop a connection that goes quiet between events.
  The maximum is 4000 s.
- **Target group** health check path `/health/live` (liveness only). `/health/ready` additionally pings
  Postgres and Redis, so using it as the target-group check turns a Redis blip into a failed deployment.
- **HTTPS** on 443 with the ACM certificate, and an HTTP listener that redirects to 443. The app already
  runs `--proxy-headers`, so client IPs (and therefore the rate limiter) see the real caller.
- **Rolling deploys**: `minimum_healthy_percent = 100`, `maximum_percent = 200` with the circuit breaker
  gives a replacement without downtime; the migration runs before the service update (B8), never inside
  container startup.
- Useful extras, all in `alb.tf`/`observability.tf`: ALB access logs to S3, the `5xx` alarm, and
  `deregistration_delay` kept low (15–30 s) so a replaced task stops receiving requests quickly.

### B8 — Migrate the schema and bootstrap the admin (one-off tasks)

Migrations are a task, not a container start-up step, so a rollback and a re-deploy never race the schema.
The `migrate` task definition is the same image with `alembic -c alembic.ini upgrade head` as its command.

```bash
cd infra/aws
CLUSTER=$(terraform output -raw cluster_name)
MIGRATE_TD=$(terraform output -raw migrate_task_definition)
SUBNETS=$(terraform output -raw private_subnet_ids)          # "subnet-a,subnet-b"
TASK_SG=$(terraform output -raw task_security_group_id)

run_task() {  # $1 = overrides JSON (optional)
  aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
    --task-definition "$MIGRATE_TD" \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$TASK_SG],assignPublicIp=DISABLED}" \
    --overrides "${1:-'{}'}" --query 'tasks[0].taskArn' --output text
}

MIGRATE_ARN=$(run_task)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$MIGRATE_ARN"
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$MIGRATE_ARN" \
  --query 'tasks[0].{exit:containers[0].exitCode,reason:stoppedReason}' --output json

# First administrator (idempotent; BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD
# come from Secrets Manager into the migrate task definition)
BOOTSTRAP_ARN=$(run_task '{"containerOverrides":[{"name":"migrate","command":["python","scripts/bootstrap_admin.py"]}]}')
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$BOOTSTRAP_ARN"
```

Two failure modes to expect on the first run: `exitCode: 1` with a database error means the task cannot
reach RDS (private subnets need the NAT gateway, or VPC endpoints, to pull the image and read secrets); a
`ResourceInitializationError` means the execution role is missing a permission (ECR pull, Secrets Manager
read, or the EFS client actions). The migration's `CREATE EXTENSION vector` failing with
`permission denied to create extension` means the database URL is not the RDS master user — see B3.

### B9 — The frontend: build, upload, invalidate

The UI is a static bundle; CloudFront serves it from a private S3 bucket through an origin access control,
with a distribution-level custom error response mapping `403`/`404` to `/index.html` (200) so client-side
routes resolve. `VITE_API_BASE_URL` is compiled in, so it is set here, not in ECS.

```bash
cd frontend
VITE_AUTH_ENABLED=true VITE_API_BASE_URL="https://api.example.com/api/v1" npm ci && npm run build

BUCKET=$(terraform -chdir=../infra/aws output -raw frontend_bucket)
DIST_ID=$(terraform -chdir=../infra/aws output -raw cloudfront_distribution_id)

# hashed assets are immutable; index.html must never be cached
aws s3 sync dist "s3://$BUCKET" --delete \
  --exclude "index.html" --cache-control "public,max-age=31536000,immutable"
aws s3 cp dist/index.html "s3://$BUCKET/index.html" --cache-control "no-cache"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*"
```

`VITE_API_BASE_URL` must be an **HTTPS origin with a valid certificate**, i.e. the API's custom domain in
front of the ALB (`domain_name` in `terraform.tfvars` + an ACM certificate), not the raw ALB DNS name —
the browser would reject the certificate on `https://<alb-dns>/api/v1`. Remember to put that same origin
in `FRONTEND_ORIGINS` (B6), and to keep the UI and API on the same *site* only if CORS is configured:
cross-origin here is expected and supported.

Optional simplification: add an `/api/*` behavior to the CloudFront distribution that forwards to the
ALB. The UI then calls its own origin (no CORS at all, one certificate). If you do that, disable caching
for that behavior *and* verify `/chat/stream` still streams — server-sent events through a CDN can be
buffered, which is why it is not the default in `frontend.tf`.

### B10 — Automate it: `.github/workflows/deploy-aws.yml`

The workflow does exactly the steps above, in the same order, so a human can reproduce any failure by hand.
It assumes **OIDC**, not long-lived keys: create an IAM OIDC provider for
`token.actions.githubusercontent.com` and a role whose trust policy is scoped to this repository and
branch, then store its ARN as the `AWS_ROLE_ARN` secret. Every id the workflow needs is printed by
`terraform output` in `infra/aws`.

| Repository configuration | Kind | Used for |
| --- | --- | --- |
| `AWS_ROLE_ARN` | secret | `aws-actions/configure-aws-credentials` role assumption |
| `AWS_REGION` | variable | every `aws` call and the ECR login |
| `ECR_REPOSITORY`, `ECS_CLUSTER_NAME`, `ECS_API_SERVICE`, `ECS_WORKER_SERVICE`, `ECS_MIGRATE_TASK_DEFINITION` | variables | image push, `run-task`, `update-service` |
| `PRIVATE_SUBNET_IDS`, `TASK_SECURITY_GROUP_ID` | variables | the `--network-configuration` of the migration task |
| `FRONTEND_BUCKET`, `CLOUDFRONT_DISTRIBUTION_ID` | variables | `s3 sync` and cache invalidation |
| `VITE_API_BASE_URL`, `VITE_AUTH_ENABLED` | variables | the frontend build |

Note what the workflow deliberately does **not** do: run `terraform apply`. It registers a new task
definition revision from the live one with the new image tag, so CI needs no Terraform state, no
`terraform.tfvars` (which is gitignored and holds the provider token), and cannot drift anything except
the image. Run `terraform apply -var image_tag=<sha>` when you want the Terraform state to record the
deployed image too, and remember that the next `apply` without that variable resets the tasks to
`var.image_tag`'s value — set that variable to the release you want to keep.

Pipeline order, which is the part that matters:

1. `docker buildx build --push` for `linux/amd64`, tagged with the commit SHA (and `latest`), with a
   registry cache.
2. `aws ecs describe-task-definition` → `register-task-definition` for the `api`, `worker` and `migrate`
   families with the new image.
3. `aws ecs run-task` for the migration, `aws ecs wait tasks-stopped`, and a non-zero container exit code
   **fails the workflow** — the schema change must never be skipped silently.
4. `aws ecs update-service` on the API and the worker, then `aws ecs wait services-stable`.
5. Frontend: `npm ci && npm run build` with the `VITE_*` variables, `aws s3 sync`, CloudFront
   invalidation.

### B11 — Deploying by hand and rolling back

```bash
cd infra/aws
CLUSTER=$(terraform output -raw cluster_name)
API_SERVICE=$(terraform output -raw api_service_name)
WORKER_SERVICE=$(terraform output -raw worker_service_name)

# 1. build and push the image with a new tag (B2)
# 2. register the new revisions and roll both services: each service points at a task
#    definition revision, so this apply *is* the deploy
terraform apply -var "image_tag=$(git rev-parse --short HEAD)"

# 3. run the migration task (B8) and confirm exit code 0

# 4. confirm both services settled
aws ecs wait services-stable --cluster "$CLUSTER" --services "$API_SERVICE" "$WORKER_SERVICE"
```

Rollback is a previous task-definition revision, and nothing else:

```bash
FAMILY=$(terraform output -raw api_task_definition)
aws ecs list-task-definitions --family-prefix "$FAMILY" --status ACTIVE \
  --query 'taskDefinitionArns[-5:]' --output table          # pick the revision to go back to

aws ecs update-service --cluster "$CLUSTER" --service "$API_SERVICE" \
  --task-definition "${FAMILY}:2"                            # family:revision
```

Two things to keep in mind. The schema stays where it is — both migrations present are additive, which is
what makes this safe. And the next `terraform apply` re-points the service at the revision Terraform
manages, so if CI registered newer revisions after that rollback, `apply` will move the service forward
again; `var.image_tag` is what decides where.

The deployment circuit breaker means a release whose tasks never pass `/health/live` rolls back on its own.

## Verification checklist (both tracks)

`$API` is `https://rag.example.com` (Option A, through Caddy) or `https://api.example.com` / the ALB DNS
name over HTTP for testing (Option B).

```bash
# 1. Process and dependencies
curl -sS "$API/health/live"      # {"status":"ok"}
curl -sS "$API/health/ready"     # {"status":"ready","checks":{"database":true,"redis":true}}

# 2. Sign in (the same route the UI uses)
TOKEN=$(curl -sS -X POST "$API/api/v1/auth/token" -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"<password>"}' \
  | python -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
curl -sS "$API/api/v1/auth/me" -H "Authorization: Bearer $TOKEN"

# 3. Ingest one source (multipart; the response is a job, not a document)
curl -sS -X POST "$API/api/v1/documents" -H "Authorization: Bearer $TOKEN" \
  -F "file=@Mansam_Booklet_Spreads-EN.pdf" -F "access_scope=tenant" -F "category=catalogue"

# 4. Watch the worker: queued -> processing (extracting/chunking/embedding) -> completed
curl -sS "$API/api/v1/documents/jobs/<job_id>" -H "Authorization: Bearer $TOKEN"

# 5. Ask a question only that source can answer
curl -sS -X POST "$API/api/v1/chat" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"How many boutiques does Mansam have in KSA?"}'

# 6. Re-upload the same file: the second call must answer 409
```

Pass criteria: `grounded: true` with citations whose excerpts come from the uploaded file,
`verification_status` `verified` (or `verification_skipped` with `RAG_VERIFY_ENABLED=false`), an honest
uncertainty answer for a question the corpus cannot answer, a follow-up in the same `conversation_id`
resolving a pronoun, and `409` on the duplicate. Then open the UI, sign in, send a message, expand
**Sources**, and match the answer's request id against the API log line.

**On Option B, step 3 is the EFS test.** If the API accepts the upload and the worker never reports
`processing`, the two tasks are not seeing the same filesystem — check the mount in both task definitions
and the `elasticfilesystem:*` permissions on the roles. This is the failure this architecture exists to
avoid.

## Cost: what actually drives the bill on Option B

Check the AWS pricing page for current rates; the point of this table is which knobs move the number.

| Resource | Default in `terraform.tfvars` | What changes the cost |
| --- | --- | --- |
| Fargate — API | 0.5 vCPU / 1 GB, 1 task | tasks × hours; autoscaling raises it under load |
| Fargate — worker | 0.5 vCPU / 1 GB, 1 task | concurrency, and how long ingestion batches run |
| RDS PostgreSQL | `db.t4g.micro`, 20 GB gp3 | instance class and storage; a multi-AZ deployment roughly doubles it |
| ElastiCache | `cache.t4g.micro` | node count and node type |
| ALB | 1 ALB + 1 target group | fixed hourly plus LCU (requests/bytes/connections) |
| NAT Gateway | one | hourly plus **per GB processed** — the quiet cost driver, because every image pull and model call from a private subnet goes through it |
| EFS | one filesystem | storage plus throughput; small here because only source files live in it |
| CloudFront + S3 | one distribution | requests and outbound data transfer |
| Secrets Manager | 6 secrets | per secret per month, plus API calls |
| CloudWatch Logs | 30-day retention | ingestion volume; set retention, or logs become the second-largest line item |

Two easy savings: run the worker on `FARGATE_SPOT` capacity (ingestion retries via Celery's `acks_late`
and `max_retries`, so an interrupted task is re-run) and avoid a NAT gateway by adding VPC endpoints for
ECR, S3, Secrets Manager and CloudWatch Logs.

## Troubleshooting (Option B)

| Symptom | Cause and fix |
| --- | --- |
| Task fails to start with `ResourceInitializationError` | the execution role cannot pull from ECR, read a secret, or mount EFS — check `ecs.tf`'s role policies and the EFS `ClientMount`/`ClientWrite` actions |
| Tasks run but the target group reports `unhealthy` | nothing is listening on port 8000 in the task, or `/health/live` is not reachable from the ALB security group; check the task's log stream, not the ALB |
| `502` immediately after a deployment | the new tasks are not registered yet: watch `aws ecs wait services-stable`; a failed health check with the circuit breaker on will roll back by itself |
| `504` on `/chat/stream` | the ALB idle timeout was hit between stream events; raise `idle_timeout` (max 4000 s) or break long silences. Verify `RAG_VERIFY_ENABLED` is what you expect — it roughly doubles the answer time |
| Upload `202` but the job stays `queued` | the worker service has no running tasks, or its log group shows a broker connection error. Confirm the worker task's `REDIS_URL` points at the replication group's primary endpoint and not at an endpoint from a previous deployment |
| Upload `202`, job fails with `missing_source` | the worker's EFS mount is missing or points somewhere else than the API's |
| Migration task exits non-zero with `permission denied to create extension "vector"` | `DATABASE_URL` is not the RDS master user (B3) |
| Migration task exits non-zero with a connection error | the private subnets have no route to RDS/ECR/Secrets Manager (no NAT gateway and no VPC endpoints), or the task security group is not allowed on 5432/6379 |
| Celery log: `KeyError: 'ingestion.index_document'` | the task module was not loaded; `celery_app.py` declares `include=["app.workers.ingestion"]` — check that the worker command points at this package and not a locally modified one |
| Browser CORS error while the API returns `200` | `FRONTEND_ORIGINS` does not match the UI origin exactly (CloudFront domain vs custom domain, scheme, trailing slash), or the tasks have not been redeployed since it changed |
| Frontend loads but every call fails | `VITE_API_BASE_URL` was compiled with the wrong origin: rebuild and re-sync the bundle, then invalidate CloudFront |

## Where each local service goes

| `docker-compose.yml` | Option A (EC2) | Option B (ECS) |
| --- | --- | --- |
| `postgres` | the same container, unpublished | RDS for PostgreSQL 16 with `pgvector` |
| `redis` | the same container, unpublished | ElastiCache replication group with AUTH |
| `backend` | the same container, behind Caddy | ECS service `<project>-<env>-api` behind the ALB |
| `worker` | the same container | ECS service `<project>-<env>-worker` |
| `frontend` | built on the box, served by Caddy (see A6) | S3 + CloudFront |
| `reranker` | the same container on a `g`-series instance (A8) | a hosted reranker, or a separate GPU EC2 box |
| `speech` | the same container on a CPU or `g`-series instance (A8) | a hosted speech endpoint |
| `uploads/` volume | the bind mount on the instance's EBS volume | the EFS access point at `/app/uploads` |
| `.env` | the server's `.env`, `chmod 600` | Secrets Manager + task-definition `environment` |

Related: [`docs/DEPLOY-RENDER.md`](DEPLOY-RENDER.md) (the same application on Render),
[`infra/aws/README.md`](../infra/aws/README.md) (the Terraform), [`docs/DEPLOYMENT.md`](DEPLOYMENT.md),
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md), [`docs/SECURITY.md`](SECURITY.md),
[`docs/TESTING.md`](TESTING.md) and [`.github/workflows/deploy-aws.yml`](../.github/workflows/deploy-aws.yml).









