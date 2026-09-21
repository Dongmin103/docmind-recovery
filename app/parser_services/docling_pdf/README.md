# DocMind Docling PDF parser

Internal-only, linux/amd64 service for model-free Docling Parse PDF extraction.
It groups native PDF text cells into positioned lines, never performs OCR,
never calls remote services, and exposes no host port. DocMind's deterministic
preflight router selects it only for the configured PDF canary document IDs.
