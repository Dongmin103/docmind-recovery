# Consented PDF OCR service

`docker-compose-windows-dev-surya-gpu.yml` adds the Surya 2 service to the
Windows parser network. It is an opt-in overlay; the ordinary PDF route remains
Kordoc native text with OCR disabled. The service has no published host port.

Build the derived image from `Containerfile` using the pinned local
`docmind-surya-parser:gpu-0.22.1-cuda12.4-sm61` base. The derived image copies
the maintained `service.py`; do not run a second GPU-resident Surya container
at the same time. Set `SURYA_MODEL_DIR` to the read-only directory containing
the verified `surya-2.gguf` and `surya-2-mmproj.gguf` files. The service
checks their SHA-256 values at startup.

The application uses `PARSER_PLATFORM_SURYA_URL` (default
`http://surya2-ocr:8091` inside the parser network) only for an explicitly
consented PDF job. Keep the worker's `claim_lease_seconds` at 1800; the Surya
HTTP deadline is 1200 seconds and the service request watchdog is 1100
seconds. A shorter lease is rejected for a consented OCR job. The client
checks `/ready` before each request and requires verified GPU execution from
`/health` after inference before any result can be staged for activation.

Native PDF text is always retained. Surya text is added by source page when
it is not already present in a native block. Rendered OCR coordinates are
omitted from PDF highlights because they are not PDF page coordinates.
