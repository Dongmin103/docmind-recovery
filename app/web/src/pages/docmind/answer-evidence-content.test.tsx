import knowledgeService from '@/services/knowledge-service';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import AnswerEvidence from './answer-evidence';

jest.mock('@/services/knowledge-service', () => ({
  __esModule: true,
  default: { getChunk: jest.fn() },
}));

type Citation =
  import('@/services/docmind-service').DocMindAnswerResult['citations'][number];
const Citation: Citation = {
  number: 1,
  chunk_id: 'chunk-synthetic',
  doc_id: 'document-synthetic',
  document_name: 'synthetic.xlsx',
  chunk_set_id: 'set-synthetic',
  source_version_id: 'version-synthetic',
  content: '검색용 합성 표 텍스트\n둘째 줄',
};
const TableHtml =
  '<table data-sheet="요약"><caption>합성 표</caption><colgroup><col span="2"></colgroup><thead><tr><th colspan="2" scope="colgroup">항목</th></tr></thead><tbody><tr><td rowspan="2" data-cell="A2:A3">시험</td><td>적합<br>확인</td></tr><tr><td>완료</td></tr></tbody></table>';
const Chunk: import('@/interfaces/database/dataset').IChunk = {
  available_int: 1,
  chunk_id: Citation.chunk_id,
  doc_id: Citation.doc_id,
  doc_name: Citation.document_name,
  chunk_set_id: Citation.chunk_set_id,
  source_version_id: Citation.source_version_id,
  content_with_weight: Citation.content,
  display_html: TableHtml,
  image_id: '',
  positions: [],
  provenance: [
    { kind: 'xlsx', sheet: '요약', cell_range: 'A1:B3' },
    { kind: 'xlsx', sheet: '요약', cell_range: 'A1:B3' },
  ],
};
const MockGetChunk = knowledgeService.getChunk as jest.Mock;
const response = (chunk: unknown) => ({ data: { code: 0, data: chunk } });

function renderEvidence(citation: Citation | undefined, datasetId?: string) {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: 2,
        retryDelay: 0,
        placeholderData: (previous: unknown) => previous,
      },
    },
  });
  const tree = (
    selected: Citation | undefined,
    dataset: string | undefined,
  ) => (
    <QueryClientProvider client={client}>
      <AnswerEvidence
        citation={selected}
        datasetId={dataset}
        onClose={jest.fn()}
      />
    </QueryClientProvider>
  );
  const view = render(tree(citation, datasetId));
  return {
    ...view,
    switchTo: (selected: Citation | undefined, dataset: string | undefined) =>
      view.rerender(tree(selected, dataset)),
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  MockGetChunk.mockResolvedValue(response(Chunk));
});

it('loads only the exact selected chunk and shows stored XLSX cells and location once', async () => {
  const view = renderEvidence(Citation, 'dataset-synthetic');
  const table = await screen.findByRole('table');
  expect(MockGetChunk).toHaveBeenCalledWith({
    kb_id: 'dataset-synthetic',
    doc_id: 'document-synthetic',
    chunk_id: 'chunk-synthetic',
  });
  expect(screen.getByRole('columnheader', { name: '항목' })).toHaveAttribute(
    'colspan',
    '2',
  );
  expect(screen.getByRole('cell', { name: '시험' })).toHaveAttribute(
    'rowspan',
    '2',
  );
  expect(screen.getByRole('cell', { name: '시험' })).toHaveAttribute(
    'data-cell',
    'A2:A3',
  );
  expect(table).toHaveAttribute('data-sheet', '요약');
  expect(table.querySelector('col')).toHaveAttribute('span', '2');
  expect(screen.getAllByText('요약!A1:B3')).toHaveLength(1);
  expect(view.container).not.toHaveTextContent(Citation.content);
  expect(view.container).not.toHaveTextContent('<table');
  expect(table.closest('.chunk-content')).toHaveClass(
    'overflow-x-auto',
    'max-w-full',
  );
});

it('keeps plain source text and line breaks without interpreting Markdown or guessing location', async () => {
  const citation = {
    ...Citation,
    content: '# literal heading\n**literal emphasis**\nA   B',
  };
  MockGetChunk.mockResolvedValue(
    response({
      ...Chunk,
      content_with_weight: citation.content,
      display_html: undefined,
      provenance: [],
    }),
  );
  const view = renderEvidence(citation, 'dataset-synthetic');
  await waitFor(() =>
    expect(screen.queryByRole('status')).not.toBeInTheDocument(),
  );
  expect(view.container.textContent).toContain(citation.content);
  expect(view.container.querySelector('h1, strong')).toBeNull();
  expect(screen.queryByText('문서 위치')).not.toBeInTheDocument();
  expect(screen.queryByText(/p\. /)).not.toBeInTheDocument();
});

it.each([undefined, '', '  ', 42])(
  'uses the original citation when display HTML is absent or invalid (%s)',
  async (display_html) => {
    MockGetChunk.mockResolvedValue(response({ ...Chunk, display_html }));
    const view = renderEvidence(Citation, 'dataset-synthetic');
    await waitFor(() =>
      expect(screen.queryByRole('status')).not.toBeInTheDocument(),
    );
    expect(view.container.textContent).toContain(Citation.content);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  },
);

it('renders legacy HTML table fallback safely without duplicate plaintext', async () => {
  const citation = {
    ...Citation,
    content:
      '<table><tr><td>한 번만 표시</td></tr></table><img src="https://invalid.example/track">',
  };
  const view = renderEvidence(citation);
  expect(screen.getByRole('table')).toBeInTheDocument();
  expect(screen.getAllByText('한 번만 표시')).toHaveLength(1);
  expect(view.container.querySelector('img')).toBeNull();
  expect(view.container).not.toHaveTextContent('<table');
  expect(MockGetChunk).not.toHaveBeenCalled();
});

it('does not load evidence until citation and dataset are both present', async () => {
  const view = renderEvidence(undefined, 'dataset-synthetic');
  expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
  expect(MockGetChunk).not.toHaveBeenCalled();
  view.switchTo(Citation, undefined);
  expect(view.container.textContent).toContain(Citation.content);
  expect(MockGetChunk).not.toHaveBeenCalled();
  view.switchTo(Citation, 'dataset-synthetic');
  expect(await screen.findByRole('table')).toBeInTheDocument();
});

it('keeps citation text readable during pending and rejected requests without retries', async () => {
  let reject: (error: Error) => void = () => {};
  MockGetChunk.mockImplementationOnce(
    () =>
      new Promise((_resolve, fail) => {
        reject = fail;
      }),
  );
  const view = renderEvidence(Citation, 'dataset-synthetic');
  expect(view.container.textContent).toContain(Citation.content);
  expect(screen.getByRole('status')).toBeInTheDocument();
  await act(async () => reject(new Error('synthetic failure')));
  await waitFor(() =>
    expect(screen.queryByRole('status')).not.toBeInTheDocument(),
  );
  expect(view.container.textContent).toContain(Citation.content);
  expect(screen.getByText(/표시 정보를 불러올 수 없/)).toBeInTheDocument();
  expect(MockGetChunk).toHaveBeenCalledTimes(1);
});

it.each([
  ['set changed', { chunk_set_id: 'new-set' }],
  ['set missing', { chunk_set_id: undefined }],
  ['text changed', { content_with_weight: '현재 변경된 텍스트' }],
  ['text missing', { content_with_weight: undefined }],
  ['chunk wrong', { chunk_id: 'other-chunk' }],
  ['chunk missing', { chunk_id: undefined }],
  ['doc wrong', { doc_id: 'other-doc' }],
  ['doc missing', { doc_id: undefined }],
  ['source version changed', { source_version_id: 'new-version' }],
])('rejects fetched display when %s', async (_name, fields) => {
  MockGetChunk.mockResolvedValue(response({ ...Chunk, ...fields }));
  const view = renderEvidence(Citation, 'dataset-synthetic');
  await waitFor(() =>
    expect(screen.queryByRole('status')).not.toBeInTheDocument(),
  );
  expect(view.container.textContent).toContain(Citation.content);
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
  expect(screen.queryByText('요약!A1:B3')).not.toBeInTheDocument();
  expect(screen.getByText(/변경되었거나 확인할 수 없/)).toBeInTheDocument();
});

it.each([undefined, null, {}, [], { data: { code: 1, data: Chunk } }])(
  'retains citation for malformed or unsuccessful responses (%s)',
  async (value) => {
    MockGetChunk.mockResolvedValue(value);
    const view = renderEvidence(Citation, 'dataset-synthetic');
    await waitFor(() =>
      expect(screen.queryByRole('status')).not.toBeInTheDocument(),
    );
    expect(view.container.textContent).toContain(Citation.content);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(MockGetChunk).toHaveBeenCalledTimes(1);
  },
);

it('accepts matching legacy chunks without optional set/version and real PDF metadata', async () => {
  const citation = {
    ...Citation,
    chunk_set_id: undefined,
    source_version_id: undefined,
  };
  MockGetChunk.mockResolvedValue(
    response({
      ...Chunk,
      chunk_set_id: undefined,
      source_version_id: undefined,
      provenance: [],
      positions: [[3, 0, 1, 0, 1]],
    }),
  );
  renderEvidence(citation, 'dataset-synthetic');
  expect(await screen.findByRole('table')).toBeInTheDocument();
  expect(screen.getByText('p. 3')).toBeInTheDocument();
});

it('accepts a matching source version when the endpoint omits that optional field', async () => {
  MockGetChunk.mockResolvedValue(
    response({ ...Chunk, source_version_id: undefined }),
  );
  renderEvidence(Citation, 'dataset-synthetic');
  expect(await screen.findByRole('table')).toBeInTheDocument();
});

it.each(['citation', 'dataset', 'snapshot', 'set', 'version'])(
  'does not reuse pending or cached display across a %s switch',
  async (kind) => {
    let resolveOld: (value: unknown) => void = () => {};
    let resolveNew: (value: unknown) => void = () => {};
    MockGetChunk.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    MockGetChunk.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveNew = resolve;
        }),
    );
    const view = renderEvidence(Citation, 'dataset-synthetic');
    const next = {
      ...Citation,
      ...(kind === 'citation'
        ? { chunk_id: 'second-chunk', doc_id: 'second-doc' }
        : {}),
      ...(kind === 'snapshot' ? { content: '새 인용 텍스트' } : {}),
      ...(kind === 'set' ? { chunk_set_id: 'second-set' } : {}),
      ...(kind === 'version' ? { source_version_id: 'second-version' } : {}),
    };
    const dataset = kind === 'dataset' ? 'dataset-second' : 'dataset-synthetic';
    view.switchTo(next, dataset);
    await act(async () => resolveOld(response(Chunk)));
    expect(view.container.textContent).toContain(next.content);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    await act(async () =>
      resolveNew(
        response({
          ...Chunk,
          chunk_id: next.chunk_id,
          doc_id: next.doc_id,
          content_with_weight: next.content,
          chunk_set_id: next.chunk_set_id,
          source_version_id: next.source_version_id,
          display_html: '<table><tr><td>선택한 새 근거</td></tr></table>',
        }),
      ),
    );
    expect(await screen.findByRole('table')).toHaveTextContent(
      '선택한 새 근거',
    );
    view.switchTo(Citation, 'dataset-synthetic');
    expect(screen.queryByText('선택한 새 근거')).not.toBeInTheDocument();
    expect(await screen.findByRole('table')).toHaveTextContent('시험');
    expect(screen.queryByText('선택한 새 근거')).not.toBeInTheDocument();
  },
);

it('removes active HTML and all URL/style attributes while retaining table spans', async () => {
  const unsafe =
    '<script>bad()</script><iframe src="https://invalid.example/frame"></iframe><link rel="stylesheet" href="https://invalid.example/style"><style>@import "https://invalid.example/style";</style><img src="https://invalid.example/image" onerror="bad()"><video poster="https://invalid.example/poster"></video><svg><a xlink:href="javascript:bad()">svg</a><image href="https://invalid.example/svg"></image></svg><a href="javascript:bad()" ping="https://invalid.example/ping">안전한 링크 글자</a><table style="background:url(https://invalid.example/css)" onclick="bad()" class="injected"><tr><td rowspan="2" colspan="3" data-cell="A1:C2" style="position:fixed">안전한 셀<br>다음 줄</td></tr></table>';
  MockGetChunk.mockResolvedValue(response({ ...Chunk, display_html: unsafe }));
  const view = renderEvidence(Citation, 'dataset-synthetic');
  const table = await screen.findByRole('table');
  expect(
    view.container
      .querySelector('.chunk-content')
      ?.querySelector(
        'script, iframe, link, style, img, video, svg, [style], [onclick], [onerror], [href], [src], [ping], .injected',
      ),
  ).toBeNull();
  const cell = table.querySelector('td');
  expect(cell).toHaveAttribute('rowspan', '2');
  expect(cell).toHaveAttribute('colspan', '3');
  expect(cell).toHaveAttribute('data-cell', 'A1:C2');
  expect(cell?.querySelector('br')).not.toBeNull();
  expect(screen.getByText('안전한 링크 글자')).toBeInTheDocument();
});
