# Surya 2 CPU parser

This directory contains the isolated `linux/amd64` CPU parser used by the
DocMind PDF canary.

- `Containerfile.cpu-amd64` installs `surya-ocr==0.22.1` from the frozen lock.
- The image downloads and verifies the official llama.cpp `b10718` x64 binary.
- Surya GGUF model files are never copied into Git or the public image. They are
  mounted read-only at `/models` by Compose.
- `service.py` serves health checks concurrently while the Surya engine lock keeps
  full-page inference concurrency at one on the 31 GiB target.
- PDF parsing uses direct Surya full-page recognition. It does not invoke
  Docling, DeepDoc or PaddleOCR.

Before starting the service, review the Surya model license and run:

```bash
./scripts/download_surya_models.sh ./models/surya
```

The script pins the upstream model revision and verifies both SHA-256 values.
