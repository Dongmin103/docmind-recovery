import { useQuery } from '@tanstack/react-query';
import {
  getDocMindHierarchy,
  type DocMindHierarchy,
} from '@/services/docmind-service';
import { SourceIndexStatus } from './source-index-status';

export const SourceHierarchyKeys = {
  hierarchy: () => ['docmind-hierarchy'] as const,
};

export function SourceDocumentStatus({ documentId }: { documentId: string }) {
  const status = useQuery<DocMindHierarchy, Error>({
    queryKey: SourceHierarchyKeys.hierarchy(),
    queryFn: async () => {
      const { data } = await getDocMindHierarchy();
      if (data.code !== 0) throw new Error('문서 상태 조회 실패');
      return data.data as DocMindHierarchy;
    },
    refetchInterval: 10_000,
  });
  const node = status.data?.nodes.find(
    (item) => item.document_id === documentId,
  );
  return (
    <p role="status" className="shrink-0 px-4 py-2 text-sm text-text-secondary">
      {status.isError ? (
        '문서 상태를 확인하지 못했습니다.'
      ) : node ? (
        <SourceIndexStatus node={node} />
      ) : status.isPending ? (
        '문서 상태 확인 중'
      ) : (
        '현재 원본 문서 목록에서 확인되지 않는 문서입니다.'
      )}
    </p>
  );
}
