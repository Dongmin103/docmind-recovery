export type PreviewStatus =
  | 'QUEUED'
  | 'DECRYPTING'
  | 'PROCESSING'
  | 'READY'
  | 'FAILED'
  | 'CANCELLED'
  | 'EXPIRED'
  | 'CLEANUP_FAILED';

export interface PreviewSession {
  preview_id: string;
  status: PreviewStatus;
  source_format: string;
  display_format: string;
  viewer_kind: 'pdf' | 'word' | 'powerpoint' | 'excel' | 'hwp';
  page_count?: number;
  source_version_id: string;
  chunk_set_id?: string;
  chunk_set_changed?: boolean;
  expires_at?: string;
  error_code?: string;
}

export interface CreatedPreview extends PreviewSession {
  preview_token: string;
}
