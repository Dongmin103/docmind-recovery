export interface CreatePreviewRequest {
  source_version_id: string;
  chunk_set_id: string;
  idempotency_key: string;
}
