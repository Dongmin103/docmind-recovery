# AVDC OpenVINO embedding deployment

`service.py` and `pool.py` are byte-for-byte copies of the CPU deployment verified
over pinned SSH on 2026-11-06. The separate Windows GPU deployment continues to
use `../local_embedding/service.py`; do not replace it with this CPU-only adapter.
Trial wording in the source is retained to preserve identity; the deployment
record establishes that this version is now persistent.

## Runtime contract

- GTE multilingual base, 768-dimensional normalized CLS vectors, FP32.
- OpenVINO 2026.4.1; 22 workers, four inference threads and one stream per worker.
- Disjoint four-logical-processor affinity groups. OpenVINO's internal pinning
  is disabled; the process pool supplies affinity instead.
- Short inputs (<=128 tokens): at most 16 per batch, at least 85% token fill.
  Longer inputs: individually. Original request order is restored after sorting.
- Inputs over 8192 tokens are rejected, never truncated. Bearer-authenticated
  `/v1/embeddings`; metadata-only unauthenticated `/health`.
- Pool logs worker port, times and status, not text, vectors or keys.

`config.example.json` contains verified non-secret CPU/model settings and a key
file path. Copy it outside Git and use the existing restricted key file.
Model revision: `9bbca17d9273fd0d03d5725c7a4b0f6b45142062`; code revision:
`40ced75c3017eb27626c9d4ea981bde21a2662f4`.

## Build and export

This extends the existing AVDC CPU image, not a fresh installation bundle.
Original base image ID:
`sha256:4c0a891dbb00ce030772f8e6962059fa791060c9648f21a82e025fd388a19e31`.
It supplies the pinned local model/tokenizer and trusted code cache, Python,
torch 2.8.0+cpu, transformers 4.57.1, tokenizers 0.22.2 and numpy 2.4.6.
Image archives, model weights and credentials are deliberately outside Git.

Put approved offline Linux wheels for OpenVINO 2026.4.1 and telemetry 2025.2.0
in ignored `wheels/`. Tag the verified base image locally, then:

```sh
docker build --network none --pull=false --build-arg BASE_IMAGE=docmind-ovfull-base:20261106 -t docmind-gte-openvino:20261106 tools/avdc_embedding
```

`export_model.py` is the actual successful export/compatibility script. Run it in
an isolated container of this image with a restricted writable export directory
mounted at `/trial`. Its `config.json` needs local `model_path` and `code_revision`.
The script saves `model.xml`/`model.bin` without FP16 compression, incorporates
CLS pooling and L2 normalization, and compares synthetic Korean/English vectors
against PyTorch. It uses local weights only. Do not convert inside the serving
container. The adapter expects `/opt/docmind/openvino-model/model.xml`.

Recorded hashes identify deployed artifacts; later exports/builds may serialize
differently and need their own numerical/deployment verification. No build or
conversion was repeated merely to publish these sources.

## Apply and rollback

`compose.example.yaml` is a path-parameterized embedding-only override for the
existing AVDC stack, with matching CPU/memory limits. Existing networks,
healthcheck, caches, restart policy and other services come from the base stack.
Inspect the merged `docker compose ... config` before applying; the example
alone is not bootable. Keep real config/env/keys in ignored `.local/` paths.

Production uses both `compose.override.yaml` and `.local/bench98/override.yaml`
under `/opt/docmind-avdc`. Only embedding was recreated using
`up -d --no-deps --force-recreate --wait --wait-timeout 420 embedding`.
Backups and `rollback.sh` remain in `.local/deploy-openvino-20261106/`;
rollback restores both overrides and recreates only embedding. Its temporary
automatic rollback timer is disabled.

Publishing these files does not deploy, convert models or index documents.
Tests use fakes and synthetic inputs:

```sh
python -m unittest discover -s tools/avdc_embedding -p 'test_*.py'
```

See [deployment evidence](../../docs/AVDC-OPENVINO-DEPLOYMENT-2026-11-06.md).
