FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY backend/pyproject.toml ./pyproject.toml
RUN python -m pip install --upgrade pip==25.0.1 && \
    python -m pip wheel --wheel-dir=/wheels ".[dev]"

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --system app && useradd --system --gid app --create-home app
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY backend/app ./app
COPY migrations ./migrations
COPY backend/alembic.ini ./alembic.ini
COPY prompts ./prompts
# `scripts/bootstrap_admin.py` creates the first administrator. It runs as a
# one-off job (Render) or a one-off task (ECS) against the same image, so the
# repository's scripts directory has to be inside the image as well.
COPY scripts ./scripts
RUN mkdir -p /app/uploads && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

