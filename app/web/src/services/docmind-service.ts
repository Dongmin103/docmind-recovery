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

export interface DocMindDocumentOption {
  id: string;
  name: string;
  folder_id: string;
  relative_path: string;
  source_version_id?: string;
  chunk_set_id?: string;
}

export interface DocMindFolderCatalog {
  project_id?: string;
  dataset_id: string;
  folders: DocMindFolder[];
  documents: DocMindDocumentOption[];
  hierarchical?: boolean;
  initialized?: boolean;
  can_administer?: boolean;
  can_upload?: boolean;
  source_sync?: boolean;
  workspace_mode?: 'authenticated' | 'shared';
}

export interface DocMindSearchResult {
  dataset_id: string;
  chunks: ITestingChunk[];
  ranked_chunks?: ITestingChunk[];
  ranked_total?: number;
  total: number;
  candidate_count: number;
  caps?: {
    candidates: number;
    per_document: number;
    results: number;
  };
}

export type DocMindSearchScope =
  | { mode: 'all' }
  | { mode: 'folders'; folderIds: string[] }
  | { mode: 'documents'; documentIds: string[] };

export interface DocMindSearchRequest {
  question: string;
  projectId?: string;
  scope: DocMindSearchScope;
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
  source_format: 'pdf' | 'doc' | 'docx' | 'xls' | 'xlsx' | 'pptx' | 'hwp' | 'hwpx';
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
  registrations: DocMindRegistration[];
}

export interface DocMindHierarchyNode {
  file_id: string;
  parent_file_id?: string;
  name: string;
  relative_path: string;
  depth: number;
  type: 'folder' | 'file';
  child_count?: number;
  source_enabled?: boolean;
  document_id?: string;
  document_exists: boolean;
  index_state?:
    | 'INDEXED'
    | 'PENDING'
    | 'PROCESSING'
    | 'FAILED'
    | 'CLEANUP'
    | 'CLEANUP_FAILED'
    | 'RETRY_WAIT'
    | 'ACTION_REQUIRED';
  index_cleanup_state?: 'PENDING' | 'FAILED' | 'COMPLETE' | null;
  index_error_code?: string | null;
  searchable?: boolean;
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
  projectId,
  scope,
}: DocMindSearchRequest) => {
  const data: Record<string, unknown> = {
    question,
    scope:
      scope.mode === 'folders'
        ? { mode: scope.mode, folder_ids: scope.folderIds }
        : scope.mode === 'documents'
          ? { mode: scope.mode, document_ids: scope.documentIds }
          : { mode: scope.mode },
  };
  if (projectId) data.project_id = projectId;
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

export const retryDocMindRegistration = (
  registrationId: string,
  idempotencyKey: string,
) =>
  request.post(`/api/v1/docmind/admin/registrations/${registrationId}/retry`, {
    data: {},
    headers: { 'Idempotency-Key': idempotencyKey },
  });
