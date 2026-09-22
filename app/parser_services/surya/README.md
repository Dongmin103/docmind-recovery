# Surya 2 CPU parser

This directory contains the isolated `linux/amd64` CPU parser used by the
DocMind PDF canary.

- `Containerfile.cpu-amd64` installs `surya-ocr==0.22.1` from the frozen lock.
- The image downloads and verifies the official llama.cpp `b10718` x64 binary.
- Surya GGUF model files are never copied into Git or the public image. They are
  mounted read-only at `/models` by Compose.
- `service.py` serves health checks concurrently while a non-blocking admission
  gate keeps full-page inference concurrency at one. A second parse receives
  `503 PARSER_SURYA_BUSY` instead of consuming an unbounded worker queue.
- PDF parsing uses direct Surya full-page recognition. It does not invoke
  Docling, DeepDoc or PaddleOCR.
- Office-media OCR caps the upstream full-page decoder at 1,024 tokens without
  changing the 12,288-token PDF setting. Inside the engine lock, media calls
  also temporarily use a 600-second inference timeout and restore the PDF
  setting afterward. The Compose defaults layer the media service watchdog at
  660 seconds and the caller deadline at 720 seconds, keeping the whole OCR
  stage bounded below the host-worker lease.
- Runtime cache and llama.cpp sentinel data use a private, non-persistent tmpfs
  owned by UID/GID 10001 with mode `0700`; model weights remain on the separate
  read-only model mount.

Before starting the service, review the pinned
[Surya model license](https://github.com/datalab-to/surya/blob/v0.22.1/MODEL_LICENSE).
Downloading or using the weights constitutes acceptance. The operator must be
authorized to accept for the applicable person or organization and must check
the license's use, attribution, share-alike, and commercial restrictions. Only
after that review, explicitly record acceptance for the download command:

```bash
SURYA_MODEL_LICENSE_ACCEPTED=1 ./scripts/download_surya_models.sh ./models/surya
```

The script pins the upstream model revision and verifies both SHA-256 values.
