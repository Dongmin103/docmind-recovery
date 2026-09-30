# DEPT2 indexing performance: measured bottleneck and improvement plan

Date: 2026-09-30 (KST). Scope: encrypted T: / DEPT2 source `dept-2-e2e`.
No document names, content, source paths, credentials, or vectors are recorded here.

## Target and measurement boundary

- User target: a representative **200 MB batch of documents** indexed within **600 seconds**.
- Start the clock when the files become observable at T:; stop after every selected
  file is searchable, its job is COMPLETE, and both host and container plaintext
  cleanup are COMPLETE. Record discovery/queue, decrypt, parse, chunk, embedding,
  index/activation, status acknowledgement, and cleanup separately.
- Keep OCR disabled for the remaining DEPT2 run, as requested. An image-only
  document with no searchable text is a failure, not a successful zero-text index.
- This target is unproven. The present 242-file supported population totals
  272,335,213 encrypted bytes. Files still in progress or awaiting retry are
  excluded from the completed-file timing baseline below.

## Evidence snapshot

At the snapshot: 173 COMPLETE, 30 DISCOVERED, 1 PARSING, 37 RETRY_WAIT, and
1 FAILED. The host worker and source watcher were running. The 173 completed
files contain 142,888,780 encrypted bytes, 7,399 chunks, and 2,466,452
reported embedding tokens. There are 172 completed host timing records.

| Measurement across completed files | Observed |
| --- | ---: |
| Host cycle time, sum of 172 records | 5,593.86 s (93.2 min) |
| Embedding stage, sum of 173 records | 4,798.85 s (80.0 min) |
| Parse HTTP stage, sum | 482.13 s (8.0 min) |
| Index staging stage, sum | 53.94 s |
| Host median / p95 / max cycle | 7.82 / 115.90 / 712.23 s |
| Embedding median / p95 / max | 5.03 / 107.25 / 675.21 s |
| HWP embedding | 3,847.92 s, 80.2% of all completed embedding time |
| XLSX embedding | 753.36 s, 15.7% |
| Top two HWP embedding | 1,329.78 s, 27.7% of all completed embedding time |

The completed-file host cycles alone imply about 1.53 MB/min for this mix;
the target requires 20 MB/min, roughly **13.1 times** this measured rate.
This is a comparison of service time sums, not a wall-clock batch benchmark.
It omits failed attempts, waits, and discovery, so it cannot establish the
actual 200 MB completion time. Extrapolating from file bytes alone is also
imprecise: embedded images and text density vary widely.

One 6,873,774-byte HWP produced 1,143 chunks and 351,718 reported tokens;
embedding took 675.21 s, parse HTTP 5.60 s, and host cycle 712.23 s.
A second HWP of similar size also produced 1,143 chunks and took 654.57 s
to embed. The two source hashes differ. This is evidence of high embedding
work, not proof of duplicated text. In contrast, an 18,354,080-byte HWP
produced 36 chunks and completed its host cycle in 21.99 s. File size alone
is therefore a poor admission or performance predictor.

The active embedding service is **local HTTP TEI/BGE-M3**, not a hosted API.
The client sends sequential batches of 16 texts. The TEI container is capped
at four CPUs; during an active large HWP it used about 382% CPU. The Windows
host has six physical cores / twelve logical processors. The host worker
handles one claim synchronously and retains the decryptor mutex through the
application's indexing response and cleanup. Thus both model compute and
serial delivery constrain batch throughput. Increasing HTTP timeout from
30 to 120 seconds prevents one observed timeout, but does not speed inference.

The OCR-off cohort at this snapshot contains 62 completed files, of which
53 are HWP; HWP contributes 97.7% of that cohort's embedding time. That
composition does not demonstrate that switching OCR off increased HWP cost.
HWP parsing was already independent of page/image OCR in this route. A paired
synthetic scanned-PDF parser test showed OCR off was faster but yielded zero
searchable text; it does not measure end-to-end HWP speed.

## Improvement sequence

1. **Explain HWP chunk production and embedding request cost.** For the two
   1,143-chunk HWP jobs and other top-ten slow jobs, emit private aggregate
   metrics only: normalized block count/type, heading boundaries, chunk length
   distribution, repeated-content hash frequency, number of TEI requests,
   tokens per request, TEI wait/inference time, and vector count. Check whether
   128-token chunk budget, atomic blocks, repeated headers, or overlapping text
   causes unnecessary chunks. Do not merge across provenance boundaries or
   silently remove searchable content.
2. **Benchmark model throughput on identical extracted text.** When the live
   worker is idle, replay an opaque, locally retained sample from heavy HWP
   documents without changing the live index. Compare client batch sizes
   8/16/32, TEI token batch limits, and four versus available additional CPUs.
   Measure vectors/s, reported tokens/s, p95 request latency, timeouts,
   RAM/swap, and vector equivalence. A faster setting is accepted only if
   vectors retain the same dimension and retrieval checks pass. CPU tuning
   alone is not assumed to deliver the required ~13x end-to-end gain.
3. **Measure higher-capacity embedding options.** Benchmark BGE-M3 on a
   suitable GPU or dedicated inference host before any production switch.
   This workstation has a 4 GB GTX 1080; model fit and WSL inference support
   must be tested rather than assumed. An external HTTPS embedding provider
   would require an explicit data-handling decision and a full reindex because
   model vectors cannot be mixed. The representative 200 MB sample needs at
   least 20 MB/min end-to-end; for the completed sample's token density,
   embedding alone would need roughly 5.8k reported tokens/s to fit 600 s.
   The current completed sample averages about 514 reported tokens/s during
   embedding. These are planning estimates, not hardware promises.
4. **Reduce chunk work only where content-preserving evidence supports it.**
   Replay the same normalized HWP blocks with current 128-token chunking and
   candidate larger budgets; compare chunk count, total embedded tokens,
   heading/source locators, exact search hits, and retrieval recall. Test
   content-hash vector caching only for truly identical model inputs. Keep a
   candidate only if it lowers time without hiding content or breaking
   document navigation. Larger chunks may be slower on a transformer and are
   not an automatic fix.
5. **Pipeline overlap after embedding capacity is available.** Release the
   global decryptor mutex immediately after verified decryption, then permit
   bounded in-flight deliveries with distinct job directories and fencing.
   Preserve signed claims, source/executable hashes, per-job plaintext cleanup,
   cleanup receipts, and exact active chunk-set checks. Start with two
   concurrent jobs and scale only while TEI, MySQL, ES, and memory stay healthy.
   Parallel requests against an already saturated four-CPU model will not
   deliver the required throughput by themselves.
6. **Remove retry overhead.** Diagnose and fix the PPTX normalization cluster,
   PDF normalization failures, HWPX structural failure, and the type mismatch
   before the final speed trial. Record the phase and elapsed time of every
   attempt. A failed or unsearchable file prevents passing the batch target.

## Acceptance test

- Freeze a representative real 200 MB set from this source, including heavy
  HWP, PDF, PPTX, XLSX, and small documents. Record encrypted and decrypted
  bytes, text/token/chunk distribution, and an opaque manifest hash privately.
- Run the full arrival-to-searchable path three times on isolated clean
  indexes with OCR disabled. Preserve source files and record every job's
  stage and host wall times. All three runs must finish within 600 s, with
  zero failed/missing files and completed host/container cleanup.
- Verify expected versus actual active ES chunk counts and source version
  bindings, representative search hits and locators, source fingerprints,
  and no plaintext residue. Report bottleneck shares and any regressions.
- If the measured run misses 600 s, keep the target open and size embedding
  capacity from the observed token throughput. Do not infer success from a
  single small file or a parser-only OCR benchmark.

Evidence lives in ignored `.local/kordoc-t-drive-cutover` CSV/JSON records;
the collector adds wall-clock spans and monotonic-versus-wall gaps to expose
clock anomalies rather than concealing them.
