import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

const mockSearchMutate = jest.fn();
const mockRefetch = jest.fn();
let mockCanAdminister = false;
let mockFolderError = false;
let mockHierarchyError = false;
let mockRegistrationError = false;
let mockSearchResult: any;
let mockSearchVariables: any;
let mockDocumentOptions: any[];
let mockHierarchyDocumentId = 'document-1';

jest.mock('@tanstack/react-query', () => ({
  useMutation: (options: { mutationKey?: string[] }) => ({
    data:
      options.mutationKey?.[0] === 'docmind-search'
        ? mockSearchResult
        : undefined,
    variables:
      options.mutationKey?.[0] === 'docmind-search'
        ? mockSearchVariables
        : undefined,
    error: undefined,
    isError: false,
    isPending: false,
    mutate:
      options.mutationKey?.[0] === 'docmind-search'
        ? mockSearchMutate
        : jest.fn(),
  }),
  useQuery: (options: { queryKey?: string[] }) => {
    if (options.queryKey?.[0] === 'docmind-folders') {
      return {
        data: {
          project_id: 'project-1',
          dataset_id: 'dataset-1',
          can_administer: mockCanAdminister,
          can_upload: false,
          source_sync: true,
          documents: mockDocumentOptions,
          folders: [
            { id: 'folder-1', name: '품질', document_count: 1 },
            { id: 'folder-2', name: '밸리데이션', document_count: 1 },
          ],
        },
        isError: mockFolderError,
        refetch: mockRefetch,
      };
    }
    if (options.queryKey?.[0] === 'docmind-hierarchy') {
      return {
        data: {
          dataset_id: 'dataset-1',
          nodes: [
            {
              file_id: 'file-1',
              document_id: mockHierarchyDocumentId,
              name: 'protocol.pdf',
              relative_path: '품질/protocol.pdf',
              depth: 1,
              type: 'file',
              document_exists: true,
              index_state: 'FAILED',
              index_cleanup_state: 'PENDING',
              index_error_code: 'HOST_WORKER_ERROR',
            },
          ],
        },
        isError: mockHierarchyError,
        refetch: mockRefetch,
      };
    }
    return {
      data: { registrations: [] },
      isError: mockRegistrationError,
      refetch: mockRefetch,
    };
  },
}));

jest.mock('@/services/docmind-service', () => ({
  getDocMindFolders: jest.fn(),
  getDocMindHierarchy: jest.fn(),
  getDocMindRegistrations: jest.fn(),
  registerDocMindDocuments: jest.fn(),
  retryDocMindRegistration: jest.fn(),
  searchDocMind: jest.fn(),
}));
jest.mock('@/components/icon-font', () => ({ FileIcon: () => null }));
jest.mock('@/components/highlight-markdown', () => ({
  __esModule: true,
  default: ({ children }: any) => children,
}));
jest.mock('@/components/document-preview', () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock('@/components/originui/input', () => ({
  Input: (props: any) => <input {...props} />,
}));
jest.mock('@/components/ui/modal/modal', () => ({
  Modal: ({ children }: any) => children,
}));
jest.mock('@/hooks/use-document-request', () => ({
  useGetChunkHighlights: () => ({
    highlights: [],
    setWidthAndHeight: jest.fn(),
  }),
  useGetDocumentUrl: () => () => '',
}));
jest.mock('lucide-react', () => ({
  ChevronDown: () => null,
  Search: () => null,
  X: () => null,
}));
jest.mock('./workspace-views', () => ({
  ChunkInspection: () => null,
  WorkspaceSettings: () => null,
}));

import DocMind from './index';

function renderDocMind(path = '/docmind') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <DocMind />
    </MemoryRouter>,
  );
}

function enterQuestion() {
  fireEvent.change(screen.getByLabelText('문서 검색 질문'), {
    target: { value: '시험 질문' },
  });
}

describe('DocMind search scopes', () => {
  beforeEach(() => {
    mockSearchMutate.mockClear();
    mockRefetch.mockClear();
    mockCanAdminister = false;
    mockFolderError = false;
    mockHierarchyError = false;
    mockRegistrationError = false;
    mockHierarchyDocumentId = 'document-1';
    mockSearchResult = undefined;
    mockSearchVariables = undefined;
    mockDocumentOptions = [
      {
        id: 'document-1',
        name: 'protocol.pdf',
        folder_id: 'folder-1',
        relative_path: '품질/protocol.pdf',
      },
    ];
  });

  it('searches all accessible documents by default', () => {
    renderDocMind();
    enterQuestion();
    fireEvent.click(screen.getByLabelText('검색 실행'));

    expect(mockSearchMutate).toHaveBeenCalledWith({
      question: '시험 질문',
      projectId: 'project-1',
      scope: { mode: 'all' },
    });
  });

  it('requires a folder before sending folders scope', () => {
    renderDocMind();
    enterQuestion();
    fireEvent.click(screen.getByText('전체 문서'));
    fireEvent.click(screen.getByLabelText('선택 폴더와 하위 폴더'));

    expect(screen.getByLabelText('검색 실행')).toBeDisabled();
    fireEvent.click(screen.getByLabelText('품질'));
    fireEvent.click(screen.getByLabelText('검색 실행'));

    expect(mockSearchMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        scope: { mode: 'folders', folderIds: ['folder-1'] },
      }),
    );
  });

  it('sends only explicitly selected document IDs', () => {
    renderDocMind();
    enterQuestion();
    fireEvent.click(screen.getByText('전체 문서'));
    fireEvent.click(screen.getByLabelText('선택 문서'));
    fireEvent.click(screen.getByLabelText('품질/protocol.pdf'));
    fireEvent.click(screen.getByLabelText('검색 실행'));

    expect(mockSearchMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        scope: { mode: 'documents', documentIds: ['document-1'] },
      }),
    );
  });

  it('allows document scope for a non-admin user', () => {
    renderDocMind();
    enterQuestion();
    fireEvent.click(screen.getByText('전체 문서'));

    expect(screen.getByLabelText('선택 문서')).not.toBeDisabled();
  });

  it('does not expose the removed catalog workspace', () => {
    renderDocMind();

    expect(screen.queryByText('Catalog')).not.toBeInTheDocument();
  });

  it('keeps unverified direct uploads out of the cloud library', () => {
    mockCanAdminister = true;
    renderDocMind('/docmind?view=library');
    expect(screen.queryByLabelText('등록할 문서')).not.toBeInTheDocument();
    expect(screen.getByText(/문서 추가·수정은 All-in-One/)).toBeInTheDocument();
  });

  it('shows actual ingestion failure on the document detail view', () => {
    mockCanAdminister = true;
    mockHierarchyDocumentId = 'b'.repeat(32);
    renderDocMind(`/docmind?view=document&id=${'a'.repeat(32)}&doc_id=${mockHierarchyDocumentId}&source=docmind`);
    expect(screen.getByRole('status')).toHaveTextContent(
      '인덱싱 실패 · 임시 파일 정리 대기 (HOST_WORKER_ERROR)',
    );
  });

  it('does not show stale success when detail status refresh fails', () => {
    mockCanAdminister = true;
    mockHierarchyError = true;
    mockHierarchyDocumentId = 'b'.repeat(32);
    renderDocMind(`/docmind?view=document&id=${'a'.repeat(32)}&doc_id=${mockHierarchyDocumentId}&source=docmind`);
    expect(screen.getByRole('status')).toHaveTextContent('문서 상태를 확인하지 못했습니다.');
  });

  it('blocks search on a failed scope load and allows scope recovery', () => {
    mockFolderError = true;
    renderDocMind();
    enterQuestion();
    expect(screen.getByRole('alert')).toHaveTextContent(
      '검색 범위를 불러오지 못했습니다',
    );
    expect(screen.getByLabelText('검색 실행')).toBeDisabled();
    fireEvent.submit(screen.getByLabelText('문서 검색 질문').closest('form')!);
    expect(mockSearchMutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('검색 범위 다시 불러오기'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('shows a failed library request instead of an empty library and supports retry', () => {
    mockCanAdminister = true;
    mockHierarchyError = true;
    mockRegistrationError = true;
    renderDocMind('/docmind?view=library');
    expect(screen.getAllByRole('alert')).toHaveLength(2);
    expect(screen.queryByText('문서가 없습니다.')).not.toBeInTheDocument();
    expect(
      screen.queryByText('현재 진행하거나 반영할 작업이 없습니다.'),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('자료 목록 새로고침'));
    expect(mockRefetch).toHaveBeenCalledTimes(3);
  });

  it('hides results from a previous scope after the selection changes', () => {
    mockSearchVariables = {
      projectId: 'project-1',
      scope: { mode: 'all' },
    };
    mockSearchResult = {
      dataset_id: 'dataset-1',
      candidate_count: 1,
      ranked_chunks: [{ doc_id: 'document-1', doc_name: 'protocol.pdf', content_with_weight: 'saved result' }],
    };
    renderDocMind();
    expect(screen.getByText('결과 1개 · 후보 1개')).toBeInTheDocument();

    fireEvent.click(screen.getByText('전체 문서'));
    fireEvent.click(screen.getByLabelText('선택 문서'));

    expect(screen.queryByText('saved result')).not.toBeInTheDocument();
    expect(screen.getByText('검색 범위가 변경되었습니다. 다시 검색해 주세요.')).toBeInTheDocument();
  });

  it('hides a cached result when its document leaves the live catalog', () => {
    mockSearchVariables = {
      projectId: 'project-1',
      scope: { mode: 'all' },
    };
    mockSearchResult = {
      dataset_id: 'dataset-1',
      candidate_count: 1,
      ranked_chunks: [{ doc_id: 'document-1', doc_name: 'protocol.pdf', content_with_weight: 'saved result' }],
    };
    const view = renderDocMind();
    expect(screen.getByText('결과 1개 · 후보 1개')).toBeInTheDocument();

    mockDocumentOptions = [];
    view.rerender(
      <MemoryRouter initialEntries={['/docmind']}>
        <DocMind />
      </MemoryRouter>,
    );

    expect(screen.queryByText('saved result')).not.toBeInTheDocument();
    expect(screen.getByText('검색 범위가 변경되었습니다. 다시 검색해 주세요.')).toBeInTheDocument();
  });
});
