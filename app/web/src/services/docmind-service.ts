import type { ITestingChunk } from '@/interfaces/database/dataset';
import request from '@/utils/request';

export interface DocMindFolder {
  id: string;
  name: string;
  document_count: number;
  parent_id?: string;
  relative_path?: string;
  depth?: number;
}

export interface DocMindFolderCatalog {
  dataset_id: string;
  catalog_source: 'static' | 'database';
  catalog_version_id: string;
  folders: DocMindFolder[];
  hierarchical?: boolean;
  initialized?: boolean;
  can_administer?: boolean;
  workspace_mode?: 'authenticated' | 'shared';
}

export interface DocMindSearchResult {
  dataset_id: string;
  chunks: ITestingChunk[];
  ranked_chunks?: ITestingChunk[];
  ranked_total?: number;
  total: number;
  candidate_count: number;
  selected_folders: Array<{ id: string; score?: number }>;
  catalog_source: 'static' | 'database';
  catalog_version_id: string;
  scope_mode: 'automatic' | 'manual';
  effective_folder_ids: string[];
  caps: {
    folders: number;
    candidates: number;
    per_folder: number;
    per_document: number;
    results: number;
  };
}

export interface DocMindSearchRequest {
  question: string;
  folderIds?: string[];
  catalogVersionId?: string;
}

export type DocMindRegistrationState =
  | 'UPLOADING'
  | 'UPLOADED'
  | 'INDEX_QUEUED'
  | 'INDEXING'
  | 'INDEXED'
  | 'FAILED'
  | 'CANCELLED';

export interface DocMindParserRun {
  parse_run_id: string;
  chunk_set_id: string;
  active_chunk_set_id?: string;
  active: boolean;
  source_format: 'pdf' | 'docx' | 'xlsx' | 'pptx' | 'hwp' | 'hwpx';
  selection_reason: string;
  parser_name: string;
  parser_version: string;
  model_version?: string;
  backend: string;
  schema_version: string;
  phase: string;
  warnings: string[];
  error_code?: string;
  error_message?: string;
  raw_artifact_ref?: string;
  expected_pages: number;
  completed_pages: number;
  reused_pages: number;
  failed_pages: number;
}

export interface DocMindRegistration {
  registration_id: string;
  document_id: string;
  document_name?: string;
  document_exists: boolean;
  folder_id?: string;
  state: DocMindRegistrationState;
  error_code?: string;
  error_message?: string;
  blocker_code?: string;
  progress: number;
  chunk_count: number;
  index_ready: boolean;
  draft_eligible: boolean;
  active_catalog_member: boolean;
  retry_allowed: boolean;
  is_current: boolean;
  retry_of_id?: string;
  created_at?: string;
  updated_at?: string;
  parser_run?: DocMindParserRun;
}

export interface DocMindRegistrationList {
  project_id: string;
  dataset_id: string;
  catalog_version_id: string;
  registrations: DocMindRegistration[];
}

export type DocMindDraftOperation = 'ADD' | 'REMOVE' | 'MOVE';
export type DocMindReadinessMode = 'SEARCH_VALIDATED' | 'ADMIN_SAVED';

export interface DocMindDraftChange {
  operation: DocMindDraftOperation;
  document_id: string;
  document_name?: string;
  registration_id?: string;
  from_folder_id?: string;
  to_folder_id?: string;
  expected_parent_folder_id?: string;
  ordinal: number;
}

export interface DocMindDraft {
  draft_id: string;
  version_label: string;
  parent_version_id: string;
  lifecycle_state:
    | 'DRAFT'
    | 'GENERATING'
    | 'GENERATED'
    | 'VALIDATING'
    | 'READY'
    | 'FAILED';
  health_state: 'UNVALIDATED' | 'VALID' | 'EXPIRED' | 'INVALID';
  health_reason?: string;
  snapshot_hash: string;
  active_parent_is_current: boolean;
  change_count: number;
  has_effective_changes: boolean;
  generation_progress: number;
  digest_ready_count: number;
  membership_count: number;
  card_ready_count: number;
  can_generate: boolean;
  validation_report_hash?: string;
  validated_at?: string;
  validation_expires_at?: string;
  readiness_mode?: DocMindReadinessMode;
  search_validation_performed?: boolean;
  source_ready_version_id?: string;
  snapshot_schema_version?: number;
  folders: Array<{
    id: string;
    name: string;
    ordinal: number;
    document_count: number;
    parent_id?: string;
    relative_path?: string;
    depth?: number;
  }>;
  changes: DocMindDraftChange[];
  routing_cards?: Array<{
    folder_id: string;
    folder_name: string;
    l0: string;
    l1: string;
    l0_hash?: string;
    l1_hash?: string;
  }>;
  created_at?: string;
  updated_at?: string;
}

export interface DocMindDraftList {
  project_id: string;
  active_version_id: string;
  drafts: DocMindDraft[];
}

export interface DocMindDraftChangeRequest {
  operation: DocMindDraftOperation;
  documentId: string;
  registrationId?: string;
  fromFolderId?: string;
  toFolderId?: string;
  expectedParentFolderId?: string;
}

export interface DocMindCatalogVersion {
  version_id: string;
  version_label: string;
  parent_version_id?: string;
  lifecycle_state:
    | 'DRAFT'
    | 'GENERATING'
    | 'GENERATED'
    | 'VALIDATING'
    | 'READY'
    | 'PUBLISHED'
    | 'SUPERSEDED'
    | 'FAILED';
  health_state: 'UNVALIDATED' | 'VALID' | 'EXPIRED' | 'INVALID';
  health_reason?: string;
  membership_count: number;
  active: boolean;
  publish_allowed: boolean;
  rollback_allowed: boolean;
  delete_allowed: boolean;
  delete_blocker?: string;
  deletion_pending?: boolean;
  validation_report_hash?: string;
  validated_at?: string;
  validation_expires_at?: string;
  readiness_mode?: DocMindReadinessMode;
  search_validation_performed?: boolean;
  source_ready_version_id?: string;
  routing_cards?: Array<{
    folder_id: string;
    folder_name: string;
    relative_path?: string;
    l0: string;
    l1: string;
  }>;
}

export interface DocMindManualRoutingCard {
  folder_id: string;
  l0: string;
  l1: string;
}

export interface DocMindManualCardRevisionRequest {
  sourceReadyVersionId: string;
  expectedActiveVersionId: string;
  expectedSourceSnapshotHash: string;
  cards: DocMindManualRoutingCard[];
}

export interface DocMindManualCardRevisionResponse {
  draft_id: string;
  version_label: string;
  parent_version_id: string;
  source_ready_version_id: string;
  lifecycle_state: 'READY';
  health_state: 'VALID';
  snapshot_hash: string;
  routing_card_set_hash: string;
  root_identity_sha256: string;
  manual_save_report_hash: string;
  readiness_mode: 'ADMIN_SAVED';
  search_validation_performed: false;
}

export interface DocMindCatalogVersionList {
  project_id: string;
  active_version_id: string;
  catalog_source_mode: 'static' | 'database';
  versions: DocMindCatalogVersion[];
}

export interface DocMindHierarchyNode {
  file_id: string;
  parent_file_id?: string;
  name: string;
  relative_path: string;
  depth: number;
  type: 'folder' | 'file';
  child_count?: number;
  document_id?: string;
  document_exists: boolean;
  index_state?: 'INDEXED' | 'PENDING';
  semantic_folder_id?: string;
  mutation_capabilities?: {
    can_create_child: boolean;
    can_edit: boolean;
    can_delete: boolean;
    edit_blocker_code: string | null;
    delete_blocker_code: string | null;
  };
}

export interface DocMindFolderMutationResult {
  folder_id: string;
  name?: string;
  parent_file_id?: string;
  deleted?: boolean;
}

export interface DocMindHierarchy {
  project_id: string;
  dataset_id: string;
  source_root_file_id: string;
  nodes: DocMindHierarchyNode[];
}

export interface DocMindImportItem {
  relative_path: string;
  state: 'RUNNING' | 'SUCCEEDED' | 'FAILED';
  error_code?: string;
  file_id?: string;
  document_id?: string;
  registration_id?: string;
}

export interface DocMindImportJob {
  job_id: string;
  state: 'RUNNING' | 'COMPLETED' | 'PARTIAL' | 'FAILED';
  item_count: number;
  succeeded_count: number;
  failed_count: number;
  items: DocMindImportItem[];
}

export const getDocMindFolders = () => request.get('/api/v1/docmind/folders');

export const bootstrapDocMindWorkspace = () =>
  request.post('/api/v1/docmind/bootstrap');

export const searchDocMind = ({
  question,
  folderIds,
  catalogVersionId,
}: DocMindSearchRequest) => {
  const data: Record<string, unknown> = { question };
  if (folderIds?.length) {
    data.folder_ids = folderIds;
    data.catalog_version_id = catalogVersionId;
  }
  return request.post('/api/v1/docmind/search', { data });
};

export const registerDocMindDocuments = (folderId: string, files: File[]) => {
  const data = new FormData();
  data.append('folder_id', folderId);
  files.forEach((file) => data.append('file', file));
  return request.post('/api/v1/docmind/admin/registrations', { data });
};

export const getDocMindRegistrations = (states?: DocMindRegistrationState[]) =>
  request.get('/api/v1/docmind/admin/registrations', {
    params: states?.length ? { state: states } : undefined,
  });

export const getDocMindHierarchy = () =>
  request.get('/api/v1/docmind/admin/hierarchy');

export const getDocMindImportJobs = () =>
  request.get('/api/v1/docmind/admin/hierarchy/imports');

export const importDocMindLocalFolder = (
  files: File[],
  idempotencyKey: string,
) => {
  const data = new FormData();
  files.forEach((file) => {
    data.append('path', file.webkitRelativePath || file.name);
    data.append('file', file);
  });
  return request.post('/api/v1/docmind/admin/hierarchy/imports', {
    data,
    headers: { 'Idempotency-Key': idempotencyKey },
  });
};

export const retryDocMindLocalFolderImport = (
  jobId: string,
  files: File[],
  idempotencyKey: string,
) => {
  const data = new FormData();
  files.forEach((file) => {
    data.append('path', file.webkitRelativePath || file.name);
    data.append('file', file);
  });
  return request.post(
    `/api/v1/docmind/admin/hierarchy/imports/${jobId}/retry`,
    { data, headers: { 'Idempotency-Key': idempotencyKey } },
  );
};

export const createDocMindHierarchyFolder = (
  parentFileId: string,
  name: string,
) =>
  request.post('/api/v1/docmind/admin/hierarchy/folders', {
    data: { parent_file_id: parentFileId, name },
  });

export const updateDocMindHierarchyFolder = (
  folderId: string,
  update: { parentFileId?: string; name?: string },
) =>
  request.patch(`/api/v1/docmind/admin/hierarchy/folders/${folderId}`, {
    data: {
      ...(update.parentFileId ? { parent_file_id: update.parentFileId } : {}),
      ...(update.name ? { name: update.name } : {}),
    },
  });

export const deleteDocMindHierarchyFolder = (folderId: string) =>
  request.delete(`/api/v1/docmind/admin/hierarchy/folders/${folderId}`);

export const captureDocMindHierarchyDraft = (
  expectedActiveVersionId: string,
  idempotencyKey: string,
) =>
  request.post('/api/v1/docmind/admin/hierarchy/capture', {
    data: { expected_active_version_id: expectedActiveVersionId },
    headers: { 'Idempotency-Key': idempotencyKey },
  });

export const retryDocMindRegistration = (
  registrationId: string,
  idempotencyKey: string,
) =>
  request.post(`/api/v1/docmind/admin/registrations/${registrationId}/retry`, {
    data: {},
    headers: { 'Idempotency-Key': idempotencyKey },
  });

export const getDocMindDrafts = () =>
  request.get('/api/v1/docmind/admin/catalog/drafts');

export const getDocMindDraft = (draftId: string) =>
  request.get(`/api/v1/docmind/admin/catalog/drafts/${draftId}`);

export const createDocMindDraft = (
  expectedParentVersionId: string,
  idempotencyKey: string,
) =>
  request.post('/api/v1/docmind/admin/catalog/drafts', {
    data: { expected_parent_version_id: expectedParentVersionId },
    headers: { 'Idempotency-Key': idempotencyKey },
  });

export const changeDocMindDraft = (
  draftId: string,
  change: DocMindDraftChangeRequest,
  idempotencyKey: string,
) =>
  request.post(`/api/v1/docmind/admin/catalog/drafts/${draftId}/changes`, {
    data: {
      operation: change.operation,
      document_id: change.documentId,
      registration_id: change.registrationId,
      from_folder_id: change.fromFolderId,
      to_folder_id: change.toFolderId,
      expected_parent_folder_id: change.expectedParentFolderId,
    },
    headers: { 'Idempotency-Key': idempotencyKey },
  });

export const generateDocMindDraft = (draftId: string) =>
  request.post(`/api/v1/docmind/admin/catalog/drafts/${draftId}/generate`, {
    data: {},
  });

export const createDocMindManualCardRevision = (
  revision: DocMindManualCardRevisionRequest,
  idempotencyKey: string,
) =>
  request.post(
    `/api/v1/docmind/admin/catalog/drafts/${revision.sourceReadyVersionId}/manual-card-revisions`,
    {
      data: {
        expected_active_version_id: revision.expectedActiveVersionId,
        expected_source_snapshot_hash: revision.expectedSourceSnapshotHash,
        cards: revision.cards,
      },
      headers: { 'Idempotency-Key': idempotencyKey },
    },
  );

export const getDocMindCatalogVersions = () =>
  request.get('/api/v1/docmind/admin/catalog/versions');

export const publishDocMindCatalogVersion = (
  versionId: string,
  expectedActiveVersionId: string,
  idempotencyKey: string,
) =>
  request.post(`/api/v1/docmind/admin/catalog/drafts/${versionId}/publish`, {
    data: { expected_active_version_id: expectedActiveVersionId },
    headers: { 'Idempotency-Key': idempotencyKey },
  });

export const rollbackDocMindCatalogVersion = (
  versionId: string,
  expectedActiveVersionId: string,
  idempotencyKey: string,
) =>
  request.post(`/api/v1/docmind/admin/catalog/${versionId}/rollback`, {
    data: { expected_active_version_id: expectedActiveVersionId },
    headers: { 'Idempotency-Key': idempotencyKey },
  });

export const deleteDocMindCatalogVersion = (
  versionId: string,
  expectedActiveVersionId: string,
  expectedVersionLabel: string,
) =>
  request.delete(`/api/v1/docmind/admin/catalog/versions/${versionId}`, {
    data: {
      expected_active_version_id: expectedActiveVersionId,
      expected_version_label: expectedVersionLabel,
    },
  });
