# Surya 2 parser

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

## NVIDIA GPU parser on the Windows WSL host

`Containerfile.gpu-amd64` builds the same pinned Surya 0.22.1 service with
llama.cpp b10718 from its verified commit and CUDA 12.4 for `sm_61` (GTX 1080).
It reuses the licensed, SHA-verified GGUF files through the same read-only
`/models` mount, retains one-request admission, and requests 99 GPU layers.
The CUDA image contains no model weights. The original CPU image remains
available for rollback.

Install and configure the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
inside the Docker-running WSL distribution first, then restart Docker and
verify that `docker run --rm --gpus all` can see the GPU. Append
`app/docker/docker-compose-windows-dev-surya-gpu.yml` after the existing
Windows development and generationless E2E Compose overlays when building or
starting the Surya service. The overlay intentionally overrides the existing
`surya-parser-cpu` service key so the app's `surya-parser` network alias and
single model mount continue to resolve to one parser. The container/image
labels identify the CUDA implementation.

Before replacing the CPU service, start the GPU image separately with the
same read-only model mount and run a real OCR request. `/health` reports
`gpu_device_visible` and `gpu_layers_requested` as configuration facts.
`gpu_execution_verified` becomes true after a successful request only when
the exact spawned llama-server PID appears in NVIDIA's compute-process list.
`gpu_offload_verified` and layer counts remain false/null unless llama.cpp
also prints its per-layer and CUDA-buffer evidence. The b10718 server on this
host omits those lines at its default verbosity, so a null layer count does
not negate a verified CUDA compute process. Keep the CPU service until the
OCR result and process-specific execution proof both pass.

On this Windows host, the production-like development stack has two tracked
overlays and one ignored local source overlay. Keep all three when appending
the GPU overlay; otherwise the existing source and secret mounts can change:

```powershell
C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  -f app/docker/docker-compose-windows-dev-generationless-e2e.yml `
  -f .local/docker/legacy-doc-source-overlay.yml `
  -f app/docker/docker-compose-windows-dev-surya-gpu.yml `
  --profile full config --quiet

C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  -f app/docker/docker-compose-windows-dev-generationless-e2e.yml `
  -f .local/docker/legacy-doc-source-overlay.yml `
  -f app/docker/docker-compose-windows-dev-surya-gpu.yml `
  --profile full up -d --no-deps --no-build surya-parser-cpu
```

The service key is historical; with the fourth overlay its image and GPU
device request are CUDA-backed. To roll back this one parser, repeat the
`up -d --no-deps --no-build --force-recreate surya-parser-cpu` command with
only the first three Compose files, leaving the app and data services alone.

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
