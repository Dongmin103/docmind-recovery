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
  expect(await screen.findByText('절차입니다 [1]')).toBeInTheDocument();
  expect(screen.getByText('<script>text</script>')).toBeInTheDocument();
  expect(document.querySelector('script')).toBeNull();
  expect(MockAnswer).toHaveBeenCalledWith({ ...Request, neighborWindow: 5 });
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  expect(screen.getByText(/입력 한도에 맞춰 주변 청크 일부 제외/)).toBeInTheDocument();
});

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
