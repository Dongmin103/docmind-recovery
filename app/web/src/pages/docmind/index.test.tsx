import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

const mockSearchMutate = jest.fn();

jest.mock('@tanstack/react-query', () => ({
  useMutation: (options: { mutationKey?: string[] }) => ({
    data: undefined,
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
          can_administer: true,
          folders: [
            { id: 'folder-1', name: '품질', document_count: 1 },
            { id: 'folder-2', name: '밸리데이션', document_count: 1 },
          ],
        },
        isError: false,
      };
    }
    if (options.queryKey?.[0] === 'docmind-hierarchy') {
      return {
        data: {
          dataset_id: 'dataset-1',
          nodes: [
            {
              file_id: 'file-1',
              document_id: 'document-1',
              name: 'protocol.pdf',
              relative_path: '품질/protocol.pdf',
              depth: 1,
              type: 'file',
              document_exists: true,
            },
          ],
        },
        isError: false,
      };
    }
    return { data: { registrations: [] }, isError: false, refetch: jest.fn() };
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

function renderDocMind() {
  return render(
    <MemoryRouter initialEntries={['/docmind']}>
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
  beforeEach(() => mockSearchMutate.mockClear());

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

  it('does not expose the removed catalog workspace', () => {
    renderDocMind();

    expect(screen.queryByText('Catalog')).not.toBeInTheDocument();
  });
});
