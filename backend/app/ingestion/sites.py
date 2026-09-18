"""Helpers for ingesting whole websites: HTML pages plus JSON content APIs."""

import re
from urllib.parse import urljoin, urlparse

HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
JSON_CONTENT_TYPES = frozenset({"application/json", "text/json"})
TEXT_CONTENT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv"})
INGESTIBLE_CONTENT_TYPES = HTML_CONTENT_TYPES | JSON_CONTENT_TYPES | TEXT_CONTENT_TYPES

SITEMAP_PATHS = ("sitemap.xml", "sitemap_index.xml")

MEDIA_TYPE_BY_CONTENT_TYPE = {
    "application/json": "application/json",
    "text/json": "application/json",
    "text/plain": "text/plain",
    "text/markdown": "text/markdown",
    "text/csv": "text/csv",
}

EXTENSION_BY_CONTENT_TYPE = {
    "application/json": ".json",
    "text/json": ".json",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
}

SPA_MARKERS = (
    "app-root",
    "ng-version",
    "__next_data__",
    "__nuxt__",
    "data-reactroot",
    'id="root"',
    "id='root'",
)

SPA_EXTRACTION_HINT = (
    "This page is a JavaScript-rendered single-page application, so the downloaded HTML "
    "contains no readable text. Ingest the site's JSON content API instead (for this site: "
    "https://<host>/api/public/productlines, /api/public/collections, /api/public/emotions)."
)

_LOC_PATTERN = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_SCRIPT_STYLE_PATTERN = re.compile(r"<(script|style|template)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")

MIN_SERVER_RENDERED_CHARS = 200


def normalize_site_paths(base_url: str, paths: list[str]) -> list[str]:
    """Resolve site-relative paths against ``base_url``, keeping order and dropping duplicates."""
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in [base_url, *paths]:
        candidate = urljoin(base_url, raw.strip())
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        key = f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path or '/'}"
        if parsed.query:
            key = f"{key}?{parsed.query}"
        if key in seen:
            continue
        seen.add(key)
        resolved.append(candidate)
    return resolved


def parse_sitemap_urls(xml_text: str, base_url: str, limit: int) -> list[str]:
    """Extract same-host page URLs from a sitemap document, capped at ``limit`` entries."""
    host = (urlparse(base_url).hostname or "").lower()
    urls: list[str] = []
    seen: set[str] = set()
    for match in _LOC_PATTERN.finditer(xml_text):
        candidate = match.group(1)
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if parsed.hostname.lower() != host or candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
        if len(urls) >= limit:
            break
    return urls


def detect_spa_shell(html_text: str) -> bool:
    """Return True when the HTML looks like an empty SPA shell that renders content in the browser."""
    lowered = html_text.lower()
    return any(marker in lowered for marker in SPA_MARKERS)


def estimate_visible_text_length(html_text: str) -> int:
    """Approximate the amount of text a reader would see without executing JavaScript."""
    without_code = _SCRIPT_STYLE_PATTERN.sub(" ", html_text)
    text = _TAG_PATTERN.sub(" ", without_code)
    return len(_WHITESPACE_PATTERN.sub(" ", text).strip())


def is_unrendered_spa(html_text: str, min_chars: int = MIN_SERVER_RENDERED_CHARS) -> bool:
    """Return True only when an SPA shell is present *and* it carries no meaningful server-rendered text.

    Server-side rendered pages (for example Angular Universal output) keep their framework markers but
    do contain real text, so they are still ingestible.
    """
    return detect_spa_shell(html_text) and estimate_visible_text_length(html_text) < min_chars


def source_document_name(url: str, content_type: str) -> str:
    """Build a stable, human-readable document name such as ``host-productlines.json``."""
    parsed = urlparse(url)
    host = (parsed.hostname or "site").lower()
    stem = parsed.path.rstrip("/").rsplit("/", 1)[-1] or "index"
    if "." in stem:
        stem = stem.rsplit(".", 1)[0] or "index"
    return f"{host}-{stem}{EXTENSION_BY_CONTENT_TYPE.get(content_type, '.html')}"


def media_type_for(content_type: str) -> str:
    """Map a response content type onto a media type the ingestion extractors support."""
    return MEDIA_TYPE_BY_CONTENT_TYPE.get(content_type, "text/html")


def is_ingestible_content_type(content_type: str) -> bool:
    return content_type in INGESTIBLE_CONTENT_TYPES
