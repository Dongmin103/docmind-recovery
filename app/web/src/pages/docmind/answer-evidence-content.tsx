import ChunkContent from '@/components/chunk-content';
import type { IChunk } from '@/interfaces/database/dataset';
import type { DocMindAnswerResult } from '@/services/docmind-service';
import knowledgeService from '@/services/knowledge-service';
import { getSourceLocations } from '@/utils/source-location';
import { useQuery } from '@tanstack/react-query';

type AnswerCitation = DocMindAnswerResult['citations'][number];

const AnswerEvidenceKeys = {
  chunk: (datasetId: string | undefined, citation: AnswerCitation) =>
    [
      'docmind-answer-evidence',
      datasetId,
      citation.doc_id,
      citation.chunk_id,
      citation.chunk_set_id,
      citation.source_version_id,
      citation.content,
    ] as const,
};

function matchesCitation(
  chunk: IChunk | undefined,
  citation: AnswerCitation,
): chunk is IChunk {
  return (
    !!chunk &&
    typeof chunk.chunk_id === 'string' &&
    !!chunk.chunk_id &&
    chunk.chunk_id === citation.chunk_id &&
    typeof chunk.doc_id === 'string' &&
    !!chunk.doc_id &&
    chunk.doc_id === citation.doc_id &&
    typeof chunk.content_with_weight === 'string' &&
    chunk.content_with_weight === citation.content &&
    (citation.chunk_set_id === undefined ||
      chunk.chunk_set_id === citation.chunk_set_id) &&
    (citation.source_version_id === undefined ||
      chunk.source_version_id === undefined ||
      chunk.source_version_id === citation.source_version_id)
  );
}

export default function AnswerEvidenceContent({
  citation,
  datasetId,
}: {
  citation: AnswerCitation;
  datasetId?: string;
}) {
  const chunk = useQuery({
    queryKey: AnswerEvidenceKeys.chunk(datasetId, citation),
    queryFn: async () => {
      const response = await knowledgeService.getChunk({
        kb_id: datasetId,
        doc_id: citation.doc_id,
        chunk_id: citation.chunk_id,
      });
      if (response?.data?.code !== 0)
        throw new Error('Evidence display unavailable');
      const fetched = response.data.data as IChunk | undefined;
      return matchesCitation(fetched, citation) ? fetched : null;
    },
    enabled: !!datasetId && !!citation.doc_id && !!citation.chunk_id,
    retry: false,
    gcTime: 0,
    placeholderData: () => undefined,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const displayHtml = chunk.data?.display_html;
  const hasDisplayHtml =
    typeof displayHtml === 'string' && !!displayHtml.trim();
  const location = chunk.data ? getSourceLocations(chunk.data) : undefined;
  return (
    <>
      {location && location !== '문서 위치' && (
        <p className="mb-2 text-xs text-text-secondary">{location}</p>
      )}
      <ChunkContent
        content={hasDisplayHtml ? displayHtml : citation.content}
        html={hasDisplayHtml}
      />
      {chunk.isFetching && (
        <p role="status" className="mt-2 text-xs text-text-secondary">
          근거 표시 정보를 확인하는 중입니다.
        </p>
      )}
      {chunk.isError && (
        <p className="mt-2 text-xs text-text-secondary">
          표시 정보를 불러올 수 없어 인용 당시 텍스트를 표시합니다.
        </p>
      )}
      {chunk.data === null && (
        <p className="mt-2 text-xs text-text-secondary">
          근거가 변경되었거나 확인할 수 없어 인용 당시 텍스트를 표시합니다.
        </p>
      )}
    </>
  );
}
