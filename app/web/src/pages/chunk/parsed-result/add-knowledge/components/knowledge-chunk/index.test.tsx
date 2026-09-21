import { act, fireEvent, render, screen } from '@testing-library/react';
import * as React from 'react';
import Chunk from './index';

jest.mock('@/utils/css-support', () => ({ supportsCssAnchor: false }));
jest.mock('@/components/ui/resizable', () => ({
  ResizableHandle: () => <div />,
  ResizablePanel: ({ children }: any) => <div>{children}</div>,
  ResizablePanelGroup: ({ children }: any) => <div>{children}</div>,
}));

Object.assign(globalThis, { React });

let mockSearch = '';
let mockLayoutSize = { width: 1100, height: 700 };
let mockDocumentInfo = { name: 'document.pdf', parser_id: 'na', type: 'pdf' };
let mockFilteredChunkIds: string[] = [];
const mockSetLayout = jest.fn();

jest.mock('ahooks', () => ({
  ...jest.requireActual('ahooks'),
  useSize: () => mockLayoutSize,
}));
jest.mock('react-resizable-panels', () => ({
  PanelGroup: jest
    .requireActual('react')
    .forwardRef(({ children, direction }: any, ref: any) => {
      const ReactLib = jest.requireActual('react');
      ReactLib.useImperativeHandle(ref, () => ({ setLayout: mockSetLayout }));
      return ReactLib.createElement(
        'div',
        { 'data-testid': 'panel-group', 'data-direction': direction },
        children,
      );
    }),
}));
const mockBack = jest.fn();
let mockChunkUpdatingVisible = false;
let mockChunks: Array<{ chunk_id: string }> = [];
let mockInspectionError: Error | null = null;
const mockRetryInspection = jest.fn();

jest.mock('react-router', () => ({
  useLocation: () => ({ pathname: '/chunk/parsed/chunks' }),
  useNavigate: () => jest.fn(),
  useSearchParams: () => [new URLSearchParams(mockSearch)],
}));

jest.mock('@/routes', () => ({
  Routes: { DataflowResult: '/dataflow-result' },
}));

jest.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

jest.mock('@/hooks/logic-hooks/navigate-hooks', () => ({
  QueryStringMap: { id: 'id' },
  useNavigatePage: () => ({
    getQueryString: () => 'dataset-1',
    navigateToDataFile: () => mockBack,
  }),
}));

jest.mock('@/hooks/use-chunk-request', () => ({
  useFetchNextChunkList: (
    _enabled: boolean,
    options: { chunkIds: string[] },
  ) => {
    mockFilteredChunkIds = options.chunkIds;
    return {
      data: {
        data: mockChunks,
        documentInfo: mockDocumentInfo,
        total: 0,
      },
      pagination: { current: 1, onChange: jest.fn(), pageSize: 10 },
      loading: false,
      searchString: '',
      handleInputChange: jest.fn(),
      available: undefined,
      handleSetAvailable: jest.fn(),
      dataUpdatedAt: 0,
      error: mockInspectionError,
      retry: mockRetryInspection,
    };
  },
  useSwitchChunk: () => ({ switchChunk: jest.fn() }),
}));

jest.mock('./hooks', () => ({
  useChangeChunkTextMode: () => ({
    changeChunkTextMode: jest.fn(),
    textMode: 'full',
  }),
  useDeleteChunkByIds: () => ({ removeChunk: jest.fn() }),
  useGetChunkHighlights: () => ({
    highlights: [],
    setWidthAndHeight: jest.fn(),
  }),
  useHandleChunkCardClick: () => ({
    handleChunkCardClick: jest.fn(),
    selectedChunkId: '',
  }),
  useUpdateChunk: () => ({
    chunkUpdatingLoading: false,
    onChunkUpdatingOk: jest.fn(),
    showChunkUpdatingModal: jest.fn(),
    hideChunkUpdatingModal: jest.fn(),
    chunkId: undefined,
    chunkUpdatingVisible: mockChunkUpdatingVisible,
    documentId: 'document-1',
  }),
}));

jest.mock('@/components/document-preview', () => ({ fileType }: any) => (
  <div data-testid="source-preview" data-type={fileType} />
));
jest.mock(
  '@/components/document-preview/document-header',
  () =>
    ({ children }: any) => <div>{children}</div>,
);
jest.mock('./components/document-view-switch', () =>
  jest.requireActual('./components/document-view-switch'),
);
jest.mock('@/components/ui/segmented', () => ({
  Segmented: ({ options, onChange }: any) => (
    <div>
      {options.map((option: any) => (
        <button key={option.value} onClick={() => onChange(option.value)}>
          {option.label}
        </button>
      ))}
    </div>
  ),
}));
jest.mock('@/pages/chunk/representation', () => ({ onNodeClick }: any) => (
  <button onClick={() => onNodeClick({ source_chunk_ids: ['chunk-1'] })}>
    Artifact node
  </button>
));
jest.mock('@/components/document-preview/hooks', () => ({
  useGetDocumentUrl: () => '',
}));
jest.mock('@/components/ui/message', () => ({ warning: jest.fn() }));
jest.mock('@/components/ui/ragflow-pagination', () => ({
  RAGFlowPagination: () => null,
}));
jest.mock('@/components/ui/spin', () => ({
  Spin: ({ children }: { children?: JSX.Element }) => children ?? null,
}));
jest.mock('./components/chunk-card', () => ({ readOnly }: any) => (
  <article data-testid="chunk-card">
    {!readOnly && <button>edit chunk</button>}
  </article>
));
jest.mock('./components/chunk-creating-modal', () => () => (
  <div role="dialog">chunk editor</div>
));
jest.mock('./components/chunk-result-bar', () => ({ readOnly }: any) => (
  <div>
    <input aria-label="search chunks" />
    {!readOnly && <button>add chunk</button>}
  </div>
));
jest.mock('./components/chunk-result-bar/checkbox-sets', () => () => (
  <button>delete chunks</button>
));
jest.mock('lucide-react', () => ({
  LucideArrowBigLeft: () => null,
  File: () => null,
  LayoutList: () => null,
}));

const renderChunk = (search = '') => {
  mockSearch = search;
  return render(<Chunk />);
};

beforeEach(() => {
  mockBack.mockClear();
  mockLayoutSize = { width: 1100, height: 700 };
  mockDocumentInfo = { name: 'document.pdf', parser_id: 'na', type: 'pdf' };
  mockFilteredChunkIds = [];
  mockSetLayout.mockClear();
  mockChunkUpdatingVisible = false;
  mockChunks = [];
  mockInspectionError = null;
  mockRetryInspection.mockClear();
});

describe('RAGFlow chunk inspection source', () => {
  it('shows the DocMind source beside the existing Back button', () => {
    renderChunk('?id=dataset-1&doc_id=document-1&source=docmind');

    expect(screen.getByRole('button', { name: 'common.back' })).toBeVisible();
    expect(screen.getByText('DocMind에서 연 인덱싱 검사')).toBeVisible();
  });

  it.each([
    '',
    '?source=ragflow',
    '?source=DocMind',
    '?source=https%3A%2F%2Fexample.com%2Fdocmind',
  ])('does not show the source badge for unsupported source %p', (search) => {
    renderChunk(search);

    expect(screen.getByRole('button', { name: 'common.back' })).toBeVisible();
    expect(
      screen.queryByText('DocMind에서 연 인덱싱 검사'),
    ).not.toBeInTheDocument();
  });
});

describe('embedded read-only chunk viewer', () => {
  it('uses the embedded layout and invokes the supplied back callback', () => {
    const onBack = jest.fn();

    const { container } = render(<Chunk embedded readOnly onBack={onBack} />);

    expect(container.querySelector('main')).toHaveClass('h-full', 'min-h-0');
    expect(container.querySelector('main')).not.toHaveClass('h-dvh');
    expect(screen.getByText('document.pdf')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));

    expect(onBack).toHaveBeenCalledTimes(1);
    expect(mockBack).not.toHaveBeenCalled();
  });

  it('keeps inspection controls but removes every chunk mutation control', () => {
    mockChunks = [{ chunk_id: 'chunk-1' }];
    mockChunkUpdatingVisible = true;

    render(<Chunk embedded readOnly onBack={jest.fn()} />);

    expect(
      screen.getByRole('textbox', { name: 'search chunks' }),
    ).toBeVisible();
    expect(screen.getByTestId('chunk-card')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'add chunk' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'edit chunk' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'delete chunks' })).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows a recoverable inspection error instead of an empty document', () => {
    mockInspectionError = new Error('문서에 접근할 수 없습니다.');
    render(<Chunk embedded readOnly onBack={jest.fn()} />);
    expect(screen.getByRole('alert')).toHaveTextContent(
      '문서에 접근할 수 없습니다.',
    );
    expect(screen.queryByTestId('chunk-card')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '다시 불러오기' }));
    expect(mockRetryInspection).toHaveBeenCalledTimes(1);
  });

  it('preserves the standalone editable viewer by default', () => {
    mockChunks = [{ chunk_id: 'chunk-1' }];
    mockChunkUpdatingVisible = true;

    const { container } = renderChunk();

    expect(container.querySelector('main')).toHaveClass('h-dvh');
    expect(screen.getByRole('button', { name: 'add chunk' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'edit chunk' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'delete chunks' })).toBeVisible();
    expect(screen.getByRole('dialog')).toBeVisible();
  });
});

test('breakpoint changes preserve Artifact mode and its selected chunk filter', async () => {
  const view = render(<Chunk embedded readOnly />);
  fireEvent.click(screen.getByRole('button', { name: 'Artifact' }));
  fireEvent.click(screen.getByRole('button', { name: 'Artifact node' }));
  expect(mockFilteredChunkIds).toEqual(['chunk-1']);
  mockLayoutSize = { width: 564, height: 700 };
  await act(async () => view.rerender(<Chunk embedded readOnly />));
  expect(screen.getByRole('button', { name: 'Artifact node' })).toBeVisible();
  expect(mockFilteredChunkIds).toEqual(['chunk-1']);
  expect(mockSetLayout).toHaveBeenLastCalledWith([70, 30]);
  mockLayoutSize = { width: 1100, height: 700 };
  await act(async () => view.rerender(<Chunk embedded readOnly />));
  expect(screen.getByRole('button', { name: 'Artifact node' })).toBeVisible();
  expect(mockFilteredChunkIds).toEqual(['chunk-1']);
  expect(mockSetLayout).toHaveBeenLastCalledWith([40, 60]);
});

test.each(['XLS', 'XLSX'])(
  'normalizes %s document metadata before mounting the source preview',
  (extension) => {
    mockDocumentInfo = {
      name: 'table.' + extension,
      parser_id: 'na',
      type: 'doc',
    };
    render(<Chunk embedded readOnly />);
    expect(screen.getByTestId('source-preview')).toHaveAttribute(
      'data-type',
      extension.toLowerCase(),
    );
  },
);
