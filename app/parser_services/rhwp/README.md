# DocMind RHWP parser service

This isolated service parses ordinary, unprotected HWP 5.x and HWPX documents with
`rhwp-python==0.8.1`. Native document creation, complete IR consumption, media hashing, and
resource release occur in one dedicated worker thread. Only immutable JSON crosses the service
boundary, and media OCR is disabled.

Only HWP/HWPX output is then converted to a `DoclingDocument` and chunked with
`docling-core==2.92.0` `HybridChunker`. The pinned multilingual MiniLM tokenizer
uses a 512-token ceiling. Small peer paragraphs merge within heading context;
large tables use compact native Markdown with repeated headers. Fitting connected
body-rowspan groups stay within one chunk; oversized groups retain bounded row
splitting. The chunker policy identity is 2.92.0+docmind-compact-rowspan-v1. Search text and table display
HTML are returned separately, and every searchable RHWP locator must remain
covered by the chunk manifest.

Encrypted, DRM-controlled, and distribution-protected documents are outside this canary's scope.
The service does not claim to detect or support those protection classes. Malformed containers,
native parser errors, incomplete media identity, empty text, and ambiguous failures return a
generic fail-closed error and must not be indexed or activated.

본 제품은 한컴의 HWP 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.

The public Containerfile is architecture-neutral and the CPU Compose profile
builds it as `linux/amd64`. The initial public canary validates PDF only, so HWP
and HWPX remain disabled until a separate real-document acceptance run is
completed on the target server.

The amd64 image preloads the installed system FreeType library because the
pinned RHWP native wheel otherwise fails to resolve FT_Palette_Data_Get.
