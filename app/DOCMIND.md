# DocMind CPU deployment canary

This branch packages DocMind on top of RAGFlow v0.27.1 for an internal-network,
loginless shared workspace. The first release target is intentionally narrow:

- Ubuntu 24.04, `linux/amd64`
- 31 GiB RAM, 8 GiB swap
- Surya 2 PDF parsing on CPU with exactly one request admitted at a time
- one authorized PDF, at most five pages, for the first canary
- no real Office, HWP/HWPX, media OCR, bulk corpus or accuracy benchmark

This is a source and CPU-canary candidate. It is not yet a production-verified
release.

## 1. Prepare configuration

```bash
cp docker/.env.docmind.cpu.example docker/.env.docmind.cpu
openssl rand -hex 32
```

Put the generated value in `OPENVIKING_ROOT_API_KEY` and replace both external
provider keys in `docker/.env.docmind.cpu`. After OpenViking starts, use its
Admin API to create a dedicated account administrator and store the returned
user/admin key in `OPENVIKING_DATA_API_KEY`. Root keys are management-only and
must never be configured as DocMind data credentials. Never commit that file.

Review the upstream Surya model license before downloading the weights. The
weights are deliberately excluded from Git and every public image.

```bash
chmod +x scripts/download_surya_models.sh
export SURYA_MODEL_LICENSE_ACCEPTED=1
./scripts/download_surya_models.sh ./models/surya
mkdir -p runtime/private
```

## 2. Build the AMD64 CPU images

```bash
docker compose \
  -f docker/docker-compose.yml \
  -f docker/docker-compose-docmind.yml \
  -f docker/docker-compose-parser-platform.yml \
  --env-file docker/.env \
  --env-file docker/.env.docmind.cpu \
  --profile cpu \
  --profile elasticsearch \
  --profile metadata-mysql \
  --profile parser-platform-cpu \
  build ragflow-cpu surya-parser-cpu
```

Do not reuse private local overlay images. The build must start from this clean
checkout.

## 3. Start and verify OpenViking

```bash
docker compose \
  -f docker/docker-compose.yml \
  -f docker/docker-compose-docmind.yml \
  -f docker/docker-compose-parser-platform.yml \
  --env-file docker/.env \
  --env-file docker/.env.docmind.cpu \
  up -d openviking

set -a
source docker/.env.docmind.cpu
set +a
python3 scripts/docmind_openviking_capability_gate.py \
  --container docmind-openviking \
  --output runtime/private/docmind-openviking-capability.json
```

The capability gate uses `OPENVIKING_DATA_API_KEY`, creates new temporary
OpenViking roots, restarts only the OpenViking container, verifies persistence,
and writes a target-specific PASS artifact. Historical local evidence and root
keys used as data credentials are not accepted.

## 4. Start DocMind and the Surya CPU parser

```bash
docker compose \
  -f docker/docker-compose.yml \
  -f docker/docker-compose-docmind.yml \
  -f docker/docker-compose-parser-platform.yml \
  --env-file docker/.env \
  --env-file docker/.env.docmind.cpu \
  --profile cpu \
  --profile elasticsearch \
  --profile metadata-mysql \
  --profile parser-platform-cpu \
  up -d
```

Only ports 80, 443, 9380 and 9381 should be exposed to the internal network.
OpenViking is bound to host loopback; parser services have no host ports.

## 5. One-PDF canary

1. Open `http://SERVER_IP/docmind`.
2. Configure the required RAGFlow model providers in Settings.
3. Import one authorized PDF of at most five pages.
4. Wait for `INDEXED`.
5. Create a Catalog draft, review L0/L1, Publish and run one source-grounded
   search.
6. Open the original/chunk inspection view and confirm text, table and page
   provenance.

Monitor resources while the PDF is parsed:

```bash
watch -n 2 'free -h; docker stats --no-stream'
```

Stop the canary if the Surya or RAGFlow container is OOM-killed/restarted, swap
continues growing beyond 2 GiB, the parser exceeds the configured two-hour
deadline, or the active Catalog changes before an explicit Publish.

## Current support boundary

| Path | Public source | First server canary |
| --- | --- | --- |
| PDF to Surya 2 to HybridChunker | Included | One PDF only |
| Search, Catalog, L0/L1 and Publish | Included | Same PDF only |
| DOCX/XLSX/PPTX Docling service | Included but disabled | Not verified |
| HWP/HWPX RHWP service | Included but disabled | Not verified |
| Media OCR | Excluded | Not supported |
| GPU parsing | Excluded from this target | Not supported |
