from rag.parser_platform.pdf_source import normalize_pdf_source


def test_known_ra_transport_preamble_is_stripped_only_for_pdf() -> None:
    source = b"{!-- ra:000000001dd4a4b9000000000652c9bb --}\n%PDF-1.7\nbody"

    normalized = normalize_pdf_source(source)

    assert normalized.content == b"%PDF-1.7\nbody"
    assert normalized.stripped_prefix_bytes == 45
    assert normalized.normalized


def test_unknown_prefix_is_not_silently_treated_as_pdf() -> None:
    source = b"unexpected\n%PDF-1.7\nbody"

    normalized = normalize_pdf_source(source)

    assert normalized.content == source
    assert not normalized.normalized


def test_ra_transport_preamble_without_pdf_header_is_not_stripped() -> None:
    source = b"{!-- ra:000000001dd4a4b9000000000652c9bb --}\nnot-a-pdf"

    normalized = normalize_pdf_source(source)

    assert normalized.content == source
    assert not normalized.normalized
