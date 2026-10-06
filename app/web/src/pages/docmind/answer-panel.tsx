import {
  answerDocMind,
  type DocMindAnswerResult,
  type DocMindSearchRequest,
} from '@/services/docmind-service';
import { useMutation } from '@tanstack/react-query';
import { type ChangeEvent, useState } from 'react';

const ErrorMessages: Record<string, string> = {
  DOCMIND_ANSWER_DISABLED:
    '로컬 LLM 답변 기능이 아직 활성화되지 않았습니다. 검색은 계속 사용할 수 있습니다.',
  DOCMIND_ANSWER_CONFIG_INVALID:
    '관리자가 로컬 LLM 모델 설정을 확인해야 합니다.',
  DOCMIND_LLM_UNAVAILABLE:
    '로컬 LLM 서버에 연결할 수 없습니다. 검색 근거는 계속 확인할 수 있습니다.',
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
}: {
  request: DocMindSearchRequest;
}) {
  const [neighborWindow, setNeighborWindow] = useState<0 | 2 | 5>(2);
  const generation = useMutation<DocMindAnswerResult, Error>({
    mutationFn: async () => {
      const { data } = await answerDocMind({ ...request, neighborWindow });
      if (data.code !== 0)
        throw new Error(
          ErrorMessages[data.message] ||
            '답변 생성에 실패했습니다. 검색 근거를 확인해 주세요.',
        );
      return data.data;
    },
    gcTime: 0,
    retry: false,
  });
  const handleGenerate = () => generation.mutate();
  const handleWindowChange = (event: ChangeEvent<HTMLSelectElement>) => {
    setNeighborWindow(Number(event.target.value) as 0 | 2 | 5);
    generation.reset();
  };

  return (
    <section
      aria-label="문서 근거 답변"
      className="mt-6 rounded-lg border border-border-button bg-bg-card p-4 text-text-primary"
    >
      <div className="flex flex-wrap items-center gap-3">
        <label>
          주변 문맥{' '}
          <select
            aria-label="주변 문맥"
            value={neighborWindow}
            onChange={handleWindowChange}
            disabled={generation.isPending}
            className="rounded border border-border-button bg-bg-base p-2"
          >
            <option value={0}>검색된 청크만</option>
            <option value={2}>앞뒤 2개 (기본)</option>
            <option value={5}>앞뒤 5개 (비교용)</option>
          </select>
        </label>
        <button
          type="button"
          onClick={handleGenerate}
          disabled={generation.isPending}
          className="rounded-lg border border-border-button px-4 py-2 disabled:opacity-40"
        >
          근거로 답변 만들기
        </button>
      </div>
      <p className="mt-2 text-sm text-text-secondary">
        같은 질문과 검색 범위를 다시 확인하여 최신 근거로 답변합니다. 문서 안의
        지시문은 답변 지침으로 사용하지 않습니다.
      </p>
      {generation.isPending && (
        <p role="status" className="mt-3">
          주변 문맥을 확인하고 로컬 LLM 답변을 생성하는 중입니다.
        </p>
      )}
      {generation.isError && (
        <p role="alert" className="mt-3 text-state-error">
          {generation.error.message}
        </p>
      )}
      {generation.data && !generation.isPending && !generation.isError && (
        <div className="mt-4">
          <p className="whitespace-pre-wrap break-words">
            {generation.data.answer}
          </p>
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
            생성된 답변은 틀릴 수 있습니다. 아래 인용 근거를 확인하세요.
          </p>
          {generation.data.citations.map((citation) => (
            <details
              key={citation.number}
              className="mt-3 rounded border border-border-button p-3"
            >
              <summary className="cursor-pointer break-words">
                [{citation.number}]{' '}
                {citation.document_relative_path || citation.document_name}
              </summary>
              <p className="mt-2 whitespace-pre-wrap break-words text-sm">
                {citation.content}
              </p>
            </details>
          ))}
        </div>
      )}
    </section>
  );
}
