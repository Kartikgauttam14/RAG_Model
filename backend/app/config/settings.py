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
    hf_model: str = "buckets/kartikgauttam14/Spark-X2.5-4B-bucket"
    hf_inference_url: str | None = None
    hf_api_mode: Literal["openai", "native"] = "openai"
    llm_timeout_seconds: float = 60

    embedding_provider: str = "huggingface"
    embedding_model: str = "intfloat/multilingual-e5-large"
    embedding_inference_url: str | None = None
    embedding_dimension: int = 1024
    embedding_timeout_seconds: float = 45

    rerank_provider: str = "huggingface"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_inference_url: str | None = None
    rerank_timeout_seconds: float = 30

    stt_provider: str = "huggingface"
    stt_model: str = "openai/whisper-large-v3-turbo"
    stt_inference_url: str | None = None
    stt_api_key: str | None = None
    stt_min_confidence: float = Field(0.70, ge=0, le=1)

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
    rag_min_score: float = Field(0.35, ge=0, le=1)
    rag_min_evidence: int = Field(1, ge=1, le=10)
    rag_max_context_chars: int = Field(16000, ge=1000, le=100000)
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
    url_ingestion_enabled: bool = False
    url_allowed_hosts: Annotated[list[str], NoDecode] = []
    url_ingestion_max_pages: int = Field(25, ge=1, le=500)
    url_ingestion_user_agent: str = "MansamRAG-Ingestion/0.1"
    rate_limit_per_minute: int = Field(30, ge=1, le=10000)
    malware_scanner_command: str | None = None

    memory_ttl_seconds: int = 86400
    long_term_memory_enabled: bool = True
    long_term_memory_min_confidence: float = Field(0.85, ge=0, le=1)

    @field_validator("frontend_origins", "allowed_upload_types", "url_allowed_hosts", mode="before")
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
        if self.app_env in {"staging", "production"} and not self.authentication_enabled:
            raise RuntimeError("AUTHENTICATION_ENABLED must be true outside development")
        if self.app_env in {"staging", "production"}:
            required = {
                "HF_INFERENCE_URL": self.hf_inference_url,
                "HF_TOKEN": self.hf_token,
                "EMBEDDING_INFERENCE_URL": self.embedding_inference_url,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise RuntimeError(f"Missing production configuration: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
