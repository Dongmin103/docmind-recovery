# Surya GPU OCR benchmark — 2026-09-23

## Scope

This is an isolated Windows WSL2 development-host measurement of the Surya 2
GGUF OCR backend. It did not read, decrypt, reprocess, or index any source
document. The parser package was `surya-ocr` 0.22.1, the model revision was
`6a3a4c30e5e74446d4f8b6afd05b2f2da970f470`, and both CPU and GPU runs verified
the mounted model files before parsing. The CUDA implementation is llama.cpp
`b10718`, CUDA 12.4, `sm_61`, with `-ngl 99`; the CPU parser stayed online
throughout the isolated GPU measurement.

The host GPU was an NVIDIA GeForce GTX 1080 (8 GiB) exposed to WSL2. The test
used one request at a time, matching the production parser admission limit.

## Paired distinct-image result

Three non-sensitive 960 by 360 PNG Office-media inputs were generated in
memory. Each contained the same base OCR targets and a distinct visible
synthetic variant label. CPU and GPU received the identical three byte hashes
in the listed order; every request returned non-empty blocks and the expected
base OCR targets.

| Variant SHA-256 | CPU seconds | GPU seconds |
|---|---:|---:|
| `0ac0ff6066be9ec50b429561e168fac5d465d9dad099ddfabd15eecca93d6c16` | 8.551 | 1.851 |
| `41c9c043d0f6ca8d0c085e3ea1ded5e1d37f6381e64b14e4728b619c6dfa08ce` | 7.943 | 1.274 |
| `0ca94f7ffd6a529b3646c2f166d5eb4ee98fa65c0f34e871b39ebd29f88bd387` | 8.121 | 1.333 |
| Median | **8.121** | **1.333** |

The paired median was **6.09 times faster** on GPU, a **83.6% reduction**
in elapsed time.

## First request and repeated-input observations

For the separately measured byte-identical 960 by 360 input, CPU first request
was 40.517 seconds and GPU first request was 10.984 seconds. This includes
lazy engine/model startup and is not a steady-state latency claim. CPU versus
GPU ordering and already-resident OS files can also affect it.

Three immediate repeats of that identical input had CPU median 2.747 seconds
and GPU median 0.558 seconds. llama.cpp reported exact LCP similarity and graph
reuse for these repeats, so the 4.92x repeated-input figure is deliberately not
used as a general OCR throughput result. The distinct-image table is the useful
steady-state comparison in this report.

## GPU execution evidence

After a successful GPU OCR request, the isolated llama-server process had all
of the following observed facts:

- command line requested `-ngl 99`;
- its loaded libraries included `libggml-cuda`, CUDA runtime, cuBLAS and
  `libcuda`, and it held `/dev/dxg`;
- NVML associated its compute PID with that same llama-server process;
- GPU memory rose from about 133 MiB before model execution to 1,951 MiB.

The `b10718` logger at its configured verbosity did not emit the old
`load_tensors: offloaded N/M layers to GPU` line. Therefore
`gpu_offload_verified` remains false and this benchmark does not claim a
verified per-layer count. The runtime health endpoint now reports separately
whether a completed OCR was associated with its CUDA llama-server process.

## Parser contract check outside timed samples

One additional in-memory synthetic request confirmed non-empty OCR, health
responses during inference (maximum observed 0.002 seconds), and rejection of
a concurrent parse with `503 PARSER_SURYA_BUSY`. The isolated parser's `/tmp`
and `/opt/surya/runtime` had no files after the check; the disposable client
container used for the probe was removed.

## Live alias activation

After the isolated execution-health check passed, the live `surya-parser`
alias was recreated with GPU image
`sha256:7128dc32fb5e854ee5e2c8989164834dcd9746c1aecc86827894e78d9a5e3f45`.
The CPU parser was not run in parallel against the same model mount. Before
the first request, health correctly reported CUDA execution as unverified.

One post-activation in-memory synthetic OCR then passed with non-empty output,
health responsive during inference (0.001 seconds), and a second request
rejected as `503 PARSER_SURYA_BUSY`. Health subsequently reported
`gpu_execution_verified=true`, while still reporting `gpu_offload_verified=false`
and null layer counts. No files remained under the parser temporary/runtime
directories after the request.

## Limits and next step

This is a small synthetic Office-media OCR measurement, not a production
document corpus, p50/p95, capacity test, or SLA. It does not measure PDF,
legacy DOC conversion, network transfer, indexing, or source decryption.

GPU acceleration is enabled for the live `surya-parser` alias in this Windows
development environment. The reported result is limited to synthetic
Office-media OCR; measure representative document formats, concurrency, and
p50/p95 before applying it as an operational performance target.
