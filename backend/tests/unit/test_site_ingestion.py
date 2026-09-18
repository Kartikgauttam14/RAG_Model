import os
import subprocess
import sys
from pathlib import Path

from app.config import Settings
from app.ingestion.sites import (
    INGESTIBLE_CONTENT_TYPES,
    SPA_EXTRACTION_HINT,
    detect_spa_shell,
    estimate_visible_text_length,
    is_ingestible_content_type,
    is_unrendered_spa,
    media_type_for,
    normalize_site_paths,
    parse_sitemap_urls,
    source_document_name,
)
from app.workers.celery_app import celery_app
from app.workers.ingestion import index_document

MANSAM_BASE = "https://uatuae.mansamworld.com/home"
BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_celery_worker_registers_the_ingestion_task() -> None:
    """Regression: workers must register ingestion.index_document, or every job fails with KeyError.

    The worker only imports ``app.workers.celery_app``; the task module must therefore be loaded
    through the Celery ``include`` configuration, so this check runs in a fresh interpreter.
    """
    probe = (
        "from app.workers.celery_app import celery_app;"
        "celery_app.loader.import_default_modules();"
        "print(sorted(n for n in celery_app.tasks if n.startswith('ingestion.')))"
    )
    env = {**os.environ, "PYTHONPATH": str(BACKEND_ROOT)}

    # The probe is a fixed, trusted constant, so the subprocess call carries no injection risk.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("['ingestion.index_document']"), result.stdout
    assert "app.workers.ingestion" in celery_app.conf.include
    assert index_document.name == "ingestion.index_document"


def test_settings_accept_csv_and_json_list_env_values() -> None:
    csv_settings = Settings(
        _env_file=None,
        url_allowed_hosts="uatuae.mansamworld.com, www.mansamworld.com ,",
    )
    json_settings = Settings(
        _env_file=None,
        url_allowed_hosts='["uatuae.mansamworld.com","www.mansamworld.com"]',
        frontend_origins='["http://localhost:5173"]',
    )

    assert csv_settings.url_allowed_hosts == ["uatuae.mansamworld.com", "www.mansamworld.com"]
    assert json_settings.url_allowed_hosts == ["uatuae.mansamworld.com", "www.mansamworld.com"]
    assert json_settings.frontend_origins == ["http://localhost:5173"]
    assert Settings(_env_file=None, url_allowed_hosts="").url_allowed_hosts == []


def test_normalize_site_paths_resolves_base_and_deduplicates() -> None:
    urls = normalize_site_paths(
        MANSAM_BASE,
        [
            "api/public/productlines",
            "/api/public/productlines",
            "api/public/collections",
            "   ",
            "javascript:alert(1)",
        ],
    )

    assert urls == [
        MANSAM_BASE,
        "https://uatuae.mansamworld.com/api/public/productlines",
        "https://uatuae.mansamworld.com/api/public/collections",
    ]


def test_normalize_site_paths_keeps_query_strings_distinct() -> None:
    urls = normalize_site_paths(
        MANSAM_BASE,
        ["api/public/products/?productLineId=1", "api/public/products/?productLineId=2"],
    )

    assert urls[-2] == "https://uatuae.mansamworld.com/api/public/products/?productLineId=1"
    assert urls[-1] == "https://uatuae.mansamworld.com/api/public/products/?productLineId=2"


def test_parse_sitemap_urls_keeps_same_host_and_applies_limit() -> None:
    sitemap = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://uatuae.mansamworld.com/home</loc></url>
      <url><loc>https://uatuae.mansamworld.com/products</loc></url>
      <url><loc>https://www.mansamworld.com/external</loc></url>
      <url><loc>https://uatuae.mansamworld.com/home</loc></url>
      <url><loc>not-a-url</loc></url>
    </urlset>
    """

    assert parse_sitemap_urls(sitemap, MANSAM_BASE, 5) == [
        "https://uatuae.mansamworld.com/home",
        "https://uatuae.mansamworld.com/products",
    ]
    assert parse_sitemap_urls(sitemap, MANSAM_BASE, 1) == ["https://uatuae.mansamworld.com/home"]
    assert parse_sitemap_urls("<html><body>no sitemap</body></html>", MANSAM_BASE, 5) == []


def test_detect_spa_shell_matches_framework_bootstrap_markup() -> None:
    assert detect_spa_shell('<html><body><app-root></app-root></body></html>')
    assert detect_spa_shell('<html><body><div id="root"></div></body></html>')
    assert not detect_spa_shell("<html><body><h1>Mansam World</h1></body></html>")


def test_is_unrendered_spa_separates_ssr_pages_from_empty_shells() -> None:
    shell = '<html><body><app-root></app-root><script src="main.38242ade25b2d820.js"></script></body></html>'
    ssr = (
        '<html><body ng-version="17.0.0"><app-root><h1>Mansam World</h1><p>'
        + "Signature Arabian perfumery, crafted and sourced in the Emirates. " * 6
        + "</p></app-root></body></html>"
    )

    assert estimate_visible_text_length(shell) == 0
    assert is_unrendered_spa(shell)
    assert detect_spa_shell(ssr)
    assert not is_unrendered_spa(ssr)
    assert not is_unrendered_spa("<html><body><h1>Mansam World</h1></body></html>")


def test_source_document_name_is_derived_from_url_and_content_type() -> None:
    assert (
        source_document_name("https://uatuae.mansamworld.com/api/public/productlines", "application/json")
        == "uatuae.mansamworld.com-productlines.json"
    )
    assert source_document_name(MANSAM_BASE, "text/html") == "uatuae.mansamworld.com-home.html"
    assert source_document_name("https://uatuae.mansamworld.com/", "text/html") == "uatuae.mansamworld.com-index.html"


def test_content_type_helpers_cover_html_json_and_text_sources() -> None:
    assert {"text/html", "application/json", "text/plain"} <= INGESTIBLE_CONTENT_TYPES
    assert is_ingestible_content_type("application/json")
    assert is_ingestible_content_type("text/html; charset=utf-8".split(";")[0].strip())
    assert not is_ingestible_content_type("image/png")
    assert media_type_for("text/json") == "application/json"
    assert media_type_for("text/html") == "text/html"
    assert media_type_for("application/octet-stream") == "text/html"


def test_spa_hint_points_at_the_content_api() -> None:
    assert "/api/public/productlines" in SPA_EXTRACTION_HINT