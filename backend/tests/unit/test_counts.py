from app.rag.counts import count_fields, is_count_question

BOUTIQUE_ROW = (
    "Sheet: 10_Settings\nRow: 38\nSetting Key: boutique_count_ksa\nValue: 6\n"
    "Description: Number of KSA boutiques (Riyadh x3, Jeddah, Makkah, Madinah)\n"
    "Last Updated: 2026-06-15"
)
COUNTRY_ROW = (
    "Sheet: 10_Settings\nRow: 41\nSetting Key: phone_default_country\nValue: UAE\n"
    "Description: Default country code for the advisor line\nLast Updated: 2026-06-15"
)


def test_recorded_count_row_is_parsed() -> None:
    assert count_fields(BOUTIQUE_ROW) == [
        {
            "key": "boutique_count_ksa",
            "value": "6",
            "description": "Number of KSA boutiques (Riyadh x3, Jeddah, Makkah, Madinah)",
        }
    ]


def test_setting_with_country_in_its_key_is_not_a_count() -> None:
    """`phone_default_country` contains "count" but states a country, not a quantity."""
    assert count_fields(COUNTRY_ROW) == []


def test_count_questions_are_detected_in_both_languages() -> None:
    assert is_count_question("How many Mansam boutiques are there in Saudi Arabia?")
    assert is_count_question("عدد بوتيكات منسَم في السعودية")
    assert is_count_question("What is the number of product lines?")
    assert not is_count_question("Which scent family does the Qanun collection belong to?")
