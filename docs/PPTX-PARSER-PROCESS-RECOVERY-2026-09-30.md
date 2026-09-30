# PPTX parser process recovery deployment

Date: 2026-09-30, 15:57 KST

## Problem and change

LibreOffice conversion left orphaned GnuPG helper processes behind. With Node running as PID 1, these became zombies and consumed the parser's 64-task limit. The observed exhausted parser had 36 zombies; memory exhaustion was not observed.

Enable `init: true` for `kordoc-parser` in both the parser platform Compose definition and the separately defined Windows generationless service. The init process reaps orphaned children. The parser image, 64-task limit, memory/CPU limits, OCR-off configuration, HWP normalizer-v3, embedding timeout, and disabled PPTX fallback remain unchanged.

## Verification

- Isolated comparison with the same image and resource limits: without init, 12 synthetic PPTX requests produced 8 successes, 4 failures, 36 zombies, and 17 PID-limit hits. With init, all 12 succeeded with zero zombies and zero PID-limit hits.
- All 28 affected real PPTX documents passed parsing and normalization in the isolated init-enabled parser. This was diagnostic verification, not reindexing.
- Live deployment: one synthetic PDF and 12 consecutive synthetic PPTX requests returned HTTP 200 and three blocks each. Zombie count and PID-limit hits stayed zero. Stable task count stayed at 25 after the first and last PPTX requests.
- The deployment guard verified unchanged environment values, image identity, and runtime settings except `HostConfig.Init`. The application returned HTTP 200, and the host worker was restored after deployment.
- `git diff --check` and the retired-runtime reference guard passed.

The first deployment attempt rolled back because its environment comparison treated array order as significant. Independent inspection confirmed identical key/value pairs. The guard was corrected to compare sorted entries before the successful deployment.

## Scope and evidence

Only the parser container was recreated. Existing failed documents were not reindexed, and PPTX fallback was not enabled. This fixes the demonstrated process accumulation problem; it does not establish that all 55 earlier failures are resolved.

Local diagnostic and deployment evidence is retained under the ignored `.local/kordoc-t-drive-cutover/` directory: `pptx-init-deployment.json`, `pptx-init-live-verification.jsonl`, and the earlier PID comparison results. Original documents, extracted text, environment values, and source paths are not included in this report.
