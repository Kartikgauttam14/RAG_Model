# Deployment

Set `APP_ENV=production`, a unique 32+ character `JWT_SECRET`, production PostgreSQL/Redis URLs, HTTPS origins, hosted Hugging Face inference URLs and provider tokens. The application refuses production startup if core hosted LLM/embedding configuration is missing.

Apply the migration before deploying API and worker: `alembic -c backend/alembic.ini upgrade head`. Use durable object storage instead of the development `uploads/` volume for multi-instance deployment. Terminate TLS at a managed load balancer or reverse proxy, restrict database/Redis to private networks, and expose `/metrics` only to monitoring.

Docker is required for the Compose path. This workstation currently has no Docker executable, so Compose deployment has not been run here.
