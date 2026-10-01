import type { DocMindHierarchyNode } from '@/services/docmind-service';

const IndexStateLabels: Record<
  NonNullable<DocMindHierarchyNode['index_state']>,
  string
> = {
  INDEXED: '인덱싱 완료',
  PENDING: '인덱싱 대기',
  PROCESSING: '인덱싱 처리 중',
  FAILED: '인덱싱 실패',
  CLEANUP: '임시 파일 정리 중',
  CLEANUP_FAILED: '임시 파일 정리 실패',
  RETRY_WAIT: '인덱싱 재시도 대기',
  ACTION_REQUIRED: '인덱싱 확인 필요',
};

export function SourceIndexStatus({ node }: { node: DocMindHierarchyNode }) {
  if (!node.index_state) return null;
  const failed = ['FAILED', 'ACTION_REQUIRED', 'CLEANUP_FAILED'].includes(
    node.index_state,
  );
  return (
    <span className={failed ? 'text-state-warning' : 'text-text-secondary'}>
      {IndexStateLabels[node.index_state]}
      {node.searchable && node.index_partial_coverage
        ? ' · 일부 내용 미추출'
        : null}
      {node.searchable && node.index_image_ocr_not_run
        ? ' · 이미지 속 글자 미인식'
        : null}
      {node.index_pdf_ocr_option === 'zero_text'
        ? ' · 검색 가능한 글자 없음'
        : null}
      {failed && node.index_cleanup_state === 'PENDING'
        ? ' · 임시 파일 정리 대기'
        : failed && node.index_cleanup_state === 'FAILED'
          ? ' · 정리 재확인 필요'
          : null}
      {node.searchable && node.index_state !== 'INDEXED'
        ? ' · 이전 색인 검색 가능'
        : null}
      {node.index_error_code ? ` (${node.index_error_code})` : null}
    </span>
  );
}
