import hashlib
import re

from app.ingestion.normalize import normalize_text
from app.ingestion.types import Chunk, ExtractedDocument, ExtractedElement

SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?؟])\s+(?=[A-Z0-9\u0600-\u06ff])")


class StructureAwareChunker:
    def __init__(self, target_chars: int, max_chars: int, overlap_chars: int) -> None:
        if not 0 <= overlap_chars < target_chars <= max_chars:
            raise ValueError("Expected 0 <= overlap < target <= max")
        self.target_chars = target_chars
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars

    def chunk(self, document: ExtractedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        buffer: list[str] = []
        buffer_meta: list[ExtractedElement] = []
        active_section: str | None = None

        def flush() -> None:
            if not buffer:
                return
            content = normalize_text("\n\n".join(buffer))
            if not content:
                buffer.clear()
                buffer_meta.clear()
                return
            first = buffer_meta[0]
            pages = sorted({item.page_number for item in buffer_meta if item.page_number is not None})
            metadata = {
                "element_kinds": sorted({item.kind for item in buffer_meta}),
                "page_numbers": pages,
                "source_name": document.name,
            }
            index = len(chunks)
            chunks.append(
                Chunk(
                    content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    chunk_index=index,
                    section=active_section or first.section,
                    page_number=pages[0] if len(pages) == 1 else None,
                    parent_key=f"{document.name}:{active_section}" if active_section else None,
                    metadata=metadata,
                )
            )
            overlap = self._tail(content, self.overlap_chars)
            buffer.clear()
            buffer_meta.clear()
            if overlap:
                buffer.append(overlap)
                buffer_meta.append(first)

        for element in document.elements:
            if element.kind == "heading":
                flush()
                active_section = element.text
                buffer.append(element.text)
                buffer_meta.append(element)
                continue
            for part in self._split_oversized(element):
                projected = len("\n\n".join([*buffer, part.text]))
                section_changed = bool(buffer and part.section and active_section and part.section != active_section)
                if section_changed or projected > self.target_chars:
                    flush()
                if part.section:
                    active_section = part.section
                buffer.append(part.text)
                buffer_meta.append(part)
                if len("\n\n".join(buffer)) >= self.max_chars:
                    flush()
        flush()
        return [chunk for chunk in chunks if len(chunk.content) >= 20]

    def _split_oversized(self, element: ExtractedElement) -> list[ExtractedElement]:
        if len(element.text) <= self.max_chars:
            return [element]
        if element.kind == "table":
            units = element.text.splitlines()
        else:
            units = SENTENCE_BOUNDARY.split(element.text)
        parts: list[ExtractedElement] = []
        current: list[str] = []
        for unit in units:
            if len(unit) > self.max_chars:
                hard_parts = [unit[i : i + self.max_chars] for i in range(0, len(unit), self.max_chars)]
            else:
                hard_parts = [unit]
            for hard_part in hard_parts:
                if current and len(" ".join([*current, hard_part])) > self.max_chars:
                    parts.append(self._copy_element(element, " ".join(current)))
                    current = []
                current.append(hard_part)
        if current:
            parts.append(self._copy_element(element, " ".join(current)))
        return parts

    @staticmethod
    def _copy_element(element: ExtractedElement, text: str) -> ExtractedElement:
        return ExtractedElement(
            text=text,
            kind=element.kind,
            page_number=element.page_number,
            section=element.section,
            metadata=element.metadata,
        )

    @staticmethod
    def _tail(text: str, size: int) -> str:
        if size <= 0:
            return ""
        tail = text[-size:]
        first_space = tail.find(" ")
        return tail[first_space + 1 :] if first_space >= 0 else tail
