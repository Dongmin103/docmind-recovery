# Remaining live-ingestion validation — 2026-09-28

Times in this report use Asia/Seoul. This is the isolated Windows development
stack. The All-in-One source roots were read only. The user approved the three
legacy DOC test files under `home-test1-e2e` and one under `dept-1-e2e` for
decryption and indexing. No source original, plaintext, credential, cookie, or
chunk text is included here.

## Baseline and scope

- The eight development containers were running. The generationless E2E
  readiness check returned `ready=true`, no blockers, and zero remaining
  Windows plaintext jobs before the new run.
- All three host roots were accessible without traversal errors. Read-only
  inventory counted 5 files/9 directories in TEST1, 382 files/93 directories
  in DEPT_2, and 3 files/0 directories in DEPT_1. The four approved DOC files
  matched the encrypted sample's first 16 bytes, but had different full
  ciphertext hashes. Their full hashes were recorded before the run for
  post-run integrity comparison.
- Each of the three logical sources had an explicit, enabled default folder.
  TEST1 and DEPT_1 began disabled with zero documents; DEPT_2 began enabled
  with one complete approved document. The extra `live-fixture-20260928`
  source remained disabled with its historical document tombstoned.
- The existing Host Worker and Source Watcher tasks were running with an
  `Interactive` task principal. The Asia/Seoul midnight reconciliation task
  was registered but disabled. This task principal does not establish
  operation after logout. No reboot or logout test was attempted while
  ingestion jobs were active.

## Code and controlled tests

- `SourceVersion.content_sha256` had remained null after successful ingestion
  even though the job retained the validated plaintext SHA-256. Activation
  now copies that verified hash into the activated source version in the same
  database transaction. A synthetic two-content A-to-B test checks that A
  remains active while B parses, then A becomes retained, B becomes active,
  and their verified content hashes differ.
- The Windows watcher now accepts `-SourceId` to limit both static observations
  and discovery scans to exactly one configured source. The default task call
  without this argument retains its existing scope. An unknown or duplicate
  selected source ID is rejected.
- A failed discovery scan now backs off for 300 seconds by default. A new
  file event or fallback timer cannot bypass that deadline. Future file-read
  failures record only a safe file-kind category and HRESULT, without a path
  or filename. This was added after the first live failures, so their exact
  file kind and OS error cannot be reconstructed.
- Host worker self-test passed, including selected-source filtering and a
  synthetic event at five seconds that cannot bypass the 300-second backoff.
  In a disposable app container, the ingestion and reconciliation service
  suites passed 47/47, and Ruff passed on the changed Python files.
- An earlier command to read the ignored live host configuration, write a
  temporary fixture-only configuration, and invoke a manual scan was rejected
  before execution by automatic command review with only `blocked by policy`;
  no more specific reason was provided. It created no configuration or scan.
  The run instead used the existing host configuration with the explicit
  `-SourceId` scan/watch boundary above.

## TEST1 staged scan and ingestion

- TEST1 alone was enabled after confirming its default folder and zero
  outstanding jobs. Its first manual signed full scan completed with five
  entries and provisioned exactly three DOC documents. The two other entries
  are All-in-One management files and did not create ingestion jobs.
- A temporary watcher invocation with `-EnableDiscovery -SourceId
  home-test1-e2e` generated only TEST1 scans: the scan counts for DEPT_1 and
  DEPT_2 stayed at zero. Four follow-up TEST1 scans failed with the former
  generic `SOURCE_FILE_READ_FAILED` code. They granted no deletion authority.
  A separate read-only hash check could then read all five files, and a later
  manual TEST1 scan completed with five entries, creating three jobs. The
  original host worker processed them one at a time.
- The first document completed and activated 105 chunks with both cleanup
  sides `COMPLETE`. Its parser ended `READY_WITH_WARNING` and included
  `PARSER_SURYA_UNAVAILABLE`; this is a degraded OCR result. The first Office
  media OCR call exceeded the Surya service's 660-second media watchdog.
  Docker recorded exit code 124 and restarted the Surya container. It was
  not OOM-killed. The exact reason inference stalled before that watchdog is
  still unknown.
- After the restart, Surya health became ready and reported verified GPU
  execution. The second TEST1 document completed with 146 chunks, both
  cleanup sides `COMPLETE`, and `READY_WITH_WARNING` including
  `SURYA_MEDIA_EMPTY`. Its decrypted SHA-256 differs from the first
  document's SHA-256; this confirms two approved ciphertexts with different
  plaintext content. They are still **different document paths**: this run
  does not prove replacement of one logical path with different content.
- The third TEST1 document also completed with 146 chunks, both cleanup
  sides `COMPLETE`, and `READY_WITH_WARNING` including `SURYA_MEDIA_EMPTY`.
  After the watchdog restart, Surya had 93 successful HTTP responses, no
  second media timeout, and no second container restart during these two
  documents. The three TEST1 jobs were serial; all three versions were
  active, with no in-progress or claimable job, Windows plaintext job, or
  pending receipt. Terminal jobs retain their historical lease fields in the
  current schema; those fields do not make them claimable again.
- The browser selected-document search for the first active TEST1 document
  returned five results from eight candidates, all labelled TEST1. The TEST1
  parent-folder search after the first two activations returned five results
  from sixteen candidates, and after the third activation five results from
  twenty-four candidates, again all labelled TEST1. Search availability is
  separate from the first document's OCR quality warning.
- One TEST1 document is nested below a deep directory sequence resembling a
  client-side physical path. Read-only traversal confirmed this is the
  actual relative directory structure below the source root, which the
  current projection faithfully displays. Whether All-in-One presents a
  shorter user-facing path is unverified; no display mapping was invented.
- The original four DOC ciphertext hashes, sizes, and modification times
  still matched the pre-run values after all three TEST1 jobs completed.

## DEPT_1 staged scan

- After all TEST1 jobs reached `COMPLETE` and both cleanup states were
  `COMPLETE`, only the app container was restarted with the original four
  Compose overlays; no image rebuild or infrastructure/sidecar restart was
  needed. Readiness again returned `ready=true`, zero Windows plaintext
  jobs, and the approved app image. The frontend returned HTTP 200.
- DEPT_1 was then enabled after verifying its default folder and zero active
  ingestion jobs. Two signed manual scans targeted only `dept-1-e2e`, and
  each completed with three entries: one approved DOC and two All-in-One
  management files. Exactly one document/job was created. The job completed;
  both host and parser cleanup states are `COMPLETE`, the version is `ACTIVE`,
  and the parser is `READY_WITH_WARNING` with 146 chunks. Its warning codes
  are `DOCX_GEOMETRY_UNAVAILABLE`, `LEGACY_DOC_CONVERTED_TO_DOCX`, and
  `SURYA_MEDIA_EMPTY`. After the app-only restart, the activated version's
  content SHA-256 is present and equals the job's verified plaintext SHA-256.
  No hash value or plaintext is copied into this report. All four approved
  original DOC hashes, lengths, and modification times remain unchanged.
- In the browser, selected-document and DEPT_1 parent-folder searches for
  `설치 매뉴얼` each returned five results from eight candidates. Every shown
  result was labelled DEPT_1; none was labelled TEST1 or DEPT_2. These are
  observations while DEPT_1 was temporarily enabled. The warning state still
  limits claims about complete OCR coverage.

## Restored state

- After the browser checks, TEST1 and DEPT_1 were restored to their initial
  disabled state. DEPT_2 remained enabled. The four new document records,
  four active source versions, and four completed jobs remain for audit, with
  zero nonterminal jobs. Disabled sources are excluded by the current source
  projection, so the browser search results above describe the test window,
  not continued visibility after restoration.
- The final generationless E2E readiness check, using the previously approved
  app image digest, returned `ready=true`, no blockers, and zero remaining
  Windows plaintext jobs. The running app container matched that image;
  MySQL, Elasticsearch, Redis, and MinIO were healthy. The Windows job tree
  had zero files and directories and no pending cleanup receipts. Surya was
  running and healthy with one recorded watchdog restart, not OOM-killed.
- The scheduled Host Worker, Source Watcher, and WSL KeepAlive were running;
  the Asia/Seoul midnight reconciliation task remained disabled. All four
  task principals were still `Interactive`. No new scheduled watcher was
  registered for TEST1 or DEPT_1.

## Remaining gates

- The existing disabled fixture source has no surviving fixture-only host
  config or root. A live A-to-B replacement at one isolated logical path is
  still untested. The synthetic A-to-B service test does not substitute for it.
- Do not enable unbounded discovery or the midnight task for DEPT_2 while
  its 382 files are outside this approved ingestion set. Actual Asia/Seoul
  midnight execution remains unobserved. The SQLite reconciliation suite
  covers failed/partial scans, stale schedule fences, bounded retry, and
  authoritative completion.
- Service-account execution after logout and reboot remains unverified. All
  current DocMind scheduled tasks use `Interactive` logon, and the WSL
  KeepAlive task shares that dependency. Before any later host restart test,
  recheck that no job is active and all plaintext cleanup has completed.
