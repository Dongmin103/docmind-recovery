import { fireEvent, render, screen } from '@testing-library/react';

import type { DocMindHierarchyNode } from '@/services/docmind-service';
import { SourceTree } from './source-tree';

const Nodes: DocMindHierarchyNode[] = [
  {
    file_id: 'root',
    name: '품질 문서',
    relative_path: '품질 문서',
    depth: 0,
    type: 'folder',
    document_exists: false,
  },
  {
    file_id: 'indexed-file',
    parent_file_id: 'root',
    name: 'SOP.pdf',
    relative_path: '품질 문서/SOP.pdf',
    depth: 1,
    type: 'file',
    document_id: 'document-1',
    document_exists: true,
    index_state: 'INDEXED',
  },
  {
    file_id: 'nested',
    parent_file_id: 'root',
    name: '검증',
    relative_path: '품질 문서/검증',
    depth: 1,
    type: 'folder',
    document_exists: false,
  },
  {
    file_id: 'pending-file',
    parent_file_id: 'nested',
    name: 'Validation.pdf',
    relative_path: '품질 문서/검증/Validation.pdf',
    depth: 2,
    type: 'file',
    document_exists: false,
    index_state: 'PENDING',
  },
];

describe('SourceTree', () => {
  it.each([
    ['PROCESSING', '인덱싱 처리 중'],
    ['FAILED', '인덱싱 실패'],
    ['CLEANUP', '임시 파일 정리 중'],
    ['CLEANUP_FAILED', '임시 파일 정리 실패'],
    ['RETRY_WAIT', '인덱싱 재시도 대기'],
    ['ACTION_REQUIRED', '인덱싱 확인 필요'],
  ] as const)('renders %s without calling it pending', (state, label) => {
    render(<SourceTree nodes={[{ ...Nodes[1], index_state: state }]} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText('인덱싱 대기')).not.toBeInTheDocument();
  });

  it('shows a failed job with pending cleanup separately from searchable old content', () => {
    render(
      <SourceTree
        nodes={[
          {
            ...Nodes[1],
            index_state: 'FAILED',
            index_cleanup_state: 'PENDING',
            index_error_code: 'HOST_WORKER_ERROR',
            searchable: true,
          },
        ]}
      />,
    );
    expect(
      screen.getByText(/인덱싱 실패 · 임시 파일 정리 대기/),
    ).toHaveTextContent('이전 색인 검색 가능 (HOST_WORKER_ERROR)');
  });
  it('labels a searchable partial PPTX and unrecognized image text separately', () => {
    render(<SourceTree nodes={[{
      ...Nodes[1],
      index_partial_coverage: true,
      index_image_ocr_not_run: true,
      searchable: true,
    }]} />);
    expect(screen.getByText(/인덱싱 완료 · 일부 내용 미추출/)).toBeInTheDocument();
    expect(screen.getByText(/이미지 속 글자 미인식/)).toBeInTheDocument();
  });
  it('keeps active-index coverage visible while a newer attempt fails', () => {
    render(<SourceTree nodes={[{
      ...Nodes[1],
      index_state: 'FAILED',
      index_partial_coverage: true,
      index_image_ocr_not_run: true,
      searchable: true,
    }]} />);
    expect(screen.getByText(/이전 색인 검색 가능/)).toHaveTextContent('일부 내용 미추출');
    expect(screen.getByText(/이미지 속 글자 미인식/)).toBeInTheDocument();
  });
  it('does not imply partial indexed coverage without an active index', () => {
    render(<SourceTree nodes={[{
      ...Nodes[1],
      index_state: 'FAILED',
      index_partial_coverage: true,
      index_image_ocr_not_run: true,
      searchable: false,
    }]} />);
    expect(screen.queryByText(/일부 내용 미추출/)).not.toBeInTheDocument();
    expect(screen.queryByText(/이미지 속 글자 미인식/)).not.toBeInTheDocument();
  });
  it('distinguishes a registered paused source from an active empty folder', () => {
    render(<SourceTree nodes={[{ ...Nodes[0], source_enabled: false }]} />);
    expect(screen.getByText('동기화 중지')).toBeInTheDocument();
    expect(screen.getByText('파일 0개 · 하위 폴더 0개')).toBeInTheDocument();
  });

  it('keeps nested folders collapsed and exposes document actions after expansion', () => {
    render(
      <SourceTree
        nodes={Nodes}
        renderDocumentAction={(node) => (
          <button type="button">{node.name} 열기</button>
        )}
      />,
    );

    const rootSummary = screen.getByText('품질 문서').closest('summary');
    const rootDetails = rootSummary?.closest('details');
    const nestedDetails = screen.getByText('검증').closest('details');

    expect(rootDetails).not.toHaveAttribute('open');
    expect(nestedDetails).not.toHaveAttribute('open');
    expect(screen.getByText('파일 1개 · 하위 폴더 1개')).toBeInTheDocument();
    expect(screen.getByText('인덱싱 완료')).toBeInTheDocument();
    expect(screen.getByText('인덱싱 대기')).toBeInTheDocument();
    const documentAction = screen.getByRole('button', {
      name: 'SOP.pdf 열기',
    });
    expect(documentAction).not.toBeVisible();
    expect(
      screen.queryByRole('button', { name: 'Validation.pdf 열기' }),
    ).not.toBeInTheDocument();

    fireEvent.click(rootSummary!);

    expect(rootDetails).toHaveAttribute('open');
    expect(nestedDetails).not.toHaveAttribute('open');
    expect(documentAction).toBeVisible();
    expect(screen.getByText('Validation.pdf')).not.toBeVisible();
  });

  it('preserves an opened folder when an earlier sibling is inserted', () => {
    const { rerender } = render(<SourceTree nodes={Nodes} />);
    const rootSummary = screen.getByText('품질 문서').closest('summary');

    fireEvent.click(rootSummary!);
    expect(rootSummary?.closest('details')).toHaveAttribute('open');

    const earlierSibling: DocMindHierarchyNode = {
      file_id: 'earlier-sibling',
      name: '새 문서.pdf',
      relative_path: '새 문서.pdf',
      depth: 0,
      type: 'file',
      document_exists: false,
      index_state: 'PENDING',
    };
    rerender(<SourceTree nodes={[earlierSibling, ...Nodes]} />);

    expect(screen.getByText('품질 문서').closest('details')).toHaveAttribute(
      'open',
    );
  });

  it('does not drop nodes whose parent is missing', () => {
    const orphan: DocMindHierarchyNode = {
      file_id: 'orphan',
      parent_file_id: 'missing-folder',
      name: '고아 문서.pdf',
      relative_path: '고아 문서.pdf',
      depth: 4,
      type: 'file',
      document_exists: false,
      index_state: 'PENDING',
    };

    render(<SourceTree nodes={[...Nodes, orphan]} />);

    expect(screen.getByText('고아 문서.pdf')).toBeInTheDocument();
  });

  it('uses native keyboard-focusable summary semantics', () => {
    render(<SourceTree nodes={Nodes} />);

    const summary = screen.getByText('품질 문서').closest('summary');
    expect(summary).toBeInstanceOf(HTMLElement);

    summary?.focus();

    expect(summary).toHaveFocus();
    expect(summary?.parentElement?.tagName).toBe('DETAILS');
  });

  it('shows a concise empty state', () => {
    render(<SourceTree nodes={[]} />);

    expect(screen.getByText('문서가 없습니다.')).toBeInTheDocument();
  });
});
