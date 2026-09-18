"""Validate the golden evaluation dataset contract before live RAG evaluation."""

import json
from pathlib import Path


def main() -> None:
    path = Path("evaluation/golden.json")
    cases = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "id",
        "question",
        "required_sources",
        "allowed_sources",
        "must_not_claim",
    }
    assert isinstance(cases, list) and cases, "Golden dataset must be a non-empty list"
    ids: set[str] = set()
    for case in cases:
        assert required <= case.keys(), f"Missing fields in {case}"
        assert case["id"] not in ids, f"Duplicate case id: {case['id']}"
        assert isinstance(case["question"], str) and case["question"].strip()
        ids.add(case["id"])
    print(f"Golden dataset contract valid: {len(cases)} cases")


if __name__ == "__main__":
    main()
