from __future__ import annotations

import re
from dataclasses import dataclass


# A small number of archived source PDFs were prefixed by an upstream
# "{!-- ra:<hex> --}" transport marker. The marker is not part of the PDF
# and makes strict PDF signature validation reject an otherwise intact file.
# Keep the accepted grammar deliberately narrow: arbitrary bytes before the
# PDF header must continue to fail closed.
_RA_PREAMBLE = re.compile(
    rb"\A\{!-- ra:[0-9A-Fa-f]{8,128} --\}(?:\r\n|\n|\r)"
)


@dataclass(frozen=True)
class PdfSourceNormalization:
    content: bytes
    stripped_prefix_bytes: int = 0

    @property
    def normalized(self) -> bool:
        return self.stripped_prefix_bytes > 0


def normalize_pdf_source(source_bytes: bytes) -> PdfSourceNormalization:
    """Return parseable PDF bytes without mutating the stored source artifact.

    Only the known "ra" transport preamble is removed, and only when it is
    immediately followed by a PDF header. This prevents treating arbitrary
    mismatched content as a PDF.
    """

    match = _RA_PREAMBLE.match(source_bytes)
    if match is None or not source_bytes[match.end() :].startswith(b"%PDF-"):
        return PdfSourceNormalization(content=source_bytes)
    return PdfSourceNormalization(
        content=source_bytes[match.end() :],
        stripped_prefix_bytes=match.end(),
    )
