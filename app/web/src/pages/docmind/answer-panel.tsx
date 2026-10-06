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
    selectionId: number;
  }>();
  const panelRef = useRef<HTMLElement>(null);
  const answerStartRef = useRef<HTMLDivElement>(null);
  const followAnswer = useRef(false);
  const revealedTop = useRef(0);
  const restoreCitation = useRef<HTMLButtonElement | null>(null);
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
    followAnswer.current = false;
    resetGeneration();
  }, [requestKey, invalidationKey, resetGeneration]);
  useLayoutEffect(() => {
    const viewport = panelRef.current?.closest<HTMLElement>(
      '[data-docmind-search-scroll]',
    );
    if (!viewport) return;
    const stopFollowing = () => {
      followAnswer.current = false;
    };
    const handleScroll = () => {
      if (Math.abs(viewport.scrollTop - revealedTop.current) > 2)
        stopFollowing();
    };
    const handleKey = (event: KeyboardEvent) => {
      if (
        [
          'ArrowUp',
          'ArrowDown',
          'PageUp',
          'PageDown',
          'Home',
          'End',
          ' ',
        ].includes(event.key)
      )
        stopFollowing();
    };
    const handleSelection = () => {
      if (window.getSelection()?.isCollapsed === false) stopFollowing();
    };
    viewport.addEventListener('wheel', stopFollowing, { passive: true });
    viewport.addEventListener('touchstart', stopFollowing, { passive: true });
    viewport.addEventListener('pointerdown', stopFollowing);
    viewport.addEventListener('scroll', handleScroll, { passive: true });
    viewport.addEventListener('keydown', handleKey);
    document.addEventListener('selectionchange', handleSelection);
    return () => {
      viewport.removeEventListener('wheel', stopFollowing);
      viewport.removeEventListener('touchstart', stopFollowing);
      viewport.removeEventListener('pointerdown', stopFollowing);
      viewport.removeEventListener('scroll', handleScroll);
      viewport.removeEventListener('keydown', handleKey);
      document.removeEventListener('selectionchange', handleSelection);
    };
  }, []);
  useLayoutEffect(() => {
    if (
      !submittedQuestion ||
      !followAnswer.current ||
      window.getSelection()?.isCollapsed === false
    )
      return;
    const viewport = panelRef.current?.closest<HTMLElement>(
      '[data-docmind-search-scroll]',
    );
    const start = answerStartRef.current;
    if (!viewport || !start) return;
    const top = Math.max(
      0,
      viewport.scrollTop +
        start.getBoundingClientRect().top -
        viewport.getBoundingClientRect().top -
        16,
    );
    viewport.scrollTo({ top, behavior: 'instant' });
    // Read back the clamped position when the pending turn is shorter than the viewport.
    revealedTop.current = viewport.scrollTop;
  }, [submittedQuestion, isPending, generation.data, generation.isError]);
  const handleGenerate = (event: FormEvent) => {
    event.preventDefault();
    if (disabled || !request.question.trim() || submitting.current) return;
    // oxlint-disable-next-line react/immutability -- The shared ref synchronously guards remounts before React updates pending state.
    submitting.current = true;
    setLocalPending(true);
    onPendingChange?.(true);
    followAnswer.current = true;
    setSelectedEvidence(undefined);
    setSubmitted({ key: displayKey, question: request.question });
    generation.mutate({ ...request, neighborWindow: 5 });
  };
  const selectCitation = useCallback(
    (number: number, trigger: HTMLButtonElement) => {
      setSelectedEvidence((current) => ({
        number,
        trigger,
        selectionId: (current?.selectionId ?? 0) + 1,
      }));
    },
    [],
  );
  const selectSource = (event: React.MouseEvent<HTMLButtonElement>) =>
    selectCitation(
      Number(event.currentTarget.dataset.citation),
      event.currentTarget,
    );
  const closeEvidence = () => {
    restoreCitation.current = selectedEvidence?.trigger ?? null;
    setSelectedEvidence((current) =>
      current ? { ...current, number: undefined } : undefined,
    );
  };
  useLayoutEffect(() => {
    const trigger = restoreCitation.current;
    if (!trigger?.isConnected) return;
    restoreCitation.current = null;
    trigger.focus({ preventScroll: true });
    const viewport = panelRef.current?.closest<HTMLElement>(
      '[data-docmind-search-scroll]',
    );
    if (!viewport) return;
    const bounds = trigger.getBoundingClientRect();
    const visible = viewport.getBoundingClientRect();
    const delta =
      bounds.top < visible.top + 16
        ? bounds.top - visible.top - 16
        : bounds.bottom > visible.bottom - 16
          ? bounds.bottom - visible.bottom + 16
          : 0;
    if (delta)
      viewport.scrollTo({
        top: Math.max(0, viewport.scrollTop + delta),
        behavior: 'instant',
      });
  }, [selectedEvidence]);
  const citation =
    !isPending && !generation.isError
      ? generation.data?.citations.find(
          (item) => item.number === selectedEvidence?.number,
        )
      : undefined;

  return (
    <section
      ref={panelRef}
      aria-label="문서 근거 답변"
      className="docmind-answer-panel mt-5 min-w-0 text-text-primary"
    >
      <div className="min-w-0 space-y-5 pb-5">
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
        <div
          ref={answerStartRef}
          aria-label="답변 흐름"
          className={`docmind-answer-flow${citation ? ' docmind-answer-flow-with-evidence' : ''}`}
        >
          <div className="min-w-0">
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
                className="min-w-0 rounded-2xl border border-border-button bg-bg-component p-5 sm:p-6"
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
                <details className="mt-4 text-xs text-text-secondary">
                  <summary className="cursor-pointer">검색 문맥 상세</summary>
                  <p className="mt-2">
                    검색 근거 {generation.data.context.seed_count}개 · 문맥 확장{' '}
                    {generation.data.context.expanded_count}개 · 실제 입력{' '}
                    {generation.data.context.used_count}개
                  </p>
                </details>
                {generation.data.context.budget_pruned && (
                  <p className="mt-2 text-xs text-text-secondary">
                    입력 한도에 맞춰 주변 청크 일부 제외
                  </p>
                )}
                {Boolean(generation.data.context.seed_only_count) && (
                  <p className="mt-2 text-xs text-text-secondary">
                    순서/버전 정보가 없는 근거는 확장하지 않음
                  </p>
                )}
                <p className="mt-2 text-sm text-text-secondary">
                  생성된 답변은 틀릴 수 있습니다. 인용 근거를 확인하세요.
                </p>
                {generation.data.citations.map((citation) => (
                  <button
                    type="button"
                    key={citation.number}
                    data-citation={citation.number}
                    onClick={selectSource}
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
          <AnswerEvidence
            citation={citation}
            selectionId={selectedEvidence?.selectionId}
            datasetId={generation.data?.dataset_id}
            onClose={closeEvidence}
            renderInspection={renderInspection}
          />
        </div>
      </div>
      <form
        id="docmind-question"
        onSubmit={handleGenerate}
        aria-label="AI 질문 작성"
        className="min-w-0 rounded-xl border border-border-button bg-bg-component p-3 shadow-sm"
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
          선택한 범위 · 앞뒤 5개 청크
        </p>
        <details className="mt-1 text-xs leading-5 text-text-secondary">
          <summary className="cursor-pointer">답변 생성 안내</summary>
          <p>
            선택한 범위와 앞뒤 5개 청크를 확인하여 답변합니다. 문서 안의
            지시문은 답변 지침으로 사용하지 않습니다.
          </p>
        </details>
      </form>
    </section>
  );
}
