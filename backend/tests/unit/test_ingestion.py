import json
from io import BytesIO

import pytest
from app.ingestion import ExtractionError, StructureAwareChunker, extract_document
from app.ingestion.types import ExtractedDocument, ExtractedElement
from openpyxl import Workbook

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_xlsx_extraction_preserves_sheet_and_row_provenance() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Products"
    sheet.append(["SKU", "Name", "Name"])
    sheet.append(["ABM001", "Oud", "عود"])
    payload = BytesIO()
    workbook.save(payload)

    extracted = extract_document(payload.getvalue(), "catalog.xlsx", XLSX_MEDIA_TYPE)

    assert extracted.metadata["sheet_names"] == ["Products"]
    assert len(extracted.elements) == 1
    element = extracted.elements[0]
    assert "SKU: ABM001" in element.text
    assert "Name_2: عود" in element.text
    assert element.metadata == {
        "sheet": "Products",
        "sheet_index": 1,
        "row_number": 2,
        "source_type": "spreadsheet_row",
    }


def test_chunker_retains_section_and_page_provenance() -> None:
    document = ExtractedDocument(
        "policy.md",
        "text/markdown",
        [
            ExtractedElement("Delivery", "heading", page_number=2),
            ExtractedElement("Orders ship in two business days.", "paragraph", page_number=2),
            ExtractedElement("Tracking is sent by email.", "paragraph", page_number=2),
        ],
    )

    chunks = StructureAwareChunker(target_chars=200, max_chars=300, overlap_chars=20).chunk(document)

    assert len(chunks) == 1
    assert chunks[0].section == "Delivery"
    assert chunks[0].page_number == 2
    assert chunks[0].metadata["source_name"] == "policy.md"


def test_json_extraction_flattens_records_into_readable_lines() -> None:
    payload = json.dumps(
        [
            {
                "productLineId": 1,
                "productLineNameEn": "Eau de Parfum 100ml",
                "descriptionEn": "An exquisite Eau De Parfum collection with Arabian roots.",
                "image1": "Family1_EDP.jpg",
                "descriptionAr": None,
                "active": True,
                "notes": ["oud", "amber"],
                "details": {"family": "Oriental"},
            },
            {"productLineNameEn": "Luban", "descriptionEn": None, "active": False},
            {"descriptionEn": None, "active": True},
        ]
    ).encode()

    extracted = extract_document(payload, "productlines.json", "application/json")

    assert extracted.metadata == {"record_count": 3}
    assert len(extracted.elements) == 2
    element = extracted.elements[0]
    assert element.kind == "table"
    assert element.metadata == {"record_index": 0}
    assert "productLineId: 1" in element.text
    assert "productLineNameEn: Eau de Parfum 100ml" in element.text
    assert "notes[0]: oud" in element.text
    assert "notes[1]: amber" in element.text
    assert "details.family: Oriental" in element.text
    assert "descriptionAr" not in element.text
    assert "active" not in element.text
    assert extracted.elements[1].text == "productLineNameEn: Luban"
    assert extracted.elements[1].metadata == {"record_index": 1}


def test_json_extraction_rejects_records_without_readable_text() -> None:
    with pytest.raises(ExtractionError, match="no usable text"):
        extract_document(b'[{"descriptionEn": null, "active": true}]', "empty.json", "application/json")


def test_html_extraction_keeps_headings_and_tables() -> None:
    html = b"""
    <html><head><title>About</title></head><body>
      <h1>Delivery</h1>
      <p>Orders ship in two business days.</p>
      <table><tr><th>Region</th><th>Days</th></tr><tr><td>UAE</td><td>2</td></tr></table>
    </body></html>
    """

    extracted = extract_document(html, "about.html", "text/html")

    assert extracted.metadata["title"] == "About"
    assert extracted.elements[0].text == "Delivery"
    assert extracted.elements[0].kind == "heading"
    assert extracted.elements[0].section == "Delivery"
    assert extracted.elements[-1].kind == "table"
    assert "UAE | 2" in extracted.elements[-1].text


def test_html_extraction_reports_spa_shell_with_actionable_error() -> None:
    html = b"""
    <html><body><app-root></app-root>
      <script src="main.38242ade25b2d820.js" type="module"></script>
    </body></html>
    """

    with pytest.raises(ExtractionError, match="single-page application"):
        extract_document(html, "home.html", "text/html")
