import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { answerDocMind } from '@/services/docmind-service';
import AnswerPanel from './answer-panel';

jest.mock('@/services/docmind-service', () => ({ answerDocMind: jest.fn() }));
const Request = { question: '절차는?', scope: { mode: 'all' as const } };
const MockAnswer = answerDocMind as jest.Mock;

function renderPanel() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AnswerPanel request={Request} />
    </QueryClientProvider>,
  );
}

beforeEach(() => jest.clearAllMocks());

it('does not display a pending response after its request panel is replaced', async () => {
  let resolveOld: (value: unknown) => void = () => {};
  MockAnswer.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
  );
  const client = new QueryClient();
  const view = render(
    <QueryClientProvider client={client}>
      <AnswerPanel request={Request} />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  await waitFor(() => expect(MockAnswer).toHaveBeenCalledTimes(1));
  view.rerender(
    <QueryClientProvider client={client}>
      <AnswerPanel
        request={{
          ...Request,
          scope: { mode: 'documents', documentIds: ['new-document'] },
        }}
      />
    </QueryClientProvider>,
  );
  await act(async () => {
    resolveOld({
      data: {
        code: 0,
        data: {
          answer: '오래된 답변 [1]',
          citations: [],
          context: {
            seed_count: 1,
            expanded_count: 1,
            used_count: 1,
            neighbor_window: 2,
          },
        },
      },
    });
  });
  expect(screen.queryByText('오래된 답변 [1]')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '질문하기' })).not.toBeDisabled();
});

it('only generates on request and shows cited evidence safely', async () => {
  MockAnswer.mockResolvedValue({
    data: {
      code: 0,
      data: {
        answer: '절차입니다 [1]',
        context: {
          seed_count: 5,
          expanded_count: 25,
          used_count: 20,
          neighbor_window: 2,
          budget_pruned: true,
        },
        citations: [
          {
            number: 1,
            chunk_id: 'c',
            doc_id: 'd',
            document_name: '합성문서',
            content: '<script>text</script>',
          },
        ],
      },
    },
  });
  renderPanel();
  expect(MockAnswer).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  expect(await screen.findByLabelText('AI 답변')).toHaveTextContent(
    '절차입니다 [1]',
  );
  fireEvent.click(screen.getByRole('button', { name: '근거 1 확인' }));
  expect(document.querySelector('script')).toBeNull();
  expect(MockAnswer).toHaveBeenCalledWith({ ...Request, neighborWindow: 5 });
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  expect(
    screen.getByText(/입력 한도에 맞춰 주변 청크 일부 제외/),
  ).toBeInTheDocument();
});

const RichAnswer = {
  data: {
    code: 0,
    data: {
      answer:
        '## 합성 절차\n\n- 첫 단계 [1]\n- 다음 단계 [9], [01], [[1]]\n\n`코드 [1]`\n\n| 항목 | 값 |\n| --- | --- |\n| 상태 | 준비 |',
      dataset_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      citations: [
        {
          number: 1,
          chunk_id: 'chunk-test',
          doc_id: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          document_name: 'synthetic.pdf',
          document_relative_path: 'fixtures/synthetic.pdf',
          content:
            '<h3>합성 근거</h3><table><tr><th>항목</th><th>내용</th></tr><tr><td>순서</td><td>첫 단계</td></tr></table>',
        },
      ],
      context: {
        seed_count: 1,
        expanded_count: 5,
        used_count: 5,
        neighbor_window: 5,
      },
    },
  },
};

it('renders real Markdown and only connects mapped numeric references to the actual HTML evidence', async () => {
  MockAnswer.mockResolvedValue(RichAnswer);
  renderPanel();
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  expect(
    await screen.findByRole('heading', { name: '합성 절차' }),
  ).toBeInTheDocument();
  expect(screen.getByRole('list')).toHaveTextContent('첫 단계 [1]');
  expect(screen.getByRole('table')).toHaveTextContent('상태준비');
  expect(screen.getAllByRole('button', { name: '근거 1 확인' })).toHaveLength(
    1,
  );
  expect(screen.getByText(/다음 단계/)).toHaveTextContent('[9], [01], [[1]]');
  expect(screen.getByText('코드 [1]').tagName).toBe('CODE');
  fireEvent.click(screen.getByRole('button', { name: '근거 1 확인' }));
  const dialog = screen.getByRole('dialog', { name: 'synthetic.pdf' });
  expect(dialog).toHaveTextContent('fixtures/synthetic.pdf');
  expect(
    screen.getByRole('heading', { name: '합성 근거' }),
  ).toBeInTheDocument();
  expect(document.querySelectorAll('table')).toHaveLength(2);
  expect(
    screen.getByRole('link', { name: '원문 · 청크 확인' }),
  ).toHaveAttribute(
    'href',
    '/docmind?view=document&id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&doc_id=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&source=docmind&return=search',
  );
});

it('sanitizes answer and evidence without allowing automatic remote requests or injected presentation', async () => {
  const unsafe =
    '<script>window.bad=1</script><iframe src="https://invalid.example/frame"></iframe><style>body{display:none}</style><img src="https://invalid.example/image" onerror="bad()"><a href="javascript:bad()">unsafe link</a><p onclick="bad()" style="position:fixed" class="injected">Safe text</p>';
  MockAnswer.mockResolvedValue({
    data: {
      code: 0,
      data: {
        ...RichAnswer.data.data,
        answer: unsafe + '\n\n[1]',
        citations: [{ ...RichAnswer.data.data.citations[0], content: unsafe }],
      },
    },
  });
  const view = renderPanel();
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  expect(await screen.findByLabelText('AI 답변')).toHaveTextContent(
    'Safe text',
  );
  fireEvent.click(screen.getByRole('button', { name: '근거 1 확인' }));
  document
    .querySelectorAll('.docmind-answer-markdown')
    .forEach((body) =>
      expect(
        body.querySelector(
          'script, iframe, style, img, [onclick], [onerror], [style], .injected, a[href^="javascript:"]',
        ),
      ).toBeNull(),
    );
  expect(view.container.querySelector('a[href^="javascript:"]')).toBeNull();
  expect(screen.getAllByText('unsafe link')).toHaveLength(2);
});

it.each(['close', 'escape'])(
  'restores citation focus after %s and keeps keyboard focus inside evidence',
  async (action) => {
    MockAnswer.mockResolvedValue(RichAnswer);
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
    const reference = await screen.findByRole('button', {
      name: '근거 1 확인',
    });
    reference.focus();
    fireEvent.click(reference);
    const close = screen.getByRole('button', { name: '근거 닫기' });
    await waitFor(() => expect(close).toHaveFocus());
    if (action === 'close') fireEvent.click(close);
    else fireEvent.keyDown(close, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(reference).toHaveFocus());
  },
);

it.each([
  ['DOCMIND_LLM_UNAVAILABLE', '연결'],
  ['DOCMIND_LLM_AUTH_FAILED', '인증'],
  ['DOCMIND_LLM_CREDITS_REQUIRED', '잔액'],
  ['DOCMIND_LLM_RATE_LIMITED', '요청 한도'],
])('keeps search usable and explains %s', async (message, explanation) => {
  MockAnswer.mockResolvedValue({
    data: { code: 100, message },
  });
  renderPanel();
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(explanation);
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '질문하기' })).not.toBeDisabled(),
  );
});
