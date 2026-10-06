import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import {
  answerDocMind,
  getDocMindFolders,
  searchDocMind,
} from '@/services/docmind-service';
import DocMind from './index';

jest.mock('@/services/docmind-service', () => ({
  answerDocMind: jest.fn(),
  getDocMindFolders: jest.fn(),
  searchDocMind: jest.fn(),
  getDocMindHierarchy: jest.fn(),
  getDocMindRegistrations: jest.fn(),
}));
jest.mock('@/components/icon-font', () => ({ FileIcon: () => null }));
jest.mock('@/components/highlight-markdown', () => ({
  __esModule: true,
  default: ({ children }: any) => children,
}));
jest.mock('@/components/document-preview/temporary-original-preview', () => ({
  __esModule: true,
  default: () => null,
}));
jest.mock('@/components/originui/input', () => ({
  Input: (props: any) => <input {...props} />,
}));
jest.mock('@/components/ui/modal/modal', () => ({ Modal: () => null }));
jest.mock('@/hooks/use-document-request', () => ({
  useGetChunkHighlights: () => ({
    highlights: [],
    setWidthAndHeight: jest.fn(),
  }),
}));
jest.mock('./workspace-views', () => ({
  ChunkInspection: () => <p>청크 화면</p>,
  WorkspaceSettings: () => <p>설정 화면</p>,
}));

const Answer = {
  data: {
    code: 0,
    data: {
      answer: '합성 답변 [1]',
      dataset_id: 'dataset-1',
      citations: [
        {
          number: 1,
          chunk_id: 'chunk-1',
          doc_id: 'document-1',
          document_name: 'protocol.pdf',
          content: '<script>evidence</script>',
        },
      ],
      context: {
        seed_count: 1,
        expanded_count: 5,
        used_count: 5,
        neighbor_window: 2,
      },
    },
  },
};
const MockAnswer = answerDocMind as jest.Mock;
const MockSearch = searchDocMind as jest.Mock;

async function start() {
  const view = render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter>
        <DocMind />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await waitFor(() => expect(getDocMindFolders).toHaveBeenCalled());
  await waitFor(() =>
    expect(screen.getByLabelText('문서 검색 질문')).toBeInTheDocument(),
  );
  return view;
}
function question(value = '시험 질문') {
  fireEvent.change(screen.getByLabelText('문서 검색 질문'), {
    target: { value },
  });
}
function ai() {
  fireEvent.click(screen.getByRole('tab', { name: 'AI 질문' }));
}
function submit() {
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
}

beforeEach(() => {
  jest.clearAllMocks();
  (getDocMindFolders as jest.Mock).mockResolvedValue({
    data: {
      code: 0,
      data: {
        project_id: 'project-1',
        dataset_id: 'dataset-1',
        can_administer: false,
        folders: [{ id: 'folder-1', name: '품질', document_count: 1 }],
        documents: [
          {
            id: 'document-1',
            name: 'protocol.pdf',
            folder_id: 'folder-1',
            relative_path: '품질/protocol.pdf',
          },
        ],
      },
    },
  });
  MockAnswer.mockResolvedValue(Answer);
  MockSearch.mockResolvedValue({
    data: {
      code: 0,
      data: {
        dataset_id: 'dataset-1',
        total: 1,
        candidate_count: 1,
        chunks: [],
        ranked_chunks: [
          {
            doc_id: 'document-1',
            doc_name: 'protocol.pdf',
            content_with_weight: '검색 근거',
          },
        ],
      },
    },
  });
});

it('exposes both tabs before submitting and keeps existing search free of generation', async () => {
  await start();
  expect(screen.getByRole('tab', { name: '기존 검색' })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  expect(screen.getByRole('tab', { name: 'AI 질문' })).toHaveAttribute(
    'aria-selected',
    'false',
  );
  question();
  fireEvent.click(screen.getByLabelText('검색 실행'));
  expect(await screen.findByText('검색 근거')).toBeInTheDocument();
  expect(MockSearch).toHaveBeenCalledTimes(1);
  expect(MockAnswer).not.toHaveBeenCalled();
  expect(
    screen.queryByRole('region', { name: '문서 근거 답변' }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText('근거로 답변 만들기')).not.toBeInTheDocument();
});

it.each(['all', 'folders', 'documents'] as const)(
  'submits AI directly once with %s scope and five surrounding neighbors',
  async (mode) => {
    await start();
    ai();
    question();
    if (mode !== 'all') {
      fireEvent.click(screen.getByText('전체 문서'));
      fireEvent.click(
        screen.getByLabelText(
          mode === 'folders' ? '선택 폴더와 하위 폴더' : '선택 문서',
        ),
      );
      expect(screen.getByRole('button', { name: '질문하기' })).toBeDisabled();
      fireEvent.click(
        screen.getByLabelText(
          mode === 'folders' ? '품질' : '품질/protocol.pdf',
        ),
      );
    }
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    submit();
    expect(await screen.findByText('합성 답변 [1]')).toBeInTheDocument();
    expect(MockAnswer).toHaveBeenCalledTimes(1);
    expect(MockAnswer).toHaveBeenCalledWith({
      question: '시험 질문',
      projectId: 'project-1',
      scope:
        mode === 'all'
          ? { mode }
          : mode === 'folders'
            ? { mode, folderIds: ['folder-1'] }
            : { mode, documentIds: ['document-1'] },
      neighborWindow: 5,
    });
    expect(MockSearch).not.toHaveBeenCalled();
    expect(
      screen.getByText(/검색 근거 1개 · 문맥 확장 5개 · 실제 입력 5개/),
    ).toBeInTheDocument();
    expect(screen.getByText('<script>evidence</script>')).toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
  },
);

it('blocks duplicate pending submissions and retries a provider error only explicitly', async () => {
  let resolve: (value: unknown) => void = () => {};
  MockAnswer.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  await start();
  ai();
  question();
  submit();
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '질문하기' })).toBeDisabled(),
  );
  submit();
  fireEvent.submit(
    screen.getByRole('button', { name: '질문하기' }).closest('form')!,
  );
  expect(MockAnswer).toHaveBeenCalledTimes(1);
  await act(async () =>
    resolve({ data: { code: 100, message: 'DOCMIND_LLM_RATE_LIMITED' } }),
  );
  expect(await screen.findByRole('alert')).toHaveTextContent('요청 한도');
  expect(MockAnswer).toHaveBeenCalledTimes(1);
  submit();
  expect(await screen.findByText('합성 답변 [1]')).toBeInTheDocument();
  expect(MockAnswer).toHaveBeenCalledTimes(2);
});

it.each(['query', 'scope', 'tab', 'reset'] as const)(
  'discards pending and completed answers after %s changes without resubmitting',
  async (change) => {
    let resolve: (value: unknown) => void = () => {};
    MockAnswer.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    await start();
    ai();
    question();
    submit();
    await waitFor(() => expect(MockAnswer).toHaveBeenCalledTimes(1));
    if (change === 'query') question('다른 질문');
    if (change === 'reset')
      fireEvent.click(screen.getByLabelText('검색어 지우기'));
    if (change === 'scope') {
      fireEvent.click(screen.getByText('전체 문서'));
      fireEvent.click(screen.getByLabelText('선택 문서'));
    }
    if (change === 'tab') {
      fireEvent.click(screen.getByRole('tab', { name: '기존 검색' }));
      ai();
    }
    await act(async () => resolve(Answer));
    expect(screen.queryByText('합성 답변 [1]')).not.toBeInTheDocument();
    expect(MockAnswer).toHaveBeenCalledTimes(1);
    expect(MockSearch).not.toHaveBeenCalled();
  },
);

it('allows an explicit repeat of the same question and clears a completed answer on edits', async () => {
  await start();
  ai();
  question();
  submit();
  expect(await screen.findByText('합성 답변 [1]')).toBeInTheDocument();
  submit();
  await waitFor(() => expect(MockAnswer).toHaveBeenCalledTimes(2));
  expect(await screen.findByText('합성 답변 [1]')).toBeInTheDocument();
  question('다른 질문');
  expect(screen.queryByText('합성 답변 [1]')).not.toBeInTheDocument();
  question();
  expect(screen.queryByText('합성 답변 [1]')).not.toBeInTheDocument();
  expect(MockAnswer).toHaveBeenCalledTimes(2);
});

it.each(['tab', 'query', 'scope'] as const)(
  'keeps an unresolved request guarded after restoring its %s',
  async (change) => {
    let resolve: (value: unknown) => void = () => {};
    MockAnswer.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    await start();
    ai();
    question();
    submit();
    await waitFor(() => expect(MockAnswer).toHaveBeenCalledTimes(1));
    if (change === 'tab') {
      fireEvent.click(screen.getByRole('tab', { name: '기존 검색' }));
      ai();
    }
    if (change === 'query') {
      question('다른 질문');
      question();
    }
    if (change === 'scope') {
      fireEvent.click(screen.getByText('전체 문서'));
      fireEvent.click(screen.getByLabelText('선택 문서'));
      fireEvent.click(screen.getByLabelText('전체 문서'));
    }
    expect(screen.getByRole('button', { name: '질문하기' })).toBeDisabled();
    fireEvent.submit(
      screen.getByRole('button', { name: '질문하기' }).closest('form')!,
    );
    expect(MockAnswer).toHaveBeenCalledTimes(1);
    await act(async () => resolve(Answer));
    expect(screen.queryByText('합성 답변 [1]')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: '질문하기' }),
      ).not.toBeDisabled(),
    );
    submit();
    expect(await screen.findByText('합성 답변 [1]')).toBeInTheDocument();
    expect(MockAnswer).toHaveBeenCalledTimes(2);
    expect(MockAnswer).toHaveBeenNthCalledWith(1, {
      question: '시험 질문',
      projectId: 'project-1',
      scope: { mode: 'all' },
      neighborWindow: 5,
    });
    expect(MockAnswer).toHaveBeenNthCalledWith(2, {
      question: '시험 질문',
      projectId: 'project-1',
      scope: { mode: 'all' },
      neighborWindow: 5,
    });
  },
);

it('retains query input focus while invalidating the previous answer', async () => {
  await start();
  ai();
  question();
  submit();
  expect(await screen.findByText('합성 답변 [1]')).toBeInTheDocument();
  const input = screen.getByLabelText('문서 검색 질문');
  input.focus();
  question('다른 질문');
  expect(screen.getByLabelText('문서 검색 질문')).toBe(input);
  expect(input).toHaveFocus();
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  expect(screen.queryByText('합성 답변 [1]')).not.toBeInTheDocument();
});
