# Parser platform target Linux notes

The parser platform is disabled by default. Enabling requires both
`PARSER_PLATFORM_ENABLED=1` and `PARSER_PLATFORM_INTEGRATION_READY=1`, with
`TE_RUN_MODE=0` exactly.

`PARSER_PLATFORM_INTEGRATION_READY` is also the additive-schema read gate. Set
it to `1` only after the database migration succeeds. Once a versioned chunk
set has been activated, an ingestion rollback must set only
`PARSER_PLATFORM_ENABLED=0`; readiness stays `1` so staging and retained chunk
sets remain hidden from search and preview.

## CPU profile

The CPU profile uses a separately sealed Surya runtime image and adds only the
HTTP service layer. `SURYA_CPU_RUNTIME_IMAGE` must be replaced with an immutable
target-architecture image digest. The currently validated local runtime is
Linux arm64 and is not an amd64 production artifact.

The pinned Docling base-image digests in the example environment are also
arm64 manifests. An amd64 target must supply separately reviewed amd64 digests
for `DOCLING_UV_BASE_IMAGE` and `DOCLING_PYTHON_BASE_IMAGE` and set
`PARSER_PLATFORM_PLATFORM=linux/amd64`.

Measured on the local Linux arm64 Docker CPU path:

- one PDF page: about 550 seconds
- one 640x320 Office screenshot: about 987 seconds
- observed screenshot run memory: about 1.08 GiB

RAM prevents OOM but does not make VLM inference fast. A 40 GB RAM server with
no GPU should expect CPU-class latency.

## NVIDIA GPU profile

Surya officially uses `vllm` for NVIDIA GPU inference. The GPU profile is
intentionally fail-closed with the placeholder
`docmind-surya-parser:gpu-runtime-not-configured` until all of the following are
known and verified:

1. `nvidia-smi` succeeds on the target.
2. NVIDIA Container Toolkit is installed and a test container sees the GPU.
3. A Surya 0.22.1 + exact model revision + pinned vLLM/CUDA image is built.
4. The immutable GPU image digest, SBOM, license inventory and no-startup-download
   smoke are reviewed.

Do not enable CPU and GPU profiles together because both advertise the
`surya-parser` internal DNS alias.

Official references:

- https://github.com/datalab-to/surya
- https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
