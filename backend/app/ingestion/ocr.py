from typing import Protocol

import httpx

from app.config import Settings
from app.ingestion.types import ExtractedDocument, ExtractedElement


class OCRProvider(Protocol):
    async def extract_pdf(self, data: bytes, name: str) -> ExtractedDocument: ...


class OCRUnavailableError(RuntimeError):
    pass


class HTTPCloudOCRProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.ocr_inference_url:
            raise ValueError("OCR_INFERENCE_URL is required")
        self.settings = settings
        self.endpoint = settings.ocr_inference_url
        self.client = client or httpx.AsyncClient(timeout=180)

    async def extract_pdf(self, data: bytes, name: str) -> ExtractedDocument:
        headers = {"Authorization": f"Bearer {self.settings.ocr_api_key}"} if self.settings.ocr_api_key else {}
        try:
            response = await self.client.post(
                self.endpoint,
                headers=headers,
                files={"file": (name, data, "application/pdf")},
            )
            response.raise_for_status()
            payload = response.json()
            pages = payload.get("pages", [])
            elements = [
                ExtractedElement(
                    text=str(page["text"]),
                    kind="paragraph",
                    page_number=int(page.get("page_number", index + 1)),
                )
                for index, page in enumerate(pages)
                if str(page.get("text", "")).strip()
            ]
            if not elements:
                raise OCRUnavailableError("OCR returned no usable text")
            return ExtractedDocument(
                name=name,
                media_type="application/pdf",
                elements=elements,
                metadata={"ocr_provider": "http_cloud", "page_count": len(pages)},
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, OCRUnavailableError):
                raise
            raise OCRUnavailableError("OCR provider is unavailable") from exc
