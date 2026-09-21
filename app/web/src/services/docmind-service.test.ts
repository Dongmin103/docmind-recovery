jest.mock('@/utils/request', () => ({
  __esModule: true,
  default: {
    delete: jest.fn(),
    get: jest.fn(),
    patch: jest.fn(),
    post: jest.fn(),
  },
}));

import request from '@/utils/request';
import {
  captureDocMindHierarchyDraft,
  changeDocMindDraft,
  createDocMindDraft,
  createDocMindHierarchyFolder,
  deleteDocMindCatalogVersion,
  deleteDocMindHierarchyFolder,
  generateDocMindDraft,
  getDocMindCatalogVersions,
  getDocMindDrafts,
  getDocMindFolders,
  getDocMindHierarchy,
  getDocMindImportJobs,
  getDocMindRegistrations,
  importDocMindLocalFolder,
  publishDocMindCatalogVersion,
  registerDocMindDocuments,
  retryDocMindRegistration,
  rollbackDocMindCatalogVersion,
  searchDocMind,
  updateDocMindHierarchyFolder,
} from './docmind-service';

const mockGet = request.get as unknown as jest.Mock;
const mockPost = request.post as unknown as jest.Mock;
const mockDelete = request.delete as unknown as jest.Mock;
const mockPatch = request.patch as unknown as jest.Mock;

describe('DocMind service contract', () => {
  beforeEach(() => jest.clearAllMocks());

  it('loads the current serving folder catalog', () => {
    getDocMindFolders();

    expect(mockGet).toHaveBeenCalledWith('/api/v1/docmind/folders');
  });

  it('omits manual fields for automatic search', () => {
    searchDocMind({ question: 'CAPA' });

    expect(mockPost).toHaveBeenCalledWith('/api/v1/docmind/search', {
      data: { question: 'CAPA' },
    });
  });

  it('maps manual scope to the backend request contract', () => {
    searchDocMind({
      question: 'validation',
      folderIds: ['validation', 'quality-operations'],
      catalogVersionId: 'static-version',
    });

    expect(mockPost).toHaveBeenCalledWith('/api/v1/docmind/search', {
      data: {
        question: 'validation',
        folder_ids: ['validation', 'quality-operations'],
        catalog_version_id: 'static-version',
      },
    });
  });

  it('uploads one bounded folder registration batch as multipart data', () => {
    const files = [new File(['one'], 'one.pdf'), new File(['two'], 'two.pdf')];

    registerDocMindDocuments('validation', files);

    const options = mockPost.mock.calls[0][1];
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/registrations',
      expect.objectContaining({ data: expect.any(FormData) }),
    );
    expect(options.data.get('folder_id')).toBe('validation');
    expect(options.data.getAll('file')).toEqual(files);
  });

  it('polls registration status and retries with an idempotency key', () => {
    getDocMindRegistrations(['INDEXING', 'FAILED']);
    retryDocMindRegistration('registration-1', 'retry-key-1');

    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/registrations',
      { params: { state: ['INDEXING', 'FAILED'] } },
    );
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/registrations/registration-1/retry',
      {
        data: {},
        headers: { 'Idempotency-Key': 'retry-key-1' },
      },
    );
  });

  it('imports a local folder with browser relative paths and one idempotency key', () => {
    const file = new File(['pdf'], 'protocol.pdf');
    Object.defineProperty(file, 'webkitRelativePath', {
      value: 'Validation/Cleaning/protocol.pdf',
    });

    getDocMindHierarchy();
    getDocMindImportJobs();
    importDocMindLocalFolder([file], 'import-key');

    expect(mockGet).toHaveBeenCalledWith('/api/v1/docmind/admin/hierarchy');
    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/hierarchy/imports',
    );
    const options = mockPost.mock.calls[0][1];
    expect(options.data.getAll('path')).toEqual([
      'Validation/Cleaning/protocol.pdf',
    ]);
    expect(options.data.getAll('file')).toEqual([file]);
    expect(options.headers).toEqual({ 'Idempotency-Key': 'import-key' });
  });

  it('manages the source tree and captures a non-serving hierarchy draft', () => {
    createDocMindHierarchyFolder('root-file', 'Validation');
    updateDocMindHierarchyFolder('folder-file', {
      parentFileId: 'root-file',
      name: 'Validation Updated',
    });
    deleteDocMindHierarchyFolder('empty-folder');
    captureDocMindHierarchyDraft('active-v1', 'capture-key');

    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/hierarchy/folders',
      { data: { parent_file_id: 'root-file', name: 'Validation' } },
    );
    expect(mockPatch).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/hierarchy/folders/folder-file',
      {
        data: {
          parent_file_id: 'root-file',
          name: 'Validation Updated',
        },
      },
    );
    expect(mockDelete).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/hierarchy/folders/empty-folder',
    );
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/hierarchy/capture',
      {
        data: { expected_active_version_id: 'active-v1' },
        headers: { 'Idempotency-Key': 'capture-key' },
      },
    );
  });

  it('creates and changes a parent-bound non-serving draft', () => {
    getDocMindDrafts();
    createDocMindDraft('version-v0', 'draft-key-1');
    changeDocMindDraft(
      'draft-1',
      {
        operation: 'ADD',
        documentId: 'document-1',
        registrationId: 'registration-1',
        toFolderId: 'validation',
      },
      'change-key-1',
    );
    generateDocMindDraft('draft-1');

    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/catalog/drafts',
    );
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/catalog/drafts',
      {
        data: { expected_parent_version_id: 'version-v0' },
        headers: { 'Idempotency-Key': 'draft-key-1' },
      },
    );
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/catalog/drafts/draft-1/changes',
      {
        data: {
          operation: 'ADD',
          document_id: 'document-1',
          registration_id: 'registration-1',
          from_folder_id: undefined,
          to_folder_id: 'validation',
          expected_parent_folder_id: undefined,
        },
        headers: { 'Idempotency-Key': 'change-key-1' },
      },
    );
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/catalog/drafts/draft-1/generate',
      { data: {} },
    );
  });

  it('lists, publishes, and rolls back immutable Catalog versions', () => {
    getDocMindCatalogVersions();
    publishDocMindCatalogVersion('ready-1', 'v0', 'publish-key');
    rollbackDocMindCatalogVersion('v0', 'ready-1', 'rollback-key');

    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/catalog/versions',
    );
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/catalog/drafts/ready-1/publish',
      {
        data: { expected_active_version_id: 'v0' },
        headers: { 'Idempotency-Key': 'publish-key' },
      },
    );
    expect(mockPost).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/catalog/v0/rollback',
      {
        data: { expected_active_version_id: 'ready-1' },
        headers: { 'Idempotency-Key': 'rollback-key' },
      },
    );
  });

  it('deletes a failed version with active and label compare-and-set guards', () => {
    deleteDocMindCatalogVersion('failed-1', 'active-1', 'DRAFT-FAILED');

    expect(mockDelete).toHaveBeenCalledWith(
      '/api/v1/docmind/admin/catalog/versions/failed-1',
      {
        data: {
          expected_active_version_id: 'active-1',
          expected_version_label: 'DRAFT-FAILED',
        },
      },
    );
  });
});
