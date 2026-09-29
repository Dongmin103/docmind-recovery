# DEPT_1 Excel automatic ingestion validation — 2026-09-28

## Scope and outcome

The user moved the current cloud-upload test target from DEPT_2 to DEPT_1 and asked to validate the Excel file uploaded through the S: drive. The existing DEPT_1 discovery watcher had already ingested the file. No duplicate registration, manual re-ingestion, source rename, or changes to DEPT_2 were needed.

The S: file and corresponding encrypted server file were located by matching filename and timestamp. Cryptographic verification against the ingestion records confirmed that the decrypted plaintext matches the S: file and the ciphertext matches the server file. File hashes were checked again after the audit and remained unchanged.

## Evidence

| Check | Result |
| --- | --- |
| Source | `dept-1-e2e` |
| Document ID | `244efabc016740eaa030db8206f6fe07` |
| Ingestion job | `6238f118424c60950f04ced86c3d5c95` |
| Job state / attempts | `COMPLETE` / 1 |
| Source version | `ACTIVE`, search cleanup complete |
| Plaintext size | 19,812 bytes; SHA-256 matches S: file |
| Ciphertext size | 19,861 bytes; SHA-256 matches server file |
| Parser | Docling 2.115.0, XLSX, `READY` |
| Index | 40 chunks; one task completed, zero failed tasks |
| Host and server cleanup states | Both `COMPLETE` |
| Host plaintext work-root files | 0; only an empty structural preview directory remained |
| Container ephemeral-parser files | 0 |

The job record shows creation at 16:16:55 and completion update at 16:18:16 on 2026-09-28 (local time).

## Live search verification

Authenticated shared-session search API, query `표준 MES 기능 목록`:

| Scope | API code | Candidates | Returned | Target found | Outside DEPT_1 |
| --- | --- | --- | --- | --- | --- |
| Exact Excel document | 0 | 8 | 5 | Yes | 0 |
| DEPT_1 folder | 0 | 16 | 5 | Yes | 0 |

All returned results in both checks referenced the target Excel document. This validates retrieval, not answer-model generation or the visual quality of every extracted spreadsheet cell.

The first session request returned HTTP 502. A subsequent health request and repeated authenticated searches succeeded without a restart or configuration change; the cause of the transient response was not established.

## Boundaries

- This verifies the specific uploaded file traversed S: → encrypted DEPT_1 source → decryption → parsing/indexing → temporary plaintext cleanup → scoped search. It is not a general audit of all drive mappings.
- Temporary plaintext deletion does not mean the searchable extracted chunks or embeddings are deleted; those remain as the search index.
- Original documents were not modified or deleted. No broader source configuration or application deployment was changed during this validation.
- The report excludes document body contents, credentials, and raw file hashes.
