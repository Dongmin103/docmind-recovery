import pytest

from api.utils.file_response import sanitize_content_disposition_filename


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("folder/report.pdf", "report.pdf"),
        (r"folder\report.pdf", "report.pdf"),
        ("sample 한국어.pdf", "sample____.pdf"),
    ],
)
def test_sanitize_content_disposition_filename_is_ascii_safe(filename, expected):
    result = sanitize_content_disposition_filename(filename)
    assert result == expected
    assert result.isascii()


def test_sanitize_content_disposition_filename_handles_empty_value():
    assert sanitize_content_disposition_filename(None) is None
