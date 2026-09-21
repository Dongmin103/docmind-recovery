# Third-party notices for the DocMind CPU canary

This file summarizes the additional runtime components introduced by the
DocMind canary. It does not replace the corresponding upstream license text.

| Component | Pinned use | License/source |
| --- | --- | --- |
| RAGFlow | v0.27.1 source base | Apache-2.0, existing repository LICENSE |
| Surya | `surya-ocr==0.22.1` | Apache-2.0, <https://github.com/datalab-to/surya> |
| Surya 2 GGUF weights | revision `6a3a4c30e5e74446d4f8b6afd05b2f2da970f470`, mounted externally | Modified AI Pubs OpenRAIL-M; review <https://github.com/datalab-to/surya/blob/v0.22.1/MODEL_LICENSE> before download or use |
| llama.cpp | release `b10718`, Ubuntu x64 binary | MIT, <https://github.com/ggml-org/llama.cpp> |
| Docling | `docling-slim[format-office]==2.115.0`, disabled in first canary | MIT, <https://github.com/docling-project/docling> |
| rhwp-python | `0.8.1`, disabled in first canary | MIT, <https://github.com/DanMeon/rhwp-python> |
| OpenViking | external image pinned by digest, v0.4.14 | AGPL-3.0, <https://github.com/volcengine/OpenViking> |

Surya model files, provider credentials, source documents, indexes, database
contents and target-specific capability evidence are not redistributed in Git
or in the public DocMind application image.
