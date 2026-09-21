import {
  act,
  fireEvent,
  render as renderView,
  screen,
  within,
  waitFor,
} from '@testing-library/react';
import * as React from 'react';
import { MemoryRouter } from 'react-router';

const render = (ui: any, options = {}) =>
  renderView(ui, {
    wrapper: ({ children }: { children: any }) => (
      <MemoryRouter initialEntries={['/docmind']}>{children}</MemoryRouter>
    ),
    ...options,
  });

jest.mock('./workspace-views', () => ({
  WorkspaceSettings: ({ sharedWorkspace }: any) => (
    <p data-shared-workspace={sharedWorkspace}>계정 설정 테스트 화면</p>
  ),
  ChunkInspection: ({ onBack, embedded, readOnly }: any) => (
    <div
      data-testid="workspace-inspector"
      data-embedded={embedded}
      data-read-only={readOnly}
    >
      <button onClick={onBack}>이전 작업으로 돌아가기</button>
    </div>
  ),
}));

const mockUseMutation = jest.fn();
const mockUseQuery = jest.fn();

jest.mock('@tanstack/react-query', () => ({
  useMutation: (options: unknown) => mockUseMutation(options),
  useQuery: (options: unknown) => mockUseQuery(options),
}));

jest.mock('@/services/docmind-service', () => ({
  changeDocMindDraft: jest.fn(),
  captureDocMindHierarchyDraft: jest.fn(),
  createDocMindHierarchyFolder: jest.fn(),
  deleteDocMindHierarchyFolder: jest.fn(),
  createDocMindManualCardRevision: jest.fn(),
  createDocMindDraft: jest.fn(),
  deleteDocMindCatalogVersion: jest.fn(),
  generateDocMindDraft: jest.fn(),
  getDocMindCatalogVersions: jest.fn(),
  getDocMindDraft: jest.fn(),
  getDocMindDrafts: jest.fn(),
  getDocMindFolders: jest.fn(),
  getDocMindHierarchy: jest.fn(),
  getDocMindImportJobs: jest.fn(),
  getDocMindRegistrations: jest.fn(),
  registerDocMindDocuments: jest.fn(),
  importDocMindLocalFolder: jest.fn(),
  publishDocMindCatalogVersion: jest.fn(),
  rollbackDocMindCatalogVersion: jest.fn(),
  retryDocMindRegistration: jest.fn(),
  retryDocMindLocalFolderImport: jest.fn(),
  searchDocMind: jest.fn(),
  updateDocMindHierarchyFolder: jest.fn(),
}));

jest.mock('@/components/icon-font', () => ({
  FileIcon: () => null,
}));

jest.mock('@/components/highlight-markdown', () => ({
  __esModule: true,
  default: ({ children }: any) => children,
}));

jest.mock('@/components/document-preview', () => ({
  __esModule: true,
  default: () => null,
}));

jest.mock('@/components/originui/input', () => ({
  Input: (props: any) =>
    jest.requireActual('react').createElement('input', props),
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

import {
  changeDocMindDraft,
  createDocMindManualCardRevision,
} from '@/services/docmind-service';
import DocMind from './index';

const datasetId = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const registrationDocumentId = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const missingDocumentId = 'cccccccccccccccccccccccccccccccc';

const rankedChunks = Array.from({ length: 25 }, (_, index) => ({
  chunk_id: `chunk-${index + 1}`,
  doc_id: (index + 1).toString(16).padStart(32, '0'),
  docnm_kwd: `Document ${index + 1}.pdf`,
  content_with_weight: `Evidence ${index + 1}`,
  positions: [[1, 1, 2, 3, 4]],
}));

const folders = [
  {
    id: 'quality-risk-management',
    name: 'Quality Risk Management',
    document_count: 6,
  },
  { id: 'validation', name: 'Validation', document_count: 2 },
  {
    id: 'analytical-quality-control',
    name: 'Analytical Quality Control',
    document_count: 10,
  },
  {
    id: 'biopharmaceutical-manufacturing',
    name: 'Biopharmaceutical Manufacturing',
    document_count: 2,
  },
  { id: 'quality-operations', name: 'Quality Operations', document_count: 3 },
];

const automaticResult = {
  dataset_id: datasetId,
  chunks: rankedChunks.slice(0, 5),
  ranked_chunks: rankedChunks,
  ranked_total: rankedChunks.length,
  scope_mode: 'automatic',
  effective_folder_ids: ['quality-risk-management', 'validation'],
  catalog_source: 'static',
  catalog_version_id: 'static-version',
  selected_folders: [],
  total: 5,
  candidate_count: 25,
  caps: {
    folders: 5,
    candidates: 64,
    per_folder: 32,
    per_document: 8,
    results: 5,
  },
};

describe('DocMind result browsing', () => {
  const mutate = jest.fn();
  const refetch = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
    mockUseQuery.mockReturnValue({
      data: {
        dataset_id: datasetId,
        catalog_source: 'static',
        catalog_version_id: 'static-version',
        workspace_mode: 'shared',
        folders,
      },
      isError: false,
      refetch,
    });
    mockUseMutation.mockReturnValue({
      data: automaticResult,
      isPending: false,
      isSuccess: true,
      isError: false,
      mutate,
    });
  });

  function useSearchResult(chunks: any[], ranked = true) {
    mockUseMutation.mockReturnValue({
      data: {
        ...automaticResult,
        chunks,
        ranked_chunks: ranked ? chunks : undefined,
      },
      isPending: false,
      isSuccess: true,
      isError: false,
      mutate,
    });
  }

  it('keeps only the highest-ranked chunk for each document, preserving order and location', () => {
    const first = {
      ...rankedChunks[0],
      document_relative_path: 'GMP/품질관리/SOP.pdf',
      positions: [[5, 1, 2, 3, 4]],
    };
    useSearchResult([
      first,
      {
        ...first,
        chunk_id: 'duplicate',
        content_with_weight: 'Duplicate evidence',
      },
      rankedChunks[1],
      rankedChunks[2],
      { ...first, chunk_id: 'later' },
    ]);
    render(<DocMind />);
    expect(screen.getByText('검색 문서 3개 중 3개 표시')).toBeVisible();
    expect(document.querySelectorAll('article')).toHaveLength(3);
    expect(screen.queryByText('Duplicate evidence')).toBeNull();
    expect(
      screen.getByRole('button', { name: 'Document 1.pdf p. 5 원문 열기' }),
    ).toBeVisible();
    const path = screen.getByLabelText('문서 경로: GMP > 품질관리 > SOP.pdf');
    expect(path).toHaveAttribute('title', 'GMP > 품질관리 > SOP.pdf');
    expect(path).toHaveClass('truncate');
    const names = Array.from(document.querySelectorAll('article')).map((card) =>
      within(card).getByRole('button').getAttribute('aria-label'),
    );
    expect(names).toEqual([
      'Document 1.pdf p. 5 원문 열기',
      'Document 2.pdf p. 1 원문 열기',
      'Document 3.pdf p. 1 원문 열기',
    ]);
    fireEvent.click(
      screen.getByRole('button', { name: 'Document 1.pdf p. 5 원문 열기' }),
    );
    expect(
      screen.getAllByRole('link', { name: '원문 · 청크 확인' }),
    ).toHaveLength(4);
  });

  it('deduplicates before pagination, including duplicates after the first page', () => {
    useSearchResult([
      ...rankedChunks.slice(0, 10),
      { ...rankedChunks[0], chunk_id: 'later-a' },
      ...rankedChunks.slice(10),
      { ...rankedChunks[15], chunk_id: 'later-b' },
    ]);
    render(<DocMind />);
    expect(screen.getByText('검색 문서 25개 중 10개 표시')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '결과 10개 더 보기' }));
    expect(document.querySelectorAll('article')).toHaveLength(20);
    fireEvent.click(screen.getByRole('button', { name: '결과 5개 더 보기' }));
    expect(document.querySelectorAll('article')).toHaveLength(25);
    expect(screen.getByText('검색 문서 25개 중 25개 표시')).toBeVisible();
  });

  it('keeps documents with the same filename and unidentified chunks separate', () => {
    useSearchResult([
      { ...rankedChunks[0], docnm_kwd: 'same.pdf' },
      { ...rankedChunks[1], docnm_kwd: 'same.pdf' },
      { ...rankedChunks[2], doc_id: undefined },
      { ...rankedChunks[3], doc_id: '' },
    ]);
    render(<DocMind />);
    expect(document.querySelectorAll('article')).toHaveLength(4);
    expect(
      screen.getAllByRole('button', { name: 'same.pdf p. 1 원문 열기' }),
    ).toHaveLength(2);
    expect(screen.queryByLabelText(/^문서 경로:/)).toBeNull();
  });

  it('falls back to chunks and shows one card when every chunk belongs to one document', () => {
    useSearchResult(
      [rankedChunks[0], { ...rankedChunks[0], chunk_id: 'duplicate' }],
      false,
    );
    render(<DocMind />);
    expect(document.querySelectorAll('article')).toHaveLength(1);
    expect(screen.getByText('검색 문서 1개 중 1개 표시')).toBeVisible();
  });

  it('exposes a full long path while truncating the path row', () => {
    const path =
      Array.from({ length: 20 }, () => '매우 긴 폴더 이름').join('/') +
      '/SOP.pdf';
    useSearchResult([{ ...rankedChunks[0], document_relative_path: path }]);
    render(<DocMind />);
    const row = screen.getByLabelText(
      `문서 경로: ${path.replaceAll('/', ' > ')}`,
    );
    expect(row).toHaveClass('truncate');
    expect(row).toHaveAttribute('title', path.replaceAll('/', ' > '));
  });

  it('resets the displayed document count when submitting another search', () => {
    render(<DocMind />);
    fireEvent.click(screen.getByRole('button', { name: '결과 10개 더 보기' }));
    expect(document.querySelectorAll('article')).toHaveLength(20);
    act(() =>
      mockUseMutation.mock.calls
        .find(([options]) => options.onMutate)[0]
        .onMutate(),
    );
    expect(document.querySelectorAll('article')).toHaveLength(10);
  });

  it('shows ten ranked results first and reveals ten more at a time without a new search', () => {
    render(React.createElement(DocMind));

    expect(screen.getByText('검색 문서 25개 중 10개 표시')).toBeInTheDocument();
    expect(document.querySelectorAll('article')).toHaveLength(10);

    fireEvent.click(screen.getByRole('button', { name: '결과 10개 더 보기' }));
    expect(screen.getByText('검색 문서 25개 중 20개 표시')).toBeInTheDocument();
    expect(document.querySelectorAll('article')).toHaveLength(20);

    fireEvent.click(screen.getByRole('button', { name: '결과 5개 더 보기' }));
    expect(screen.getByText('검색 문서 25개 중 25개 표시')).toBeInTheDocument();
    expect(document.querySelectorAll('article')).toHaveLength(25);
    expect(
      screen.queryByRole('button', { name: /결과 .*개 더 보기/ }),
    ).toBeNull();
  });

  it('keeps the original preview and links to document detail in the same workspace', () => {
    render(React.createElement(DocMind));

    const inspectionLinks = screen.getAllByRole('link', {
      name: '원문 · 청크 확인',
    });
    expect(inspectionLinks[0]).toHaveAttribute(
      'href',
      `/docmind?view=document&id=${datasetId}&doc_id=00000000000000000000000000000001&source=docmind&return=search`,
    );
    expect(inspectionLinks[0]).not.toHaveAttribute('target');

    fireEvent.click(
      screen.getByRole('button', {
        name: 'Document 1.pdf p. 1 원문 열기',
      }),
    );
    expect(
      screen.getAllByRole('link', {
        name: '원문 · 청크 확인',
      }),
    ).toHaveLength(11);
  });

  it('retains query, folder selection and expanded results across document inspection and settings', async () => {
    render(React.createElement(DocMind));
    const input = screen.getByPlaceholderText(
      '문서에서 찾고 싶은 내용을 입력하세요',
    );
    fireEvent.change(input, { target: { value: '밸리데이션 정확성' } });
    fireEvent.click(screen.getByRole('button', { name: /전체\(자동\)/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Validation/ }));
    fireEvent.click(screen.getByRole('button', { name: '결과 10개 더 보기' }));
    const inspection = screen.getAllByRole('link', {
      name: '원문 · 청크 확인',
    })[0];
    inspection.focus();
    fireEvent.click(inspection);
    const viewer = await screen.findByTestId('workspace-inspector');
    expect(viewer).toHaveAttribute('data-embedded', 'true');
    expect(viewer).toHaveAttribute('data-read-only', 'true');
    expect(input).not.toBeVisible();
    fireEvent.click(
      screen.getByRole('button', { name: '이전 작업으로 돌아가기' }),
    );
    expect(input).toHaveValue('밸리데이션 정확성');
    expect(screen.getByRole('checkbox', { name: /Validation/ })).toBeChecked();
    expect(screen.getByText('검색 문서 25개 중 20개 표시')).toBeVisible();
    expect(inspection).toHaveFocus();
    fireEvent.click(screen.getByRole('link', { name: '설정' }));
    expect(await screen.findByText('계정 설정 테스트 화면')).toHaveAttribute(
      'data-shared-workspace',
      'true',
    );
    fireEvent.click(screen.getByRole('link', { name: '검색' }));
    expect(input).toHaveValue('밸리데이션 정확성');
    expect(screen.getByText('검색 문서 25개 중 20개 표시')).toBeVisible();
    expect(mutate).not.toHaveBeenCalled();
  });

  it('denies administrator views for a reader even with a direct URL', () => {
    renderView(
      <MemoryRouter initialEntries={['/docmind?view=catalog']}>
        <DocMind />
      </MemoryRouter>,
    );
    expect(
      screen.getByText('이 화면은 관리자만 사용할 수 있습니다.'),
    ).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Catalog' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Publish 검토' })).toBeNull();
    expect(mutate).not.toHaveBeenCalled();
  });

  it('rejects a malformed document deep link before mounting the viewer', () => {
    renderView(
      <MemoryRouter
        initialEntries={[
          '/docmind?view=document&id=wrong&doc_id=wrong&return=https://external.test',
        ]}
      >
        <DocMind />
      </MemoryRouter>,
    );
    expect(screen.getByText('문서 주소가 올바르지 않습니다.')).toBeVisible();
    expect(screen.queryByTestId('workspace-inspector')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '이전 화면으로' }));
    expect(
      screen.getByPlaceholderText('문서에서 찾고 싶은 내용을 입력하세요'),
    ).toBeVisible();
    expect(mutate).not.toHaveBeenCalled();
  });

  it('shows Office structural provenance instead of a fake PDF page', () => {
    const officeChunk = {
      ...rankedChunks[0],
      docnm_kwd: 'structured.xlsx',
      positions: [],
      office_locator: {
        kind: 'xlsx',
        sheet: 'Sheet-B',
        cell_range: 'B3:F3',
        region_locator: '#/texts/0',
      },
    };
    mockUseMutation.mockReturnValue({
      data: {
        ...automaticResult,
        chunks: [officeChunk],
        ranked_chunks: [officeChunk],
        ranked_total: 1,
      },
      isPending: false,
      isSuccess: true,
      isError: false,
      mutate,
    });

    render(React.createElement(DocMind));
    expect(
      screen.getByRole('button', {
        name: 'structured.xlsx Sheet-B!B3:F3 원문 열기',
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText('p. 1')).toBeNull();
  });

  it('shows all automatic plus exactly five server folders', () => {
    render(React.createElement(DocMind));

    fireEvent.click(screen.getByRole('button', { name: /폴더: 전체\(자동\)/ }));

    expect(
      screen.getByRole('option', { name: /전체\(자동\)/ }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole('checkbox')).toHaveLength(5);
    for (const folder of folders) {
      expect(screen.getByText(folder.name)).toBeInTheDocument();
    }
  });

  it('shows the owner-only 1-to-5 file registration panel and status', async () => {
    const uploadMutate = jest.fn();
    const retryMutate = jest.fn();
    const createDraftMutate = jest.fn();
    mockUseQuery.mockImplementation((options: any) => {
      const key = options.queryKey[0];
      if (key === 'docmind-folders') {
        return {
          data: {
            dataset_id: datasetId,
            catalog_source: 'static',
            catalog_version_id: 'static-routing-version',
            folders,
            can_administer: true,
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-registrations') {
        return {
          data: {
            project_id: 'project-1',
            dataset_id: datasetId,
            catalog_version_id: 'db-version-v0',
            registrations: [
              {
                registration_id: 'registration-1',
                document_id: registrationDocumentId,
                document_name: 'Deviation.pdf',
                document_exists: true,
                folder_id: 'quality-operations',
                state: 'FAILED',
                error_code: 'DOCMIND_INDEX_FAILED',
                blocker_code: 'DOCMIND_INDEX_FAILED',
                progress: 0.2,
                chunk_count: 0,
                index_ready: false,
                draft_eligible: false,
                retry_allowed: true,
                is_current: true,
                parser_run: {
                  parse_run_id: 'run-1',
                  chunk_set_id: 'set-1',
                  active_chunk_set_id: 'set-old',
                  active: false,
                  source_format: 'docx',
                  selection_reason: 'file_format_docx',
                  parser_name: 'docling',
                  parser_version: '2.115.0',
                  backend: 'simple-pipeline',
                  schema_version: 'parser-platform-v1',
                  phase: 'FAILED_RETRYABLE',
                  warnings: [],
                  error_code: 'PARSER_DOCLING_TIMEOUT',
                  error_message:
                    'Docling Office 분석 시간이 제한을 초과했습니다.',
                  expected_pages: 0,
                  completed_pages: 0,
                  reused_pages: 0,
                  failed_pages: 0,
                },
              },
              {
                registration_id: 'registration-missing',
                document_id: missingDocumentId,
                document_name: 'Missing.pdf',
                document_exists: false,
                folder_id: 'quality-operations',
                state: 'FAILED',
                progress: 0,
                chunk_count: 0,
                index_ready: false,
                draft_eligible: false,
                active_catalog_member: false,
                retry_allowed: false,
                is_current: true,
              },
            ],
          },
          refetch,
        };
      }
      if (key === 'docmind-hierarchy') {
        return {
          data: {
            project_id: 'project-1',
            dataset_id: datasetId,
            source_root_file_id: 'source-root',
            nodes: [
              {
                file_id: 'source-root',
                name: 'GMP',
                relative_path: '',
                depth: 0,
                type: 'folder',
                document_exists: false,
              },
              {
                file_id: 'file-1',
                parent_file_id: 'source-root',
                name: 'Deviation.pdf',
                relative_path: 'Deviation.pdf',
                depth: 1,
                type: 'file',
                document_id: registrationDocumentId,
                document_exists: true,
                index_state: 'PENDING',
              },
              {
                file_id: 'file-stale',
                parent_file_id: 'source-root',
                name: 'Stale.pdf',
                relative_path: 'Stale.pdf',
                depth: 1,
                type: 'file',
                document_id: missingDocumentId,
                document_exists: false,
                index_state: 'PENDING',
              },
            ],
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-hierarchy-imports') {
        return { data: { jobs: [] }, isError: false, refetch };
      }
      return {
        data: {
          project_id: 'project-1',
          active_version_id: 'db-version-v0',
          drafts: [],
        },
        isError: false,
        refetch,
      };
    });
    mockUseMutation.mockImplementation((options: any) => {
      const key = options.mutationKey?.[0];
      if (key === 'docmind-registration-upload') {
        return {
          isPending: false,
          isError: false,
          mutate: uploadMutate,
        };
      }
      if (key === 'docmind-registration-retry') {
        return {
          isPending: false,
          mutate: retryMutate,
        };
      }
      if (key === 'docmind-draft-create') {
        return {
          isPending: false,
          isError: false,
          mutate: createDraftMutate,
        };
      }
      if (key === 'docmind-draft-change') {
        return {
          isPending: false,
          isError: false,
          mutate: jest.fn(),
        };
      }
      return {
        data: {
          ...automaticResult,
        },
        isPending: false,
        isSuccess: true,
        isError: false,
        mutate,
      };
    });

    render(React.createElement(DocMind));
    fireEvent.click(screen.getByRole('button', { name: '문서 등록' }));

    expect(screen.getByRole('heading', { name: '자료 관리' })).toBeVisible();
    expect(screen.getByText('폴더 가져오기')).toBeVisible();
    expect(screen.queryByLabelText('Catalog 진행 단계')).toBeNull();
    expect(
      screen.getByRole('button', { name: '폴더 전체 가져오기 선택' }),
    ).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('폴더와 문서')).toBeVisible();
    expect(screen.getByText('최대 100개 파일')).toBeVisible();
    expect(screen.getByText(/고급 폴더 편집/)).toBeInTheDocument();
    expect(screen.queryByText('Missing.pdf')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '확인 필요 2' }));
    expect(screen.getByText('Missing.pdf')).toBeVisible();
    expect(
      screen.getByText('Docling Office 분석 시간이 제한을 초과했습니다.'),
    ).toBeVisible();
    const inspectionLinks = screen.getAllByRole('link', { name: '문서 상세' });
    expect(inspectionLinks.length).toBeGreaterThanOrEqual(1);
    expect(inspectionLinks[0]).toHaveAttribute(
      'href',
      `/docmind?view=document&id=${datasetId}&doc_id=${registrationDocumentId}&source=docmind&return=library`,
    );
    expect(inspectionLinks[0]).not.toHaveAttribute('target');
    expect(
      document.querySelector(`a[href*="${missingDocumentId}"]`),
    ).toBeNull();

    fireEvent.click(
      screen.getByRole('button', { name: '기존 폴더에 문서 추가 선택' }),
    );
    expect(
      screen.getByRole('button', { name: '기존 폴더에 문서 추가 선택' }),
    ).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('문서 추가')).toBeInTheDocument();
    expect(
      screen.getByText(/기존 폴더에 추가 · 하위 폴더 구조 변경 없음/),
    ).toBeInTheDocument();
    expect(screen.queryByText('폴더와 문서')).toBeNull();
    const files = [new File(['one'], 'one.pdf'), new File(['two'], 'two.pdf')];
    fireEvent.change(screen.getByLabelText('등록할 문서'), {
      target: { files },
    });
    await waitFor(() =>
      expect(screen.getByLabelText('등록 대상 폴더')).toHaveValue(
        'quality-risk-management',
      ),
    );
    fireEvent.click(
      screen.getByRole('button', { name: '선택한 폴더에 문서 추가' }),
    );

    expect(uploadMutate).toHaveBeenCalledWith({
      folderId: 'quality-risk-management',
      files,
    });
    fireEvent.click(screen.getByRole('button', { name: '재시도' }));
    expect(retryMutate).toHaveBeenCalledWith(
      expect.objectContaining({ registrationId: 'registration-1' }),
    );
    fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));
    fireEvent.click(screen.getByRole('button', { name: '새 초안 만들기' }));
    expect(createDraftMutate).toHaveBeenCalledWith('db-version-v0');
  });

  it('adds an indexed registration to the current non-serving draft', () => {
    const draftChangeMutate = jest.fn();
    mockUseQuery.mockImplementation((options: any) => {
      const key = options.queryKey[0];
      if (key === 'docmind-folders') {
        return {
          data: {
            catalog_source: 'static',
            catalog_version_id: 'static-version',
            folders,
            can_administer: true,
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-registrations') {
        return {
          data: {
            project_id: 'project-1',
            catalog_version_id: 'static-version',
            registrations: [
              {
                registration_id: 'registration-indexed',
                document_exists: true,
                document_id: 'document-new',
                document_name: 'Validation Guide.pdf',
                folder_id: 'validation',
                state: 'INDEXED',
                progress: 1,
                chunk_count: 160,
                index_ready: true,
                draft_eligible: true,
                active_catalog_member: false,
                retry_allowed: false,
                is_current: true,
              },
              {
                registration_id: 'registration-published',
                document_id: 'document-published',
                document_name: 'Published Guide.pdf',
                folder_id: 'validation',
                state: 'INDEXED',
                progress: 1,
                chunk_count: 20,
                index_ready: true,
                draft_eligible: true,
                active_catalog_member: true,
                retry_allowed: false,
                is_current: true,
              },
              {
                registration_id: 'registration-old-2',
                document_id: 'document-new',
                document_name: 'Validation Guide.pdf',
                folder_id: 'validation',
                state: 'FAILED',
                blocker_code: 'DOCMIND_DOCUMENT_CHUNKS_EMPTY',
                progress: 1,
                chunk_count: 160,
                index_ready: false,
                draft_eligible: false,
                retry_allowed: true,
                is_current: false,
              },
              {
                registration_id: 'registration-old-1',
                document_id: 'document-new',
                document_name: 'Validation Guide.pdf',
                folder_id: 'validation',
                state: 'FAILED',
                blocker_code: 'DOCMIND_INDEX_INTERRUPTED',
                progress: 1,
                chunk_count: 160,
                index_ready: false,
                draft_eligible: false,
                retry_allowed: true,
                is_current: false,
              },
            ],
          },
          refetch,
        };
      }
      return {
        data: {
          project_id: 'project-1',
          active_version_id: 'static-version',
          drafts: [
            {
              draft_id: 'draft-failed',
              version_label: 'DRAFT-FAILED',
              parent_version_id: 'static-version',
              lifecycle_state: 'FAILED',
              health_state: 'INVALID',
              health_reason: 'DOCMIND_ROUTER_REGRESSION',
              snapshot_hash: 'f'.repeat(64),
              active_parent_is_current: true,
              change_count: 1,
              has_effective_changes: true,
              generation_progress: 0,
              digest_ready_count: 24,
              membership_count: 24,
              card_ready_count: 5,
              can_generate: false,
              folders: folders.map((folder, ordinal) => ({
                ...folder,
                ordinal,
              })),
              changes: [],
            },
            {
              draft_id: 'draft-1',
              version_label: 'DRAFT-1',
              parent_version_id: 'static-version',
              lifecycle_state: 'DRAFT',
              health_state: 'UNVALIDATED',
              snapshot_hash: 'a'.repeat(64),
              active_parent_is_current: true,
              change_count: 0,
              has_effective_changes: false,
              generation_progress: 0,
              digest_ready_count: 0,
              membership_count: 23,
              card_ready_count: 0,
              can_generate: false,
              folders: folders.map((folder, ordinal) => ({
                ...folder,
                ordinal,
              })),
              changes: [],
            },
          ],
        },
        isError: false,
        refetch,
      };
    });
    mockUseMutation.mockImplementation((options: any) => {
      const key = options.mutationKey?.[0];
      if (key === 'docmind-draft-change') {
        return {
          isPending: false,
          isError: false,
          mutate: draftChangeMutate,
        };
      }
      return {
        data: automaticResult,
        isPending: false,
        isSuccess: true,
        isError: false,
        mutate,
      };
    });

    render(React.createElement(DocMind));
    fireEvent.click(screen.getByRole('button', { name: '문서 등록' }));

    expect(
      screen.queryByText('DOCMIND_DOCUMENT_CHUNKS_EMPTY'),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '등록 기록 4건' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('DOCMIND_ROUTER_REGRESSION')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '등록 기록 4건' }));
    expect(screen.getAllByText(/처리 정보 · 이전 시도/)).toHaveLength(2);
    expect(screen.queryByRole('button', { name: '재시도' })).toBeNull();
    expect(screen.getByText(/검색 반영됨/)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '초안에 추가' })).toHaveLength(
      1,
    );
    fireEvent.click(screen.getByRole('button', { name: '초안에 추가' }));

    expect(mutate).not.toHaveBeenCalled();
    expect(draftChangeMutate).toHaveBeenCalledWith({
      draftId: 'draft-1',
      change: {
        operation: 'ADD',
        documentId: 'document-new',
        registrationId: 'registration-indexed',
        toFolderId: 'validation',
      },
    });
  });

  it('groups Catalog draft changes and keeps document details collapsed', () => {
    const hierarchyFolderId = 'dddddddddddddddddddddddddddddddd';
    mockUseQuery.mockImplementation((options: any) => {
      const key = options.queryKey[0];
      if (key === 'docmind-folders') {
        return {
          data: {
            dataset_id: datasetId,
            catalog_source: 'database',
            catalog_version_id: 'active-v1',
            folders,
            can_administer: true,
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-registrations') {
        return {
          data: {
            project_id: 'project-1',
            dataset_id: datasetId,
            catalog_version_id: 'active-v1',
            registrations: [],
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-hierarchy') {
        return {
          data: {
            project_id: 'project-1',
            dataset_id: datasetId,
            source_root_file_id: 'source-root',
            nodes: [
              {
                file_id: 'source-root',
                name: 'GMP',
                relative_path: '',
                depth: 0,
                type: 'folder',
                semantic_folder_id: hierarchyFolderId,
              },
            ],
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-hierarchy-imports') {
        return { data: { jobs: [] }, isError: false, refetch };
      }
      if (key === 'docmind-drafts') {
        return {
          data: {
            project_id: 'project-1',
            active_version_id: 'active-v1',
            drafts: [
              {
                draft_id: 'hierarchy-draft',
                version_label: 'DRAFT-HIERARCHY',
                parent_version_id: 'active-v1',
                lifecycle_state: 'DRAFT',
                health_state: 'UNVALIDATED',
                readiness_mode: 'ADMIN_SAVED',
                snapshot_schema_version: 2,
                snapshot_hash: 'a'.repeat(64),
                active_parent_is_current: true,
                change_count: 3,
                has_effective_changes: true,
                generation_progress: 0,
                digest_ready_count: 0,
                membership_count: 3,
                card_ready_count: 0,
                can_generate: true,
                folders: [
                  {
                    id: 'quality-risk-management',
                    name: 'Quality Risk Management',
                    document_count: 0,
                    ordinal: 0,
                  },
                  {
                    id: hierarchyFolderId,
                    name: 'GMP',
                    document_count: 3,
                    ordinal: 1,
                  },
                ],
                changes: [
                  {
                    operation: 'MOVE',
                    document_id: 'document-a',
                    document_name: 'Document A.pdf',
                    from_folder_id: 'quality-risk-management',
                    to_folder_id: hierarchyFolderId,
                    ordinal: 0,
                  },
                  {
                    operation: 'MOVE',
                    document_id: 'document-b',
                    document_name: 'Document B.pdf',
                    from_folder_id: 'quality-risk-management',
                    to_folder_id: hierarchyFolderId,
                    ordinal: 1,
                  },
                  {
                    operation: 'ADD',
                    document_id: 'document-c',
                    document_name: 'Document C.pdf',
                    to_folder_id: hierarchyFolderId,
                    ordinal: 2,
                  },
                ],
              },
            ],
          },
          isError: false,
          refetch,
        };
      }
      return { data: undefined, isError: false, refetch };
    });
    mockUseMutation.mockReturnValue({
      data: automaticResult,
      isPending: false,
      isSuccess: true,
      isError: false,
      mutate,
    });

    render(React.createElement(DocMind));
    fireEvent.click(screen.getByRole('button', { name: '문서 등록' }));

    fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));
    expect(screen.getByLabelText('Catalog 초안 변경 요약')).toHaveTextContent(
      'ADD1건MOVE2건',
    );
    expect(
      screen.getByLabelText('MOVE Quality Risk Management에서 GMP로 2개'),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('ADD GMP로 1개')).toBeInTheDocument();
    expect(screen.queryByText(hierarchyFolderId)).toBeNull();
    expect(screen.getByText('상세 변경 3건 보기')).toBeInTheDocument();

    fireEvent.click(screen.getByText('상세 변경 3건 보기'));
    expect(screen.getByText('MOVE · Document A.pdf')).toBeVisible();
    expect(screen.getByText('ADD · Document C.pdf')).toBeVisible();
  });

  it('shows an exact Korean message when an inherited document is added again', async () => {
    let draftMutation: any;
    (changeDocMindDraft as jest.Mock).mockResolvedValue({
      data: {
        code: 102,
        message: 'DOCMIND_DRAFT_DOCUMENT_ALREADY_IN_PARENT',
      },
    });
    mockUseMutation.mockImplementation((options: any) => {
      if (options.mutationKey?.[0] === 'docmind-draft-change') {
        draftMutation = options;
      }
      return {
        data: automaticResult,
        isPending: false,
        isSuccess: true,
        isError: false,
        mutate,
      };
    });

    render(React.createElement(DocMind));

    await expect(
      draftMutation.mutationFn({
        draftId: 'draft-1',
        change: {
          operation: 'ADD',
          documentId: 'document-published',
          registrationId: 'registration-published',
          toFolderId: 'validation',
        },
      }),
    ).rejects.toThrow(
      '이 문서는 현재 Catalog에 이미 포함되어 있어 다시 추가할 필요가 없습니다.',
    );
  });

  it('submits canonical manual folder IDs only when the user submits', () => {
    render(React.createElement(DocMind));

    fireEvent.change(
      screen.getByPlaceholderText('문서에서 찾고 싶은 내용을 입력하세요'),
      {
        target: { value: '변경관리' },
      },
    );
    fireEvent.click(screen.getByRole('button', { name: /폴더: 전체\(자동\)/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Validation/ }));
    fireEvent.click(
      screen.getByRole('checkbox', { name: /Quality Risk Management/ }),
    );
    expect(mutate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '검색 실행' }));

    expect(mutate).toHaveBeenCalledWith({
      question: '변경관리',
      folderIds: ['quality-risk-management', 'validation'],
      catalogVersionId: 'static-version',
    });
  });

  it('omits folder scope for automatic search', () => {
    render(React.createElement(DocMind));

    const searchButton = screen.getByRole('button', { name: '검색 실행' });
    expect(searchButton).toBeDisabled();

    fireEvent.change(
      screen.getByPlaceholderText('문서에서 찾고 싶은 내용을 입력하세요'),
      {
        target: { value: 'CAPA' },
      },
    );
    expect(searchButton).toBeEnabled();
    fireEvent.click(searchButton);

    expect(mutate).toHaveBeenCalledWith({ question: 'CAPA' });
  });

  it('renders the effective scope returned by the server', () => {
    render(React.createElement(DocMind));

    expect(
      screen.getByText('자동 선택: Quality Risk Management, Validation'),
    ).toBeInTheDocument();
  });

  it('retries an empty manual result across all folders only after an explicit click', () => {
    mockUseMutation.mockReturnValue({
      data: {
        ...automaticResult,
        chunks: [],
        ranked_chunks: [],
        ranked_total: 0,
        total: 0,
        candidate_count: 0,
        scope_mode: 'manual',
        effective_folder_ids: ['validation'],
      },
      isPending: false,
      isSuccess: true,
      isError: false,
      mutate,
    });
    render(React.createElement(DocMind));
    fireEvent.change(
      screen.getByPlaceholderText('문서에서 찾고 싶은 내용을 입력하세요'),
      {
        target: { value: 'qualification' },
      },
    );

    expect(mutate).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole('button', { name: '전체 폴더에서 다시 검색' }),
    );

    expect(mutate).toHaveBeenCalledWith({ question: 'qualification' });
  });

  it('prompts and refreshes when the server rejects a stale folder version', async () => {
    mockUseMutation.mockReturnValue({
      data: undefined,
      isPending: false,
      isSuccess: false,
      isError: true,
      error: new Error('DOCMIND_FOLDER_SELECTION_STALE: refresh folders'),
      mutate,
    });
    render(React.createElement(DocMind));

    expect(
      screen.getByText('폴더 목록이 변경되었습니다. 다시 선택해 주세요.'),
    ).toBeInTheDocument();
    expect(mutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '폴더 목록 새로고침' }));

    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));
    expect(mutate).not.toHaveBeenCalled();
  });

  it('does not expose Train B administration entry points', () => {
    render(React.createElement(DocMind));

    expect(screen.queryByRole('button', { name: /문서 등록/ })).toBeNull();
    expect(screen.queryByText(/Publish/)).toBeNull();
  });
});

describe('DocMind manual routing card revision', () => {
  const refetch = jest.fn();
  const manualMutate = jest.fn();
  const deleteMutate = jest.fn();
  let manualMutationOptions: any;
  let deleteMutationOptions: any;
  let manualPending = false;
  let manualError: Error | undefined;

  const routingCards = folders.map((folder, ordinal) => ({
    folder_id: folder.id,
    folder_name: folder.name,
    l0: `${folder.name} 한국어 L0 ${ordinal}`,
    l1: `${folder.name} 한국어 상세 L1 ${ordinal}`,
  }));
  const sourceReady = {
    draft_id: 'source-ready',
    version_label: 'DRAFT-SOURCE',
    parent_version_id: 'active-v1',
    lifecycle_state: 'READY',
    health_state: 'VALID',
    snapshot_hash: 'a'.repeat(64),
    active_parent_is_current: true,
    change_count: 1,
    has_effective_changes: true,
    generation_progress: 100,
    digest_ready_count: 25,
    membership_count: 25,
    card_ready_count: 5,
    can_generate: false,
    folders: folders.map((folder, ordinal) => ({ ...folder, ordinal })),
    changes: [],
    routing_cards: routingCards,
  };
  const manualReady = {
    ...sourceReady,
    draft_id: 'manual-ready',
    version_label: 'DRAFT-MANUAL',
    snapshot_hash: 'b'.repeat(64),
    readiness_mode: 'ADMIN_SAVED',
    search_validation_performed: false,
    routing_cards: routingCards.map((card) => ({
      ...card,
      l0: `${card.l0} 수정본`,
    })),
  };
  const versions = [
    {
      version_id: 'active-v1',
      version_label: 'V1',
      lifecycle_state: 'PUBLISHED',
      health_state: 'VALID',
      membership_count: 24,
      active: true,
      publish_allowed: false,
      rollback_allowed: false,
      delete_allowed: false,
      deletion_pending: false,
    },
    {
      version_id: 'source-ready',
      version_label: 'DRAFT-SOURCE',
      parent_version_id: 'active-v1',
      lifecycle_state: 'READY',
      health_state: 'VALID',
      membership_count: 25,
      active: false,
      publish_allowed: true,
      rollback_allowed: false,
      delete_allowed: false,
      deletion_pending: false,
      validation_report_hash: 'c'.repeat(64),
    },
    {
      version_id: 'manual-ready',
      version_label: 'DRAFT-MANUAL',
      parent_version_id: 'active-v1',
      lifecycle_state: 'READY',
      health_state: 'VALID',
      membership_count: 25,
      active: false,
      publish_allowed: true,
      rollback_allowed: false,
      delete_allowed: false,
      deletion_pending: false,
      readiness_mode: 'ADMIN_SAVED',
      search_validation_performed: false,
    },
    {
      version_id: 'failed-history',
      version_label: 'DRAFT-FAILED-HISTORY',
      parent_version_id: 'active-v1',
      lifecycle_state: 'FAILED',
      health_state: 'INVALID',
      health_reason: 'DOCMIND_TEST_FAILURE',
      membership_count: 25,
      active: false,
      publish_allowed: false,
      rollback_allowed: false,
      delete_allowed: true,
      deletion_pending: false,
    },
  ];

  beforeEach(() => {
    jest.clearAllMocks();
    manualMutationOptions = undefined;
    deleteMutationOptions = undefined;
    deleteMutate.mockReset();
    manualPending = false;
    manualError = undefined;
    mockUseQuery.mockImplementation((options: any) => {
      const [key, id] = options.queryKey;
      if (key === 'docmind-folders') {
        return {
          data: {
            catalog_source: 'database',
            catalog_version_id: 'active-v1',
            folders,
            can_administer: true,
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-registrations') {
        return {
          data: {
            project_id: 'project-1',
            catalog_version_id: 'active-v1',
            registrations: [],
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-drafts') {
        return {
          data: {
            project_id: 'project-1',
            active_version_id: 'active-v1',
            drafts: [sourceReady],
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-catalog-versions') {
        return {
          data: {
            project_id: 'project-1',
            active_version_id: 'active-v1',
            catalog_source_mode: 'database',
            versions,
          },
          isError: false,
          refetch,
        };
      }
      if (key === 'docmind-draft-preview') {
        return {
          data: id === 'manual-ready' ? manualReady : sourceReady,
          isPending: false,
          isError: false,
          refetch,
        };
      }
      return { data: undefined, isError: false, refetch };
    });
    mockUseMutation.mockImplementation((options: any) => {
      if (options.mutationKey?.[0] === 'docmind-manual-card-revision') {
        manualMutationOptions = options;
        return {
          isPending: manualPending,
          isError: Boolean(manualError),
          error: manualError,
          mutate: manualMutate,
          reset: jest.fn(),
        };
      }
      if (options.mutationKey?.[0] === 'docmind-catalog-version-delete') {
        deleteMutationOptions = options;
        return {
          isPending: false,
          isError: false,
          mutate: deleteMutate,
        };
      }
      return {
        data: automaticResult,
        isPending: false,
        isSuccess: true,
        isError: false,
        mutate: jest.fn(),
      };
    });
  });

  const openPublishReview = () => {
    render(React.createElement(DocMind));
    fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));
    fireEvent.click(screen.getByRole('button', { name: 'Publish 검토' }));
  };

  it('preserves unsaved L0 edits while moving to search and back to Catalog', () => {
    openPublishReview();
    fireEvent.click(screen.getByRole('button', { name: '수정' }));
    const editedText = '화면을 이동해도 유지되어야 하는 한국어 폴더 설명';
    fireEvent.change(screen.getByLabelText('Quality Risk Management L0'), {
      target: { value: editedText },
    });
    expect(screen.getByRole('button', { name: '검토 닫기' })).toBeDisabled();
    fireEvent.click(screen.getByRole('link', { name: '검색' }));
    fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));
    expect(screen.getByLabelText('Quality Risk Management L0')).toHaveValue(
      editedText,
    );
    expect(manualMutate).not.toHaveBeenCalled();
  });

  it('hides every older lifecycle until history is opened, without deleting records', () => {
    versions.push({
      ...versions[1],
      version_id: 'older-working',
      version_label: 'OLDER-WORKING',
      lifecycle_state: 'DRAFT',
      publish_allowed: false,
    });
    try {
      render(React.createElement(DocMind));
      fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));
      expect(
        screen.getByRole('region', { name: '현재 검색 버전' }),
      ).toHaveTextContent('V1');
      const history = screen.getByRole('region', { name: 'Catalog 이력' });
      expect(
        within(history).queryByText(
          /DRAFT-SOURCE|DRAFT-MANUAL|DRAFT-FAILED-HISTORY|OLDER-WORKING/,
        ),
      ).toBeNull();
      fireEvent.click(
        within(history).getByRole('button', { name: /이전 버전·기록 \d+개/ }),
      );
      for (const label of [
        'DRAFT-MANUAL',
        'DRAFT-FAILED-HISTORY',
        'OLDER-WORKING',
      ]) {
        expect(within(history).getByText(new RegExp(label))).toBeVisible();
      }
      expect(within(history).queryByText(/DRAFT-SOURCE/)).toBeNull();
      fireEvent.click(
        within(history).getByRole('button', { name: /이전 버전·기록 닫기/ }),
      );
      expect(within(history).queryByText(/OLDER-WORKING/)).toBeNull();
      expect(deleteMutate).not.toHaveBeenCalled();
      expect(manualMutate).not.toHaveBeenCalled();
    } finally {
      versions.pop();
    }
  });

  it('hides failed versions by default and deletes only after confirmation', () => {
    const confirm = jest.spyOn(window, 'confirm').mockReturnValue(true);
    render(React.createElement(DocMind));
    fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));

    expect(screen.queryByText(/DRAFT-FAILED-HISTORY/)).toBeNull();
    fireEvent.click(
      screen.getByRole('button', { name: /이전 버전·기록 \d+개/ }),
    );
    expect(screen.getByText(/DRAFT-FAILED-HISTORY/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '실패 이력 삭제' }));

    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining('DRAFT-FAILED-HISTORY'),
    );
    expect(deleteMutate).toHaveBeenCalledWith({
      versionId: 'failed-history',
      versionLabel: 'DRAFT-FAILED-HISTORY',
      expectedActiveVersionId: 'active-v1',
    });
    confirm.mockRestore();
  });

  it('refetches version and draft state even when deletion loses its response', async () => {
    render(React.createElement(DocMind));

    await act(async () => {
      await deleteMutationOptions.onSettled();
    });

    expect(refetch).toHaveBeenCalledTimes(2);
  });

  it('shows a pending state instead of another delete button while queued', () => {
    const failedVersion = versions.find(
      (version) => version.version_id === 'failed-history',
    )!;
    failedVersion.delete_allowed = false;
    failedVersion.deletion_pending = true;
    try {
      render(React.createElement(DocMind));
      fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));
      fireEvent.click(
        screen.getByRole('button', { name: /이전 버전·기록 \d+개/ }),
      );

      expect(screen.getByText('삭제 중…')).toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: '실패 이력 삭제' }),
      ).toBeNull();
    } finally {
      failedVersion.delete_allowed = true;
      failedVersion.deletion_pending = false;
    }
  });

  it('keeps Publish review read-only until 수정 and discards local edits on 수정 취소', () => {
    openPublishReview();

    expect(screen.queryAllByRole('textbox')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '수정' }));
    expect(screen.getAllByRole('textbox')).toHaveLength(10);
    const l0 = screen.getByLabelText('Quality Risk Management L0');
    fireEvent.change(l0, { target: { value: '수정한 한국어 L0' } });
    expect(l0).toHaveValue('수정한 한국어 L0');
    expect(screen.getAllByText(/\/800 · 한국어 포함/).length).toBeGreaterThan(
      0,
    );

    fireEvent.click(screen.getByRole('button', { name: '수정 취소' }));
    expect(
      screen.queryByLabelText('Quality Risk Management L0'),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '수정' }));
    expect(screen.getByLabelText('Quality Risk Management L0')).toHaveValue(
      routingCards[0].l0,
    );
  });

  it('sends the exact five canonical cards with active and source CAS values', () => {
    openPublishReview();
    fireEvent.click(screen.getByRole('button', { name: '수정' }));
    fireEvent.change(screen.getByLabelText('Quality Operations L1'), {
      target: { value: '관리자가 직접 작성한 한국어 L1' },
    });
    fireEvent.click(screen.getByRole('button', { name: '저장' }));

    expect(manualMutate).toHaveBeenCalledWith({
      idempotencyKey: expect.any(String),
      revision: {
        sourceReadyVersionId: 'source-ready',
        expectedActiveVersionId: 'active-v1',
        expectedSourceSnapshotHash: 'a'.repeat(64),
        cards: routingCards.map(({ folder_id, l0, l1 }) => ({
          folder_id,
          l0,
          l1:
            folder_id === 'quality-operations'
              ? '관리자가 직접 작성한 한국어 L1'
              : l1,
        })),
      },
    });
  });

  it('reuses the idempotency key after a failed unchanged save and rotates it after an edit', () => {
    const view = render(React.createElement(DocMind));
    fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));
    fireEvent.click(screen.getByRole('button', { name: 'Publish 검토' }));
    fireEvent.click(screen.getByRole('button', { name: '수정' }));

    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    const firstKey = manualMutate.mock.calls[0][0].idempotencyKey;

    manualError = new Error('일시적인 저장 오류');
    view.rerender(React.createElement(DocMind));
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    const retryKey = manualMutate.mock.calls[1][0].idempotencyKey;
    expect(retryKey).toBe(firstKey);

    fireEvent.change(screen.getByLabelText('Validation L0'), {
      target: { value: '새로운 한국어 설명으로 변경' },
    });
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    const changedPayloadKey = manualMutate.mock.calls[2][0].idempotencyKey;
    expect(changedPayloadKey).not.toBe(firstKey);
  });

  it('disables Save and Publish while a manual save is pending', () => {
    const view = render(React.createElement(DocMind));
    fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));
    fireEvent.click(screen.getByRole('button', { name: 'Publish 검토' }));
    fireEvent.click(screen.getByRole('button', { name: '수정' }));

    manualPending = true;
    view.rerender(React.createElement(DocMind));

    expect(screen.getByRole('button', { name: '저장 중' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Publish 실행' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '수정 취소' })).toBeDisabled();
  });

  it('selects the returned ADMIN_SAVED candidate and shows its badge', async () => {
    openPublishReview();

    await act(async () => {
      await manualMutationOptions.onSuccess({
        draft_id: 'manual-ready',
        version_label: 'DRAFT-MANUAL',
        parent_version_id: 'active-v1',
        source_ready_version_id: 'source-ready',
        lifecycle_state: 'READY',
        health_state: 'VALID',
        snapshot_hash: 'b'.repeat(64),
        routing_card_set_hash: 'd'.repeat(64),
        root_identity_sha256: 'e'.repeat(64),
        manual_save_report_hash: 'f'.repeat(64),
        readiness_mode: 'ADMIN_SAVED',
        search_validation_performed: false,
      });
    });

    expect(refetch).toHaveBeenCalled();
    expect(
      screen.getByText('관리자 저장본 · 검색 검증 안 함'),
    ).toBeInTheDocument();
    expect(screen.getByText(/저장 무결성 보고서/)).toBeInTheDocument();
    expect(
      screen.getByText(`${routingCards[0].l0} 수정본`),
    ).toBeInTheDocument();
  });

  it('retains local card edits when a save fails', () => {
    const view = render(React.createElement(DocMind));
    fireEvent.click(screen.getByRole('link', { name: 'Catalog' }));
    fireEvent.click(screen.getByRole('button', { name: 'Publish 검토' }));
    fireEvent.click(screen.getByRole('button', { name: '수정' }));
    fireEvent.change(screen.getByLabelText('Validation L0'), {
      target: { value: '오류 후에도 남아야 하는 한국어 설명' },
    });

    manualError = new Error('저장하지 못했습니다. 입력 내용은 유지됩니다.');
    view.rerender(React.createElement(DocMind));

    expect(screen.getByLabelText('Validation L0')).toHaveValue(
      '오류 후에도 남아야 하는 한국어 설명',
    );
    expect(
      screen.getByText('저장하지 못했습니다. 입력 내용은 유지됩니다.'),
    ).toBeInTheDocument();
  });

  it('translates stable manual save errors for the administrator', async () => {
    (createDocMindManualCardRevision as jest.Mock).mockResolvedValue({
      data: {
        code: 102,
        message: 'DOCMIND_MANUAL_CARD_SOURCE_STALE',
      },
    });
    openPublishReview();

    await expect(
      manualMutationOptions.mutationFn({
        revision: {
          sourceReadyVersionId: 'source-ready',
          expectedActiveVersionId: 'active-v1',
          expectedSourceSnapshotHash: 'a'.repeat(64),
          cards: routingCards,
        },
        idempotencyKey: 'stable-save-attempt',
      }),
    ).rejects.toThrow(
      '기준 Catalog가 변경되었습니다. 목록을 새로고침한 후 다시 확인하세요.',
    );
  });
});
