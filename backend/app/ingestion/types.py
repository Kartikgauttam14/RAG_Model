from dataclasses import dataclass, field
from typing import Any, Literal

ElementKind = Literal["heading", "paragraph", "table", "list", "code"]


@dataclass(frozen=True)
class ExtractedElement:
    text: str
    kind: ElementKind
    page_number: int | None = None
    section: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedDocument:
    name: str
    media_type: str
    elements: list[ExtractedElement]
    metadata: dict[str, Any] = field(default_factory=dict)
    needs_ocr: bool = False


@dataclass(frozen=True)
class Chunk:
    content: str
    content_hash: str
    chunk_index: int
    section: str | None
    page_number: int | None
    parent_key: str | None
    metadata: dict[str, Any]
