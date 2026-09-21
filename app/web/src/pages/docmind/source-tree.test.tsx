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
