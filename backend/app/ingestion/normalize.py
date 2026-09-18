import re
import unicodedata
from collections import Counter

from app.ingestion.types import ExtractedDocument, ExtractedElement

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HORIZONTAL_SPACE = re.compile(r"[ \t]+")
EXCESS_BLANKS = re.compile(r"\n{3,}")
BROKEN_HYPHEN = re.compile(r"(?<=\w)-\n(?=\w)")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = CONTROL_CHARS.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = BROKEN_HYPHEN.sub("", text)
    lines = [HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    return EXCESS_BLANKS.sub("\n\n", "\n".join(lines)).strip()


def remove_repeated_page_furniture(document: ExtractedDocument) -> ExtractedDocument:
    page_lines: dict[int, list[str]] = {}
    for element in document.elements:
        if element.page_number is None:
            continue
        lines = [line for line in normalize_text(element.text).splitlines() if line]
        page_lines.setdefault(element.page_number, []).extend(lines)
    if len(page_lines) < 4:
        return document
    candidates: Counter[str] = Counter()
    for lines in page_lines.values():
        for line in set(lines[:2] + lines[-2:]):
            if 2 <= len(line) <= 160:
                candidates[line] += 1
    threshold = max(3, round(len(page_lines) * 0.6))
    repeated = {line for line, count in candidates.items() if count >= threshold}
    if not repeated:
        return document
    cleaned: list[ExtractedElement] = []
    for element in document.elements:
        lines = [line for line in normalize_text(element.text).splitlines() if line not in repeated]
        text = "\n".join(lines).strip()
        if text:
            cleaned.append(
                ExtractedElement(
                    text=text,
                    kind=element.kind,
                    page_number=element.page_number,
                    section=element.section,
                    metadata=element.metadata,
                )
            )
    return ExtractedDocument(
        name=document.name,
        media_type=document.media_type,
        elements=cleaned,
        metadata={**document.metadata, "removed_repeated_lines": sorted(repeated)},
        needs_ocr=document.needs_ocr,
    )
