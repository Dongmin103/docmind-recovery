import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
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
let mockExtraHierarchyNodes: import('@/services/docmind-service').DocMindHierarchyNode[] =
  [];
const mockOcrRequest = jest.fn().mockResolvedValue(undefined);

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
    reset: jest.fn(),
    mutateAsync: mockOcrRequest,
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
            ...mockExtraHierarchyNodes,
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
jest.mock('@/hooks/use-document-inspection', () => ({
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
    mockExtraHierarchyNodes = [];
    mockOcrRequest.mockClear();
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

  it('only offers zero-text OCR while keeping requested progress visible', async () => {
    mockCanAdminister = true;
    const scan: import('@/services/docmind-service').DocMindHierarchyNode = {
      file_id: 'scan',
      document_id: 'scan-document',
      name: 'scan.pdf',
      relative_path: '품질/검토/scan.pdf',
      depth: 2,
      type: 'file',
      document_exists: true,
      index_state: 'FAILED',
      index_pdf_ocr_option: 'zero_text',
      index_pdf_ocr_version_id: 'scan-version',
    };
    mockExtraHierarchyNodes = [
      scan,
      {
        ...scan,
        file_id: 'mixed',
        document_id: 'mixed-document',
        name: 'mixed.pdf',
        index_state: 'INDEXED',
        searchable: true,
        index_pdf_ocr_option: 'partial_images',
      },
      {
        ...scan,
        file_id: 'slides',
        document_id: 'slides-document',
        name: 'slides.pptx',
        index_state: 'INDEXED',
        index_pdf_ocr_option: null,
        index_image_ocr_not_run: true,
      },
      {
        ...scan,
        file_id: 'running',
        document_id: 'running-document',
        name: 'running.pdf',
        index_state: 'PROCESSING',
      },
      {
        ...scan,
        file_id: 'deleted',
        document_id: 'deleted-document',
        name: 'deleted.pdf',
        document_exists: false,
      },
      { ...scan, file_id: 'folder', name: 'folder', type: 'folder' },
    ];
    const { rerender } = renderDocMind('/docmind?view=library');
    const panel = within(screen.getByRole('region', { name: 'OCR 필요' }));
    expect(panel.getByText('OCR 필요 1건')).toBeVisible();
    expect(screen.queryByText(/OCR 검토/)).toBeNull();
    expect(screen.queryByText(/이미지 속 글자/)).toBeNull();
    expect(panel.queryByText(/기타 인덱싱 확인/)).toBeNull();
    expect(panel.queryByText('mixed.pdf')).toBeNull();
    expect(panel.queryByText('slides.pptx')).toBeNull();
    expect(panel.getAllByText('품질/검토').length).toBeGreaterThan(0);
    expect(panel.queryByText('protocol.pdf')).not.toBeInTheDocument();
    expect(panel.queryByText('running.pdf')).not.toBeInTheDocument();
    expect(panel.queryByText('deleted.pdf')).not.toBeInTheDocument();
    expect(panel.queryByText('mixed.pdf')).not.toBeInTheDocument();
    fireEvent.click(panel.getByRole('button', { name: 'OCR 선택' }));
    fireEvent.click(panel.getByRole('button', { name: 'OCR로 인덱싱' }));
    await waitFor(() =>
      expect(mockOcrRequest).toHaveBeenCalledWith({
        documentId: 'scan-document',
        versionId: 'scan-version',
        key: expect.any(String),
      }),
    );
    expect(within(await panel.findByRole('region', { name: 'OCR 진행 및 결과' })).getByText('scan.pdf')).toBeVisible();
    expect(panel.queryByRole('button', { name: 'OCR 선택' })).toBeNull();
    expect(panel.queryByText('protocol.pdf')).toBeNull();
    mockExtraHierarchyNodes = [];
    rerender(
      <MemoryRouter initialEntries={['/docmind?view=library']}>
        <DocMind />
      </MemoryRouter>,
    );
    expect(
      panel.getByText('OCR 필요 0건'),
    ).toBeInTheDocument();
    expect(
      panel.getByText('현재 OCR이 필요한 자료가 없습니다.'),
    ).toBeInTheDocument();
  });

  it('does not show stale OCR counts when the hierarchy cannot be refreshed', () => {
    mockCanAdminister = true;
    mockHierarchyError = true;
    renderDocMind('/docmind?view=library');
    expect(
      screen.queryByRole('region', { name: 'OCR 필요' }),
    ).not.toBeInTheDocument();
  });

  it('shows actual ingestion failure on the document detail view', () => {
    mockCanAdminister = true;
    mockHierarchyDocumentId = 'b'.repeat(32);
    renderDocMind(
      `/docmind?view=document&id=${'a'.repeat(32)}&doc_id=${mockHierarchyDocumentId}&source=docmind`,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      '인덱싱 실패 · 임시 파일 정리 대기 (HOST_WORKER_ERROR)',
    );
  });

  it.each(['pdf', 'pptx'])('hides image OCR review hints on direct %s detail', (format) => {
    mockCanAdminister = true;
    const documentId = 'c'.repeat(32);
    mockExtraHierarchyNodes = [{
      file_id: 'mixed', document_id: documentId, name: `mixed.${format}`,
      relative_path: `mixed.${format}`, depth: 1, type: 'file', document_exists: true,
      index_state: 'INDEXED', searchable: true, index_image_ocr_not_run: true,
      index_pdf_ocr_option: format === 'pdf' ? 'partial_images' : null,
      index_pdf_ocr_version_id: 'mixed-version',
    }];
    renderDocMind(`/docmind?view=document&id=${'a'.repeat(32)}&doc_id=${documentId}&source=docmind`);
    expect(screen.getByRole('status')).toHaveTextContent('인덱싱 완료');
    expect(screen.queryByText(/이미지 속 글자|OCR 검토/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'OCR 선택' })).toBeNull();
  });

  it('does not show stale success when detail status refresh fails', () => {
    mockCanAdminister = true;
    mockHierarchyError = true;
    mockHierarchyDocumentId = 'b'.repeat(32);
    renderDocMind(
      `/docmind?view=document&id=${'a'.repeat(32)}&doc_id=${mockHierarchyDocumentId}&source=docmind`,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      '문서 상태를 확인하지 못했습니다.',
    );
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

  it('shows 32 document results eight at a time', () => {
    mockDocumentOptions = Array.from({ length: 32 }, (_, index) => ({
      id: `document-${index + 1}`,
      name: `result-${index + 1}.pdf`,
    }));
    mockSearchVariables = { projectId: 'project-1', scope: { mode: 'all' } };
    mockSearchResult = {
      dataset_id: 'dataset-1',
      candidate_count: 128,
      ranked_chunks: mockDocumentOptions.map((document) => ({
        doc_id: document.id,
        doc_name: document.name,
        content_with_weight: `Evidence for ${document.id}`,
      })),
    };
    renderDocMind();
    const results = within(screen.getByRole('region', { name: '검색 결과' }));
    expect(results.getByRole('status')).toHaveTextContent('문서 32개 · 검색 후보 128개');
    for (const count of [8, 16, 24, 32]) {
      expect(results.getAllByText(/^Evidence for document-/)).toHaveLength(count);
      if (count < 32) {
        fireEvent.click(results.getByRole('button', { name: '결과 더 보기' }));
      }
    }
    expect(results.queryByRole('button', { name: '결과 더 보기' })).not.toBeInTheDocument();
  });

  it('hides results from a previous scope after the selection changes', () => {
    mockSearchVariables = {
      projectId: 'project-1',
      scope: { mode: 'all' },
    };
    mockSearchResult = {
      dataset_id: 'dataset-1',
      candidate_count: 1,
      ranked_chunks: [
        {
          doc_id: 'document-1',
          doc_name: 'protocol.pdf',
          content_with_weight: 'saved result',
        },
      ],
    };
    renderDocMind();
    expect(screen.getByRole('region', { name: '검색 결과' })).toHaveTextContent(
      '문서 1개 · 검색 후보 1개',
    );

    fireEvent.click(screen.getByText('전체 문서'));
    fireEvent.click(screen.getByLabelText('선택 문서'));

    expect(screen.queryByText('saved result')).not.toBeInTheDocument();
    expect(
      screen.getByText('검색 범위가 변경되었습니다. 다시 검색해 주세요.'),
    ).toBeInTheDocument();
  });

  it('never attaches answer generation to existing search results', () => {
    mockSearchVariables = {
      question: '시험 질문',
      projectId: 'project-1',
      scope: { mode: 'all' },
    };
    mockSearchResult = {
      dataset_id: 'dataset-1',
      candidate_count: 1,
      ranked_chunks: [
        {
          doc_id: 'document-1',
          chunk_id: 'chunk-1',
          content_with_weight: 'synthetic',
        },
      ],
    };
    renderDocMind();
    enterQuestion();
    expect(
      screen.queryByRole('region', { name: '문서 근거 답변' }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('문서 검색 질문'), {
      target: { value: '다른 질문' },
    });
    expect(
      screen.queryByRole('region', { name: '문서 근거 답변' }),
    ).not.toBeInTheDocument();
    enterQuestion();
    expect(
      screen.queryByRole('region', { name: '문서 근거 답변' }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('전체 문서'));
    fireEvent.click(screen.getByLabelText('선택 문서'));
    expect(
      screen.queryByRole('region', { name: '문서 근거 답변' }),
    ).not.toBeInTheDocument();
  });

  it('hides a cached result when its document leaves the live catalog', () => {
    mockSearchVariables = {
      projectId: 'project-1',
      scope: { mode: 'all' },
    };
    mockSearchResult = {
      dataset_id: 'dataset-1',
      candidate_count: 1,
      ranked_chunks: [
        {
          doc_id: 'document-1',
          doc_name: 'protocol.pdf',
          content_with_weight: 'saved result',
        },
      ],
    };
    const view = renderDocMind();
    expect(screen.getByRole('region', { name: '검색 결과' })).toHaveTextContent(
      '문서 1개 · 검색 후보 1개',
    );

    mockDocumentOptions = [];
    view.rerender(
      <MemoryRouter initialEntries={['/docmind']}>
        <DocMind />
      </MemoryRouter>,
    );

    expect(screen.queryByText('saved result')).not.toBeInTheDocument();
    expect(
      screen.getByText('검색 범위가 변경되었습니다. 다시 검색해 주세요.'),
    ).toBeInTheDocument();
  });
});
