import type { DocMindHierarchyNode } from '@/services/docmind-service';
import type { ReactNode } from 'react';

export interface SourceTreeProps {
  nodes: DocMindHierarchyNode[];
  renderDocumentAction?: (node: DocMindHierarchyNode) => ReactNode;
}

const IndexStateLabels: Record<
  NonNullable<DocMindHierarchyNode['index_state']>,
  string
> = {
  INDEXED: '인덱싱 완료',
  PENDING: '인덱싱 대기',
};

interface IndexedNode {
  index: number;
  node: DocMindHierarchyNode;
}

export function SourceTree({ nodes, renderDocumentAction }: SourceTreeProps) {
  if (nodes.length === 0) {
    return <p className="text-sm text-text-secondary">문서가 없습니다.</p>;
  }

  const indexedNodes = nodes.map((node, index) => ({ index, node }));
  const folderIds = new Set(
    nodes.filter((node) => node.type === 'folder').map((node) => node.file_id),
  );
  const childrenByParent = new Map<string, IndexedNode[]>();

  indexedNodes.forEach((indexedNode) => {
    const parentId = indexedNode.node.parent_file_id;
    if (!parentId || !folderIds.has(parentId)) return;

    const siblings = childrenByParent.get(parentId) ?? [];
    siblings.push(indexedNode);
    childrenByParent.set(parentId, siblings);
  });

  const roots = indexedNodes.filter(({ node }) => {
    const parentId = node.parent_file_id;
    return !parentId || !folderIds.has(parentId) || parentId === node.file_id;
  });
  const visited = new Set<number>();

  const renderNode = (indexedNode: IndexedNode): ReactNode => {
    if (visited.has(indexedNode.index)) return null;
    visited.add(indexedNode.index);

    const { node } = indexedNode;
    if (node.type === 'file') {
      return (
        <li key={node.file_id} className="py-1.5">
          <div className="flex min-w-0 items-center gap-3 text-sm">
            <span className="min-w-0 flex-1 truncate text-text-primary">
              {node.name}
            </span>
            {node.index_state ? (
              <span className="shrink-0 text-xs text-text-secondary">
                {IndexStateLabels[node.index_state]}
              </span>
            ) : null}
            {node.document_exists && renderDocumentAction ? (
              <span className="shrink-0">{renderDocumentAction(node)}</span>
            ) : null}
          </div>
        </li>
      );
    }

    const children = childrenByParent.get(node.file_id) ?? [];
    const fileCount = children.filter(
      (child) => child.node.type === 'file',
    ).length;
    const folderCount = children.length - fileCount;

    return (
      <li key={node.file_id} className="py-1">
        <details className="group rounded-lg border border-border-button bg-bg-card px-3 py-2">
          <summary className="cursor-pointer text-sm text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary">
            <span className="font-medium">{node.name}</span>
            <span className="ml-2 text-xs text-text-secondary">
              파일 {fileCount}개 · 하위 폴더 {folderCount}개
            </span>
          </summary>
          {children.length > 0 ? (
            <ul className="ml-3 mt-2 border-l border-border-button pl-3">
              {children.map(renderNode)}
            </ul>
          ) : null}
        </details>
      </li>
    );
  };

  const renderedRoots = roots.map(renderNode);
  const renderedOrCycles = indexedNodes.map(renderNode);

  return (
    <section aria-label="문서 라이브러리">
      <ul>{renderedRoots.concat(renderedOrCycles)}</ul>
    </section>
  );
}

export default SourceTree;
