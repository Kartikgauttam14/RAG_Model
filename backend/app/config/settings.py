import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    app_env: Literal["development", "test", "staging", "production"] = "development"
    authentication_enabled: bool = False
    app_name: str = "Mansam RAG"
    api_prefix: str = "/api/v1"
    public_base_url: str = "http://localhost:8000"
    frontend_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    log_level: str = "INFO"
    log_user_content: bool = False

    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "development-only-change-this-secret"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 14
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    llm_provider: str = "huggingface"
    hf_token: str | None = None
    hf_model: str = "meta-llama/llama-3.2-3b-instruct:free"
    hf_inference_url: str | None = None
    hf_api_mode: Literal["openai", "native"] = "openai"
    llm_timeout_seconds: float = 60
    # Optional per-stage model routing. The answer draft is the quality-critical call, so
    # it can use the largest served model, while the planner, verifier and memory
    # extractor run on a smaller one. Both default to HF_MODEL.
    llm_draft_model: str | None = None
    llm_fast_model: str | None = None

    embedding_provider: str = "huggingface"
    embedding_model: str = "intfloat/multilingual-e5-large"
    embedding_inference_url: str | None = None
    embedding_dimension: int = 1024
    embedding_timeout_seconds: float = 45
    embedding_query_prefix: str = "query: "
    embedding_passage_prefix: str = "passage: "

    rerank_provider: str = "huggingface"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_inference_url: str | None = None
    rerank_timeout_seconds: float = 30
    rerank_batch_size: int = Field(32, ge=1, le=256)

    stt_provider: str = "huggingface"
    stt_model: str = "openai/whisper-large-v3-turbo"
    stt_inference_url: str | None = None
    stt_api_key: str | None = None
    stt_min_confidence: float = Field(0.70, ge=0, le=1)
    # Whisper biases decoding toward whatever vocabulary this prompt contains, which is
    # what keeps brand and product names ("Mamlakati", "Qanun") from being transcribed as
    # unrelated words. Only used by the OpenAI-compatible provider.
    stt_prompt: str | None = None

    tts_provider: str = "huggingface"
    tts_model: str = "facebook/mms-tts-eng"
    tts_inference_url: str | None = None
    tts_api_key: str | None = None
    tts_voice: str = "default"
    tts_speed: float = Field(1.0, ge=0.5, le=2)

    ocr_provider: str = "disabled"
    ocr_inference_url: str | None = None
    ocr_api_key: str | None = None

    rag_vector_top_k: int = Field(20, ge=1, le=100)
    rag_lexical_top_k: int = Field(20, ge=1, le=100)
    rerank_top_k: int = Field(8, ge=1, le=30)
    lexical_text_search_config: str = "english"
    # Dense-index backend for the vector arm: "pgvector" (Postgres HNSW, the
    # default) or "qdrant" (dedicated store, Postgres stays system of record).
    vector_store_backend: Literal["pgvector", "qdrant"] = "pgvector"
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_collection: str = "mansam_chunks"
    qdrant_timeout_seconds: float = 10
    qdrant_hnsw_m: int = Field(16, ge=4, le=64)
    qdrant_hnsw_ef_construct: int = Field(128, ge=16, le=512)
    qdrant_full_scan_threshold: int = Field(10000, ge=100, le=1000000)
    qdrant_search_ef: int = Field(64, ge=8, le=512)
    rag_min_score: float = Field(0.35, ge=0, le=1)
    rag_min_evidence: int = Field(1, ge=1, le=10)
    rag_max_context_chars: int = Field(16000, ge=1000, le=100000)
    # Verification is a second full generation over the same evidence. Keeping it on is the
    # strongest guarantee the product offers (an independent re-check plus regeneration), but
    # it roughly doubles the time to an answer, so a deployment with a hard latency budget can
    # turn it off: the admission gate, the model's own `grounded` flag and citation resolution
    # still apply, and the stored status becomes `verification_skipped`.
    rag_verify_enabled: bool = True
    # The planner is an extra generation on the critical path. A short first-turn
    # question carries no pronouns or history to resolve, so the deterministic plan
    # is used for messages below this many words and no generation is spent. Set to
    # 0 to always ask the model, or raise it to trust the model with shorter turns.
    planner_llm_min_words: int = Field(6, ge=0, le=200)
    chunk_target_chars: int = Field(1600, ge=200, le=10000)
    chunk_max_chars: int = Field(2400, ge=300, le=20000)
    chunk_overlap_chars: int = Field(200, ge=0, le=2000)

    max_upload_size_mb: int = Field(25, ge=1, le=500)
    allowed_upload_types: Annotated[list[str], NoDecode] = [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
        "application/json",
    ]
    ingestion_excluded_sheets: Annotated[list[str], NoDecode] = []
    ingestion_workflow_columns: Annotated[list[str], NoDecode] = [
        "status",
        "state",
        "review status",
        "reviewed by",
        "author",
    ]
    ingestion_unpublished_markers: Annotated[list[str], NoDecode] = ["draft", "pending", "tbd", "todo", "wip"]
    url_ingestion_enabled: bool = False
    url_allowed_hosts: Annotated[list[str], NoDecode] = []
    url_ingestion_max_pages: int = Field(25, ge=1, le=500)
    url_ingestion_user_agent: str = "MansamRAG-Ingestion/0.1"
    rate_limit_per_minute: int = Field(30, ge=1, le=10000)
    malware_scanner_command: str | None = None

    memory_ttl_seconds: int = 86400
    long_term_memory_enabled: bool = True
    long_term_memory_min_confidence: float = Field(0.85, ge=0, le=1)
    # Ollama unloads a model after five minutes of inactivity, and neither a per-request
    # `keep_alive` nor the OLLAMA_KEEP_ALIVE variable changes that through its
    # OpenAI-compatible endpoint (both were measured). When this is true the API sends a
    # one-token completion every `keep_warm_interval_seconds` so the weights stay on the GPU
    # and the first question after a pause does not pay a multi-gigabyte reload.
    keep_model_warm: bool = False
    keep_warm_interval_seconds: int = Field(240, ge=30, le=3600)
    # Long-term memory extraction costs one generation plus one embedding call. When
    # this is false the work runs after the response is committed instead of holding
    # the answer, which removes a full generation from every question. Keep it true
    # only where a caller must observe stored memories in the same request.
    long_term_memory_inline: bool = True

    @field_validator(
        "frontend_origins",
        "allowed_upload_types",
        "url_allowed_hosts",
        "ingestion_excluded_sheets",
        "ingestion_workflow_columns",
        "ingestion_unpublished_markers",
        mode="before",
    )
    @classmethod
    def parse_list_env_values(cls, value: object) -> object:
        """Accept both JSON arrays and comma-separated values from environment variables."""
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        return value

    def validate_runtime(self) -> None:
        if self.app_env in {"staging", "production"}:
            required = {
                "HF_INFERENCE_URL": self.hf_inference_url,
                "HF_TOKEN": self.hf_token,
                "EMBEDDING_INFERENCE_URL": self.embedding_inference_url,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise RuntimeError(f"Missing production configuration: {', '.join(missing)}")
            # Warn if using a localhost URL in production (common when copying .env)
            if self.hf_inference_url and any(
                host in self.hf_inference_url.lower() for host in ("localhost", "127.0.0.1")
            ):
                import warnings

                warnings.warn(
                    "HF_INFERENCE_URL points to a localhost address - this will not "
                    "work in production/cloud environments. "
                    "Use a hosted endpoint like https://openrouter.ai/api/v1",
                    stacklevel=2,
                )
        if self.vector_store_backend == "qdrant" and not self.qdrant_url:
            raise RuntimeError("Missing production configuration: QDRANT_URL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
