# Deployment

Two platform guides run this application outside a workstation, both step by step:

- **[Deploying to Render](DEPLOY-RENDER.md)** — the Docker web service (API + Celery worker in one
  container), Render Postgres with `pgvector`, Render Key Value and the static UI, driven by
  [`render.yaml`](../render.yaml).
- **[Deploying to AWS](DEPLOY-AWS.md)** — Option A: one EC2 instance running the repository's Compose
  stack; Option B: ECS Fargate with RDS, ElastiCache, EFS, an ALB and S3 + CloudFront, provisioned by the
  Terraform in [`infra/aws`](../infra/aws) and deployed by
  [`.github/workflows/deploy-aws.yml`](../.github/workflows/deploy-aws.yml).

Set `APP_ENV=production`, a unique 32+ character `JWT_SECRET`, production PostgreSQL/Redis URLs, HTTPS origins, hosted Hugging Face inference URLs and provider tokens. The application refuses production startup if core hosted LLM/embedding configuration is missing.

Apply the migration before deploying API and worker: `alembic -c backend/alembic.ini upgrade head`. Use durable object storage instead of the development `uploads/` volume for multi-instance deployment. Terminate TLS at a managed load balancer or reverse proxy, restrict database/Redis to private networks, and expose `/metrics` only to monitoring.

The Docker CLI and daemon are available on this workstation, and both
`docker/backend.Dockerfile` and `docker/frontend.Dockerfile` have been built here against the committed
`.dockerignore` as part of writing these guides (the build contexts resolved and both images built
cleanly). The Compose stack itself has not been run end to end here.
