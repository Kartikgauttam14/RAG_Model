"""Recorded count fields.

The workbook states some quantities outright (``Setting Key: boutique_count_ksa / Value: 6``).
Asking a model to tally rows across retrieved chunks instead produced 4, 6 and "five" on
different runs of the same question, because the row holding the number competes for the top-k
with dozens of neighbouring rows and is often not retrieved at all. These patterns identify a
counting question and parse the recorded fields, so both the retriever (which fetches them
directly) and the answer service (which presents them as authoritative) agree on one format.
"""

import re
from typing import Any

COUNT_QUESTION = re.compile(r"\bhow many\b|\bhow much\b|\bnumber of\b|\bcount\b|كم|عدد", re.IGNORECASE)

# Keys must contain "_count_" so that a setting such as "phone_default_country" is not
# mistaken for a count, and the value stops at the end of its line.
COUNT_SETTING = re.compile(
    r"Setting Key:\s*(?P<key>[\w]*_count_[\w]*)\s*Value:\s*(?P<value>[^\n]{1,40}?)"
    r"\s*Description:\s*(?P<description>[^\n]+)",
    re.IGNORECASE,
)


def is_count_question(question: str) -> bool:
    return bool(COUNT_QUESTION.search(question))


def count_fields(text: str) -> list[dict[str, Any]]:
    """Return the recorded count fields found in one chunk of text."""
    fields: list[dict[str, Any]] = []
    for match in COUNT_SETTING.finditer(text):
        fields.append(
            {
                "key": match.group("key").strip(),
                "value": match.group("value").strip(),
                "description": " ".join(match.group("description").split())[:200],
            }
        )
    return fields
