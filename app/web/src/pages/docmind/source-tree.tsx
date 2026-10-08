import type { DocMindHierarchyNode } from '@/services/docmind-service';
import { ChevronRight, FileText, Folder, FolderOpen } from 'lucide-react';
import type { ReactNode } from 'react';
import { SourceIndexStatus } from './source-index-status';
import './source-tree.less';

export interface SourceTreeProps {
  nodes: DocMindHierarchyNode[];
  renderDocumentAction?: (node: DocMindHierarchyNode) => ReactNode;
}

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

  const renderNode = (indexedNode: IndexedNode, depth: number): ReactNode => {
    if (visited.has(indexedNode.index)) return null;
    visited.add(indexedNode.index);

    const { node } = indexedNode;
    if (node.type === 'file') {
      return (
        <li key={node.file_id} className="docmind-source-node">
          <div className="docmind-source-file">
            <FileText className="docmind-source-file-icon" aria-hidden="true" />
            <div className="docmind-source-file-content">
              <div className="docmind-source-document">
                <span
                  className="docmind-source-name"
                  title={node.relative_path}
                >
                  {node.name}
                </span>
                {node.index_state ? (
                  <span
                    className="docmind-source-status"
                    data-state={node.index_state}
                  >
                    <SourceIndexStatus node={node} />
                  </span>
                ) : null}
              </div>
              {node.document_exists && renderDocumentAction ? (
                <div className="docmind-source-document-actions">
                  {renderDocumentAction(node)}
                </div>
              ) : null}
            </div>
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
      <li
        key={node.file_id}
        className="docmind-source-node"
        data-deep={depth >= 3 || undefined}
      >
        <details className="docmind-source-folder" open={depth < 2}>
          <summary title={node.relative_path}>
            <ChevronRight
              className="docmind-source-chevron"
              aria-hidden="true"
            />
            <span className="docmind-source-folder-icon" aria-hidden="true">
              <Folder className="docmind-source-folder-closed" />
              <FolderOpen className="docmind-source-folder-open" />
            </span>
            <span className="docmind-source-folder-info">
              <span className="docmind-source-name">{node.name}</span>
              <span className="docmind-source-folder-meta">
                <span>
                  파일 {fileCount}개 · 하위 폴더 {folderCount}개
                </span>
                {depth >= 4 && <span>{depth + 1}단계</span>}
                {node.source_enabled === false && (
                  <span className="docmind-source-paused">동기화 중지</span>
                )}
              </span>
            </span>
          </summary>
          {children.length > 0 ? (
            <ul
              className="docmind-source-children"
              aria-label={`${node.name} 하위 항목`}
            >
              {children.map((child) => renderNode(child, depth + 1))}
            </ul>
          ) : null}
        </details>
      </li>
    );
  };

  const renderedRoots = roots.map((node) => renderNode(node, 0));
  const renderedOrCycles = indexedNodes.map((node) => renderNode(node, 0));

  return (
    <section aria-label="문서 라이브러리" className="docmind-source-tree">
      <ul className="docmind-source-roots">
        {renderedRoots.concat(renderedOrCycles)}
      </ul>
    </section>
  );
}

export default SourceTree;
