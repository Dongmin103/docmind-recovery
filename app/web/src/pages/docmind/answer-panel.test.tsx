import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { answerDocMind } from '@/services/docmind-service';
import AnswerPanel from './answer-panel';

jest.mock('@/services/docmind-service', () => ({ answerDocMind: jest.fn() }));
jest.mock('@/services/knowledge-service', () => ({
  __esModule: true,
  default: { getChunk: jest.fn(() => new Promise(() => {})) },
}));
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

it('renders insufficient evidence as a normal answer without citation controls', async () => {
  MockAnswer.mockResolvedValue({
    data: {
      code: 0,
      data: {
        answer_status: 'insufficient',
        answer:
          '선택한 문서에서 질문의 전제나 답을 뒷받침할 근거를 찾지 못했습니다.',
        dataset_id: 'synthetic-kb',
        citations: [],
        context: {
          seed_count: 1,
          expanded_count: 5,
          used_count: 5,
          neighbor_window: 5,
        },
      },
    },
  });
  renderPanel();
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  const answer = await screen.findByLabelText('AI 답변');
  expect(answer).toHaveTextContent(
    '선택한 문서에서 질문의 전제나 답을 뒷받침할 근거를 찾지 못했습니다.',
  );
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(within(answer).queryByRole('button')).not.toBeInTheDocument();
  expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
});

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

it('retains citation controls for a partial answer with an unknown field', async () => {
  MockAnswer.mockResolvedValue({
    data: {
      code: 0,
      data: {
        ...RichAnswer.data.data,
        answer_status: 'partial',
        answer: '담당자는 민수입니다 [1]. 시행일은 확인할 수 없습니다.',
      },
    },
  });
  renderPanel();
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  expect(await screen.findByLabelText('AI 답변')).toHaveTextContent(
    '시행일은 확인할 수 없습니다.',
  );
  fireEvent.click(screen.getByRole('button', { name: '근거 1 확인' }));
  expect(
    screen.getByRole('complementary', { name: 'synthetic.pdf' }),
  ).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

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
  const evidence = screen.getByRole('complementary', { name: 'synthetic.pdf' });
  expect(evidence).toHaveTextContent('fixtures/synthetic.pdf');
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
  expect(screen.getAllByText('unsafe link')).toHaveLength(1);
  const source = screen
    .getByRole('complementary')
    .querySelector('.chunk-content');
  expect(source?.textContent).toBe(unsafe);
  expect(
    source?.querySelector(
      'script, iframe, style, img, a, [onclick], [onerror], [style], .injected',
    ),
  ).toBeNull();
});

it.each(['close', 'escape'])(
  'restores citation focus without scrolling after evidence %s',
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
    expect(reference).toHaveFocus();
    const focus = jest.spyOn(reference, 'focus');
    if (action === 'close') fireEvent.click(close);
    else fireEvent.keyDown(close, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByRole('complementary')).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(reference).toHaveFocus());
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
  },
);

it('keeps answer citations and composer interactive while switching inline evidence', async () => {
  MockAnswer.mockResolvedValue({
    data: {
      code: 0,
      data: {
        ...RichAnswer.data.data,
        answer: '첫 근거 [1], 두 번째 근거 [2]',
        citations: [
          ...RichAnswer.data.data.citations,
          {
            ...RichAnswer.data.data.citations[0],
            number: 2,
            document_name: 'second.pdf',
            content: '두 번째 합성 근거',
          },
        ],
      },
    },
  });
  const view = renderPanel();
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  const reference = await screen.findByRole('button', { name: '근거 1 확인' });
  fireEvent.click(reference);
  const evidence = screen.getByRole('complementary', { name: 'synthetic.pdf' });
  expect(view.container).toContainElement(evidence);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByLabelText('AI 답변')).not.toHaveAttribute('aria-hidden');
  expect(screen.getByRole('button', { name: '질문하기' })).not.toBeDisabled();
  expect(reference).not.toHaveAttribute('aria-haspopup', 'dialog');
  fireEvent.click(screen.getByRole('button', { name: '근거 2 확인' }));
  expect(
    screen.getByRole('complementary', { name: 'second.pdf' }),
  ).toHaveTextContent('두 번째 합성 근거');
  fireEvent.keyDown(screen.getByRole('button', { name: '질문하기' }), {
    key: 'Escape',
  });
  expect(screen.getByRole('complementary')).toBeInTheDocument();
  fireEvent.click(
    within(screen.getByRole('complementary')).getByRole('button', {
      name: '근거 닫기',
    }),
  );
  expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
});

it('keeps technical context inside details while preserving the answer notice', async () => {
  MockAnswer.mockResolvedValue(RichAnswer);
  renderPanel();
  const instructions = screen.getByText('답변 생성 안내').closest('details');
  expect(instructions).not.toHaveAttribute('open');
  expect(screen.queryByText('선택한 범위 · 앞뒤 5개 청크')).not.toBeInTheDocument();
  expect(instructions).toHaveTextContent('앞뒤 5개 청크');
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  await screen.findByLabelText('AI 답변');
  expect(
    screen.getByText('검색 문맥 상세').closest('details'),
  ).not.toHaveAttribute('open');
  expect(screen.getByText(/생성된 답변은 틀릴 수/)).toBeVisible();
});

it.each(['none', 'wheel', 'scroll', 'selection'])(
  'reveals the answer start on submit and respects reader movement %s before response',
  async (movement) => {
    let resolveAnswer: (value: unknown) => void = () => {};
    MockAnswer.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveAnswer = resolve;
        }),
    );
    const view = render(
      <QueryClientProvider client={new QueryClient()}>
        <div data-docmind-search-scroll>
          <AnswerPanel request={Request} />
        </div>
      </QueryClientProvider>,
    );
    const viewport = view.container.querySelector(
      '[data-docmind-search-scroll]',
    ) as HTMLElement;
    const scroll = jest.fn(({ top }) => {
      viewport.scrollTop = top;
    });
    viewport.scrollTo = scroll;
    jest
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(function (this: HTMLElement) {
        return {
          top: this.getAttribute('aria-label') === '답변 흐름' ? 180 : 0,
        } as DOMRect;
      });
    fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
    await screen.findByRole('status');
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(scroll).toHaveBeenLastCalledWith({ top: 164, behavior: 'instant' });
    if (movement === 'wheel') fireEvent.wheel(viewport);
    if (movement === 'scroll') {
      viewport.scrollTop = 20;
      fireEvent.scroll(viewport);
    }
    let selection: jest.SpyInstance | undefined;
    if (movement === 'selection') {
      selection = jest
        .spyOn(window, 'getSelection')
        .mockReturnValue({ isCollapsed: false } as Selection);
      fireEvent(document, new Event('selectionchange'));
    }
    await act(async () => {
      resolveAnswer(RichAnswer);
    });
    await screen.findByLabelText('AI 답변');
    expect(scroll).toHaveBeenCalledTimes(movement === 'none' ? 2 : 1);
    expect(screen.getByLabelText('내 질문')).toHaveTextContent('절차는?');
    expect(
      screen
        .getByLabelText('답변 흐름')
        .compareDocumentPosition(
          screen.getByRole('form', { name: 'AI 질문 작성' }),
        ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    selection?.mockRestore();
    jest.restoreAllMocks();
  },
);

it('reveals offscreen evidence on explicit open and switch, then returns to the deep citation after close reflow', async () => {
  MockAnswer.mockResolvedValue({
    data: {
      code: 0,
      data: {
        ...RichAnswer.data.data,
        answer: '첫 근거 [1], 깊은 근거 [2]',
        citations: [
          ...RichAnswer.data.data.citations,
          {
            ...RichAnswer.data.data.citations[0],
            number: 2,
            document_name: 'second.pdf',
          },
        ],
      },
    },
  });
  const view = render(
    <QueryClientProvider client={new QueryClient()}>
      <div data-docmind-search-scroll>
        <AnswerPanel request={Request} />
      </div>
    </QueryClientProvider>,
  );
  const viewport = view.container.querySelector(
    '[data-docmind-search-scroll]',
  ) as HTMLElement;
  const scroll = jest.fn(({ top }) => {
    viewport.scrollTop = top;
  });
  viewport.scrollTo = scroll;
  let evidenceY = 900;
  jest
    .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
    .mockImplementation(function (this: HTMLElement) {
      if (this === viewport) return { top: 0, bottom: 600 } as DOMRect;
      const y =
        this.tagName === 'ASIDE' ? evidenceY : this.dataset.citation ? 700 : 0;
      return {
        top: y - viewport.scrollTop,
        bottom: y - viewport.scrollTop + 24,
      } as DOMRect;
    });
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  const first = await screen.findByRole('button', { name: '근거 1 확인' });
  scroll.mockClear();
  fireEvent.click(first);
  expect(scroll).toHaveBeenLastCalledWith({ top: 884, behavior: 'instant' });
  const second = screen.getByRole('button', { name: '근거 2 확인' });
  evidenceY = 1500;
  fireEvent.click(second);
  expect(scroll).toHaveBeenLastCalledWith({ top: 1484, behavior: 'instant' });
  fireEvent.click(screen.getByRole('button', { name: '근거 닫기' }));
  expect(scroll).toHaveBeenLastCalledWith({ top: 684, behavior: 'instant' });
  expect(second).toHaveFocus();
  jest.restoreAllMocks();
});

it('keeps viewport position for visible evidence and resets the evidence reading position when switching', async () => {
  MockAnswer.mockResolvedValue({
    data: {
      code: 0,
      data: {
        ...RichAnswer.data.data,
        answer: '[1], [2]',
        citations: [
          ...RichAnswer.data.data.citations,
          {
            ...RichAnswer.data.data.citations[0],
            number: 2,
            document_name: 'second.pdf',
          },
        ],
      },
    },
  });
  const view = render(
    <QueryClientProvider client={new QueryClient()}>
      <div data-docmind-search-scroll>
        <AnswerPanel request={Request} />
      </div>
    </QueryClientProvider>,
  );
  const viewport = view.container.querySelector(
    '[data-docmind-search-scroll]',
  ) as HTMLElement;
  const scroll = jest.fn(({ top }) => {
    viewport.scrollTop = top;
  });
  viewport.scrollTo = scroll;
  jest
    .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
    .mockImplementation(function (this: HTMLElement) {
      return this === viewport
        ? ({ top: 0, bottom: 600 } as DOMRect)
        : ({ top: 100, bottom: 124 } as DOMRect);
    });
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  const first = await screen.findByRole('button', { name: '근거 1 확인' });
  scroll.mockClear();
  fireEvent.click(first);
  const evidence = screen.getByRole('complementary');
  evidence.scrollTop = 300;
  const second = screen.getByRole('button', { name: '근거 2 확인' });
  fireEvent.click(second);
  expect(evidence.scrollTop).toBe(0);
  expect(scroll).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '근거 닫기' }));
  expect(scroll).not.toHaveBeenCalled();
  expect(second).toHaveFocus();
  jest.restoreAllMocks();
});

it('reveals the same evidence again from the same or another inline reference after the reader returns to the answer', async () => {
  MockAnswer.mockResolvedValue({
    data: {
      code: 0,
      data: { ...RichAnswer.data.data, answer: '처음 [1], 다시 [1]' },
    },
  });
  const view = render(
    <QueryClientProvider client={new QueryClient()}>
      <div data-docmind-search-scroll>
        <AnswerPanel request={Request} />
      </div>
    </QueryClientProvider>,
  );
  const viewport = view.container.querySelector(
    '[data-docmind-search-scroll]',
  ) as HTMLElement;
  const scroll = jest.fn(({ top }) => {
    viewport.scrollTop = top;
  });
  viewport.scrollTo = scroll;
  jest
    .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
    .mockImplementation(function (this: HTMLElement) {
      if (this === viewport) return { top: 0, bottom: 600 } as DOMRect;
      const y =
        this.tagName === 'ASIDE' ? 900 : this.dataset.citation ? 700 : 0;
      return {
        top: y - viewport.scrollTop,
        bottom: y - viewport.scrollTop + 24,
      } as DOMRect;
    });
  fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
  const [first, second] = await screen.findAllByRole('button', {
    name: '근거 1 확인',
  });
  fireEvent.click(first);
  scroll.mockClear();
  viewport.scrollTop = 0;
  fireEvent.scroll(viewport);
  fireEvent.click(first);
  expect(scroll).toHaveBeenCalledTimes(1);
  expect(scroll).toHaveBeenLastCalledWith({ top: 884, behavior: 'instant' });
  viewport.scrollTop = 0;
  fireEvent.scroll(viewport);
  fireEvent.click(second);
  expect(scroll).toHaveBeenCalledTimes(2);
  expect(scroll).toHaveBeenLastCalledWith({ top: 884, behavior: 'instant' });
  fireEvent.click(screen.getByRole('button', { name: '근거 닫기' }));
  expect(second).toHaveFocus();
  jest.restoreAllMocks();
});

it.each([
  ['DOCMIND_LLM_UNAVAILABLE', '연결'],
  ['DOCMIND_LLM_AUTH_FAILED', '인증'],
  ['DOCMIND_LLM_CREDITS_REQUIRED', '잔액'],
  ['DOCMIND_LLM_RATE_LIMITED', '요청 한도'],
  [
    'DOCMIND_LLM_RESPONSE_INVALID',
    '모델의 응답 형식을 확인할 수 없어 답변을 표시하지 않았습니다.',
  ],
  [
    'DOCMIND_LLM_CITATIONS_INVALID',
    '모델이 올바른 근거 번호를 제시하지 않아 답변을 표시하지 않았습니다.',
  ],
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

it.each([
  [777.55, 793.61],
  [793.16, 801.61],
])(
  'reveals evidence at %s above the actual sticky composer on open and reselection',
  async (evidenceY, initialComposerTop) => {
    MockAnswer.mockResolvedValue(RichAnswer);
    const view = render(
      <QueryClientProvider client={new QueryClient()}>
        <div data-docmind-search-scroll>
          <AnswerPanel request={Request} />
        </div>
      </QueryClientProvider>,
    );
    const viewport = view.container.querySelector(
      '[data-docmind-search-scroll]',
    ) as HTMLElement;
    const scroll = jest.fn(({ top }) => {
      viewport.scrollTop = top;
    });
    viewport.scrollTo = scroll;
    let composerTop = initialComposerTop;
    const rect = jest
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(function (this: HTMLElement) {
        if (this === viewport)
          return { top: 100, bottom: 1080, height: 980 } as DOMRect;
        if (this.hasAttribute('data-docmind-composer'))
          return {
            top: composerTop,
            bottom: 1080,
            height: 1080 - composerTop,
          } as DOMRect;
        const top =
          this.tagName === 'ASIDE' ? evidenceY - viewport.scrollTop : 116;
        return { top, bottom: top + 240, height: 240 } as DOMRect;
      });
    try {
      fireEvent.click(screen.getByRole('button', { name: '질문하기' }));
      const reference = await screen.findByRole('button', {
        name: '근거 1 확인',
      });
      scroll.mockClear();
      fireEvent.click(reference);
      expect(scroll).toHaveBeenCalledTimes(1);
      expect(viewport.scrollTop).toBeCloseTo(evidenceY - 116);
      expect(scroll).toHaveBeenLastCalledWith({
        top: expect.any(Number),
        behavior: 'instant',
      });

      viewport.scrollTop = 0;
      fireEvent.scroll(viewport);
      composerTop = 1080;
      fireEvent.click(reference);
      expect(scroll).toHaveBeenCalledTimes(1);

      composerTop = initialComposerTop;
      fireEvent.click(reference);
      expect(scroll).toHaveBeenCalledTimes(2);
      expect(viewport.scrollTop).toBeCloseTo(evidenceY - 116);
    } finally {
      rect.mockRestore();
    }
  },
);