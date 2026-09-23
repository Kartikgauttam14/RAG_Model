import pytest
from app.config import Settings


def test_production_allows_explicitly_disabled_authentication() -> None:
    settings = Settings(
        app_env="production",
        authentication_enabled=False,
        hf_inference_url="https://models.example.test/v1",
        hf_token="x" * 10,
        embedding_inference_url="https://embeddings.example.test/v1",
    )

    settings.validate_runtime()


def test_production_still_requires_model_configuration() -> None:
    settings = Settings(app_env="production", authentication_enabled=False)

    with pytest.raises(RuntimeError, match="Missing production configuration"):
        settings.validate_runtime()


def test_qdrant_backend_requires_url_when_selected() -> None:
    settings = Settings(vector_store_backend="qdrant", qdrant_url=None)

    with pytest.raises(RuntimeError, match="QDRANT_URL"):
        settings.validate_runtime()


def test_qdrant_backend_passes_validation_with_url() -> None:
    settings = Settings(
        app_env="production",
        authentication_enabled=False,
        hf_inference_url="https://models.example.test/v1",
        hf_token="x" * 10,
        embedding_inference_url="https://embeddings.example.test/v1",
        vector_store_backend="qdrant",
        qdrant_url="http://qdrant:6333",
    )

    settings.validate_runtime()
