import {
  answerDocMind,
  type DocMindAnswerRequest,
  type DocMindAnswerResult,
  type DocMindSearchRequest,
} from '@/services/docmind-service';
import { useMutation } from '@tanstack/react-query';
import {
  type FormEvent,
  type MutableRefObject,
  type ReactNode,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import AnswerMarkdown from './answer-markdown';
import AnswerEvidence from './answer-evidence';
import './answer-panel.less';

const ErrorMessages: Record<string, string> = {
  DOCMIND_ANSWER_DISABLED:
    'LLM 답변 기능이 아직 활성화되지 않았습니다. 검색은 계속 사용할 수 있습니다.',
  DOCMIND_ANSWER_CONFIG_INVALID:
    '관리자가 LLM 모델과 연결 설정을 확인해야 합니다.',
  DOCMIND_LLM_UNAVAILABLE:
    'LLM 서비스에 연결할 수 없습니다. 검색 근거는 계속 확인할 수 있습니다.',
  DOCMIND_LLM_AUTH_FAILED:
    '외부 LLM 인증에 실패했습니다. 관리자가 API 키를 확인해야 합니다.',
  DOCMIND_LLM_CREDITS_REQUIRED:
    '외부 LLM의 사용 가능 잔액 또는 키의 지출 한도가 부족합니다. 관리자에게 확인해 주세요.',
  DOCMIND_LLM_RATE_LIMITED:
    '외부 LLM 요청 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.',
  DOCMIND_SOURCE_CHANGED: '문서가 변경되었습니다. 다시 검색해 주세요.',
  DOCMIND_CONTEXT_TOO_LARGE:
    '상위 근거가 모델 입력 한도를 넘었습니다. 질문이나 검색 범위를 좁혀 주세요.',
  DOCMIND_LLM_OUTPUT_TRUNCATED:
    '모델의 답변 길이 한도를 초과했습니다. 질문을 좁혀 주세요.',
  DOCMIND_LLM_CITATIONS_INVALID:
    '모델이 올바른 근거 번호를 제시하지 않아 답변을 표시하지 않았습니다.',
};

export default function AnswerPanel({
  request,
  disabled = false,
  pending = false,
  pendingRef,
  onPendingChange,
  invalidationKey,
  composer,
  renderInspection,
}: {
  request: DocMindSearchRequest;
  disabled?: boolean;
  pending?: boolean;
  pendingRef?: MutableRefObject<boolean>;
  onPendingChange?: (pending: boolean) => void;
  invalidationKey?: string;
  composer?: ReactNode;
  renderInspection?: (datasetId: string, documentId: string) => ReactNode;
}) {
  const localSubmitting = useRef(false);
  const submitting = pendingRef ?? localSubmitting;
  const [localPending, setLocalPending] = useState(false);
  const isPending = pending || localPending;
  const requestKey = JSON.stringify(request);
  const displayKey = JSON.stringify([requestKey, invalidationKey]);
  const [submitted, setSubmitted] = useState({ key: '', question: '' });
  const submittedQuestion =
    submitted.key === displayKey ? submitted.question : '';
  const [selectedEvidence, setSelectedEvidence] = useState<{
    number?: number;
    trigger: HTMLButtonElement;
  }>();
  const generation = useMutation<
    DocMindAnswerResult,
    Error,
    DocMindAnswerRequest
  >({
    mutationFn: async (submittedRequest) => {
      const { data } = await answerDocMind(submittedRequest);
      if (data.code !== 0)
        throw new Error(
          ErrorMessages[data.message] ||
            '답변 생성에 실패했습니다. 검색 근거를 확인해 주세요.',
        );
      return data.data;
    },
    onSettled: () => {
      submitting.current = false;
      setLocalPending(false);
      onPendingChange?.(false);
    },
    gcTime: 0,
    retry: false,
  });
  const resetGeneration = generation.reset;
  useLayoutEffect(() => {
    resetGeneration();
  }, [requestKey, invalidationKey, resetGeneration]);
  const handleGenerate = (event: FormEvent) => {
    event.preventDefault();
    if (disabled || !request.question.trim() || submitting.current) return;
    // oxlint-disable-next-line react/immutability -- The shared ref synchronously guards remounts before React updates pending state.
    submitting.current = true;
    setLocalPending(true);
    onPendingChange?.(true);
    setSelectedEvidence(undefined);
    setSubmitted({ key: displayKey, question: request.question });
    generation.mutate({ ...request, neighborWindow: 5 });
  };
  const selectCitation = useCallback(
    (number: number, trigger: HTMLButtonElement) => {
      setSelectedEvidence({ number, trigger });
    },
    [],
  );
  const selectSource = (event: React.MouseEvent<HTMLButtonElement>) =>
    selectCitation(
      Number(event.currentTarget.dataset.citation),
      event.currentTarget,
    );
  const closeEvidence = () =>
    setSelectedEvidence((current) =>
      current ? { ...current, number: undefined } : undefined,
    );

  return (
    <section
      aria-label="문서 근거 답변"
      className="relative mt-5 flex min-h-[24rem] min-w-0 flex-col text-text-primary"
    >
      <div className="min-w-0 flex-1 space-y-5 pb-8">
        {submittedQuestion && (
          <div
            aria-label="내 질문"
            className="ml-auto max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-tr-sm bg-accent-primary/10 px-5 py-3"
          >
            {submittedQuestion}
          </div>
        )}
        {!submittedQuestion && !isPending && (
          <p className="py-12 text-center text-text-secondary">
            문서에 대해 질문해 보세요. 답변의 인용 번호로 실제 근거를 확인할 수
            있습니다.
          </p>
        )}
        {isPending && (
          <p role="status" className="mt-3">
            주변 문맥을 확인하고 LLM 답변을 생성하는 중입니다.
          </p>
        )}
        {generation.isError && (
          <p role="alert" className="mt-3 text-state-error">
            {generation.error.message}
          </p>
        )}
        {generation.data && !isPending && !generation.isError && (
          <article
            aria-label="AI 답변"
            className="min-w-0 rounded-2xl border border-border-button bg-bg-card p-5 sm:p-6"
          >
            <p className="mb-4 text-sm font-semibold text-text-secondary">
              DocMind
            </p>
            <AnswerMarkdown
              content={generation.data.answer}
              citationNumbers={generation.data.citations.map(
                (citation) => citation.number,
              )}
              onCitation={selectCitation}
            />
            <p className="mt-3 text-sm text-text-secondary">
              검색 근거 {generation.data.context.seed_count}개 · 문맥 확장{' '}
              {generation.data.context.expanded_count}개 · 실제 입력{' '}
              {generation.data.context.used_count}개
              {generation.data.context.budget_pruned &&
                ' · 입력 한도에 맞춰 주변 청크 일부 제외'}
              {Boolean(generation.data.context.seed_only_count) &&
                ' · 순서/버전 정보가 없는 근거는 확장하지 않음'}
            </p>
            <p className="mt-2 text-sm text-text-secondary">
              생성된 답변은 틀릴 수 있습니다. 인용 근거를 확인하세요.
            </p>
            {generation.data.citations.map((citation) => (
              <button
                type="button"
                key={citation.number}
                data-citation={citation.number}
                onClick={selectSource}
                aria-haspopup="dialog"
                className="mr-2 mt-3 max-w-full rounded-lg border border-border-button px-3 py-2 text-left text-sm text-text-secondary hover:text-accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
              >
                <span className="break-words">
                  [{citation.number}] {citation.document_name}
                </span>
              </button>
            ))}
          </article>
        )}
      </div>
      <form
        id="docmind-question"
        onSubmit={handleGenerate}
        aria-label="AI 질문 작성"
        className="sticky bottom-0 min-w-0 rounded-xl border border-border-button bg-bg-card p-3 shadow-sm"
      >
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          {composer}
          <button
            type="submit"
            disabled={disabled || !request.question.trim() || isPending}
            className="ml-auto shrink-0 rounded-lg bg-accent-primary px-4 py-2 text-primary-foreground disabled:opacity-40"
          >
            질문하기
          </button>
        </div>
        <p className="mt-2 text-xs leading-5 text-text-secondary">
          선택한 범위와 앞뒤 5개 청크를 확인하여 답변합니다. 문서 안의 지시문은
          답변 지침으로 사용하지 않습니다.
        </p>
      </form>
      <AnswerEvidence
        citation={
          !isPending && !generation.isError
            ? generation.data?.citations.find(
                (citation) => citation.number === selectedEvidence?.number,
              )
            : undefined
        }
        datasetId={generation.data?.dataset_id}
        onClose={closeEvidence}
        trigger={selectedEvidence?.trigger ?? null}
        renderInspection={renderInspection}
      />
    </section>
  );
}
