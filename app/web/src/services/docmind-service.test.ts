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
  getDocMindFolders,
  registerDocMindDocuments,
  retryDocMindRegistration,
  searchDocMind,
} from './docmind-service';

const mockGet = request.get as unknown as jest.Mock;
const mockPost = request.post as unknown as jest.Mock;

describe('DocMind service contract', () => {
  beforeEach(() => jest.clearAllMocks());

  it('sends the explicit all scope without selection arrays', () => {
    searchDocMind({ question: 'CAPA', scope: { mode: 'all' } });

    expect(mockPost).toHaveBeenCalledWith('/api/v1/docmind/search', {
      data: { question: 'CAPA', scope: { mode: 'all' } },
    });
  });

  it('maps folder and document scopes to the wire contract', () => {
    searchDocMind({
      question: 'validation',
      projectId: 'project-1',
      scope: { mode: 'folders', folderIds: ['folder-1', 'folder-2'] },
    });
    searchDocMind({
      question: 'protocol',
      scope: { mode: 'documents', documentIds: ['document-1'] },
    });

    expect(mockPost).toHaveBeenNthCalledWith(1, '/api/v1/docmind/search', {
      data: {
        question: 'validation',
        project_id: 'project-1',
        scope: { mode: 'folders', folder_ids: ['folder-1', 'folder-2'] },
      },
    });
    expect(mockPost).toHaveBeenNthCalledWith(2, '/api/v1/docmind/search', {
      data: {
        question: 'protocol',
        scope: { mode: 'documents', document_ids: ['document-1'] },
      },
    });
  });

  it('keeps folder loading and document registration APIs', () => {
    const file = new File(['one'], 'one.pdf');

    getDocMindFolders();
    registerDocMindDocuments('folder-1', [file]);
    retryDocMindRegistration('registration-1', 'retry-key');

    expect(mockGet).toHaveBeenCalledWith('/api/v1/docmind/folders');
    const upload = mockPost.mock.calls[0][1].data as FormData;
    expect(upload.get('folder_id')).toBe('folder-1');
    expect(upload.getAll('file')).toEqual([file]);
    expect(mockPost).toHaveBeenLastCalledWith(
      '/api/v1/docmind/admin/registrations/registration-1/retry',
      { data: {}, headers: { 'Idempotency-Key': 'retry-key' } },
    );
  });
});
