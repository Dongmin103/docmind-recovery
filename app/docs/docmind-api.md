# DOCMIND HTTP API

All endpoints below require the existing DOCMIND login session and tenant
authorization. A deployment may additionally enable its shared workspace session.
Server source registration and the Windows host worker use a separate signed
protocol; browser sessions cannot supply arbitrary physical filesystem paths.

## Document catalog

`GET /api/v1/docmind/folders` returns accessible folders, documents, and the
current catalog version.

## Search

`POST /api/v1/docmind/search` accepts a question and an explicit scope:

```json
{
  "question": "문서 검색어",
  "scope": { "mode": "all" }
}
```

Other scopes are `{"mode":"folders","folder_ids":["<folder-id>"]}` and
`{"mode":"documents","document_ids":["<document-id>"]}`. IDs must come
from the authorized catalog. Responses use the existing `code`, `data`, and
`message` envelope and return at most five ranked chunks. Both recall lanes
apply the same source, version, and authorization scope.

## Grounded answer jobs

`POST /api/v1/docmind/answer/jobs` accepts `question`, `scope`, and a client
generated 32-character hexadecimal `request_id`; `project_id` and
`neighbor_window` are optional. HTTP 202 returns a job receipt.

`GET /api/v1/docmind/answer/jobs/<job-id>` polls the authorized job and returns
`queued`, `running`, `completed`, or `failed`. A completed job includes its
verified answer and citations. The same request ID and identical payload reuse
the original job within its retention window. Source validity is checked again
when the result is retrieved.

API keys, host HMAC secrets, document contents, and generated answer payloads
must not be placed in repository examples or diagnostic logs.
