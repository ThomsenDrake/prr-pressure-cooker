from prr_pressure_cooker.ingest import (
    extract_case_external_refs,
    html_to_text,
    strip_quoted_history,
)


def test_html_to_text_skips_styles_and_keeps_body():
    text = html_to_text(
        "<html><head><style>p {color:red}</style></head>"
        "<body><p>Good morning.</p><p>Acknowledges receipt.</p></body></html>"
    )

    assert "color:red" not in text
    assert "Good morning." in text
    assert "Acknowledges receipt." in text


def test_strip_quoted_history_removes_prior_thread():
    text = strip_quoted_history(
        "Good morning.\n\nThis acknowledges receipt.\n\nFrom: Drake <x@example.com>\n"
        "Subject: Formal Escalation\nOriginal request text"
    )

    assert "This acknowledges receipt." in text
    assert "Original request text" not in text


def test_extract_case_external_refs_handles_records_center_ids():
    refs = extract_case_external_refs(
        "[Records Center] PUBLIC RECORD REQUEST :: P041212-031826",
        "Public Records Request - General :: R052454-022226",
    )

    assert {ref["normalized_ref"] for ref in refs} == {
        "records-center:p041212-031826",
        "records-center:r052454-022226",
    }
