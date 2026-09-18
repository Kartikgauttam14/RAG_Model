import hashlib
import re
from pathlib import Path

SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(filename: str) -> str:
    cleaned = SAFE_FILENAME_PATTERN.sub("_", Path(filename).name).strip("._")
    if not cleaned:
        raise ValueError("Filename contains no usable characters")
    return cleaned[:200]


def validate_upload(*, data: bytes, filename: str, media_type: str, allowed_types: list[str], max_size_mb: int) -> None:
    if not data:
        raise ValueError("Upload is empty")
    if len(data) > max_size_mb * 1024 * 1024:
        raise ValueError(f"Upload exceeds the {max_size_mb} MB limit")
    if media_type not in allowed_types:
        raise ValueError(f"Unsupported media type: {media_type}")
    suffix = Path(filename).suffix.lower()
    expected = {
        "application/pdf": {".pdf"},
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
        "text/plain": {".txt"},
        "text/markdown": {".md", ".markdown"},
        "text/html": {".html", ".htm"},
        "text/csv": {".csv"},
        "application/json": {".json"},
    }
    if suffix not in expected.get(media_type, set()):
        raise ValueError("File extension does not match declared media type")
    if media_type == "application/pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("File does not have a valid PDF signature")
    if media_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    } and not data.startswith(b"PK"):
        raise ValueError("File does not have a valid Office container signature")
