import type { DocMindFolder } from '@/services/docmind-service';
import {
  useId,
  useMemo,
  useState,
  type ChangeEvent,
  type ReactNode,
} from 'react';
import './scope-folder-picker.less';

export interface ScopeFolderPickerProps {
  folders: DocMindFolder[];
  selectedIds: string[];
  onToggle: (id: string) => void;
}

interface FolderRowData {
  folder: DocMindFolder;
  ancestors: string[];
  hasChildren: boolean;
}

function folderPath(path?: string) {
  const normalized = path?.trim().replace(/\\/g, '/').replace(/\/$/, '');
  if (
    !normalized ||
    normalized.split('/').some((part) => !part || part === '.' || part === '..')
  )
    return undefined;
  return normalized;
}

function folderRows(folders: DocMindFolder[]): FolderRowData[] {
  const byId = new Map<string, DocMindFolder>();
  for (const folder of folders)
    if (!byId.has(folder.id)) byId.set(folder.id, folder);
  const byPath = new Map<string, string[]>();
  for (const folder of byId.values()) {
    const path = folderPath(folder.relative_path);
    if (path) byPath.set(path, [...(byPath.get(path) ?? []), folder.id]);
  }
  const parents = new Map<string, string>();
  for (const folder of byId.values()) {
    if (folder.parent_id) {
      if (folder.parent_id !== folder.id && byId.has(folder.parent_id))
        parents.set(folder.id, folder.parent_id);
      continue;
    }
    const path = folderPath(folder.relative_path);
    if (!path || !path.includes('/')) continue;
    const candidates = byPath.get(path.slice(0, path.lastIndexOf('/')));
    if (candidates?.length === 1 && candidates[0] !== folder.id)
      parents.set(folder.id, candidates[0]);
  }
  // Break a cyclic edge once; every supplied ID remains in the forest.
  const resolved = new Set<string>();
  for (const id of byId.keys()) {
    const chain = new Set<string>();
    let current: string | undefined = id;
    while (current && !resolved.has(current)) {
      if (chain.has(current)) {
        parents.delete(current);
        break;
      }
      chain.add(current);
      current = parents.get(current);
    }
    for (const member of chain) resolved.add(member);
  }
  const children = new Map<string, string[]>();
  for (const [id, parent] of parents)
    children.set(parent, [...(children.get(parent) ?? []), id]);
  const roots = [...byId.keys()].filter((id) => !parents.has(id));
  const pending = roots
    .reverse()
    .map((id) => ({ id, ancestors: [] as string[] }));
  const rows: FolderRowData[] = [];
  while (pending.length) {
    const entry = pending.pop()!;
    const childIds = children.get(entry.id) ?? [];
    rows.push({
      folder: byId.get(entry.id)!,
      ancestors: entry.ancestors,
      hasChildren: childIds.length > 0,
    });
    for (const id of [...childIds].reverse())
      pending.push({ id, ancestors: [...entry.ancestors, entry.id] });
  }
  return rows;
}

function FolderIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className="docmind-folder-icon"
      aria-hidden="true"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    >
      {expanded ? (
        <path d="M2.5 8V5A1.5 1.5 0 0 1 4 3.5h4l2 2h6A1.5 1.5 0 0 1 17.5 7v1M4 16.5h12l2-8h-14l-2 8Z" />
      ) : (
        <path d="M2.5 5a1.5 1.5 0 0 1 1.5-1.5h4l2 2H16A1.5 1.5 0 0 1 17.5 7v8a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5Z" />
      )}
    </svg>
  );
}

function FolderNode({
  row,
  selected,
  inherited,
  selectedBelow,
  expanded,
  filtering,
  childCount,
  path,
  inheritedHintId,
  onToggle,
  onExpand,
  children,
}: {
  row: FolderRowData;
  selected: boolean;
  inherited: boolean;
  selectedBelow: number;
  expanded: boolean;
  filtering: boolean;
  childCount: number;
  path: string;
  inheritedHintId: string;
  onToggle: (id: string) => void;
  onExpand: (id: string) => void;
  children: ReactNode[];
}) {
  const groupId = useId();
  const descriptionId = useId();
  const { folder, ancestors, hasChildren } = row;
  const handleSelection = () => onToggle(folder.id);
  const handleExpansion = () => onExpand(folder.id);
  const selectionNote =
    !selected && selectedBelow > 0 ? `하위 ${selectedBelow}개 선택` : '';
  const count =
    Number.isFinite(folder.document_count) &&
    folder.document_count >= 0 &&
    (!hasChildren || folder.document_count > 0)
      ? folder.document_count
      : undefined;
  const depth = ancestors.length;
  const descriptions =
    [
      !selected && inherited ? inheritedHintId : '',
      selectionNote ? descriptionId : '',
    ]
      .filter(Boolean)
      .join(' ') || undefined;
  return (
    <li
      className="docmind-folder-node"
      data-root={depth === 0 || undefined}
      data-deep={depth >= 3 || undefined}
    >
      <div
        className="docmind-folder-row"
        data-selected={selected || undefined}
        data-included={(!selected && inherited) || undefined}
      >
        {hasChildren ? (
          <button
            type="button"
            className="docmind-folder-expand"
            onClick={handleExpansion}
            aria-label={`${folder.name} 하위 폴더 ${expanded ? '접기' : '펼치기'}`}
            aria-expanded={expanded}
            aria-controls={expanded ? groupId : undefined}
            disabled={filtering}
          >
            <svg
              className="docmind-folder-chevron"
              aria-hidden="true"
              viewBox="0 0 12 12"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
            >
              <path
                d={
                  expanded
                    ? 'm2.5 4.5 3.5 3.5 3.5-3.5'
                    : 'm4.5 2.5 3.5 3.5-3.5 3.5'
                }
              />
            </svg>
            <FolderIcon expanded={expanded} />
          </button>
        ) : (
          <span className="docmind-folder-expand-space" aria-hidden="true">
            <FolderIcon expanded={false} />
          </span>
        )}
        <label
          className="docmind-folder-label"
          title={`${depth + 1}단계 · ${path}${inherited && !selected ? ' · 상위 폴더에 포함' : ''}`}
        >
          <input
            type="checkbox"
            checked={selected}
            onChange={handleSelection}
            aria-label={folder.name}
            aria-describedby={descriptions}
          />
          <span className="docmind-folder-name-block">
            <span className="docmind-folder-name">{folder.name}</span>
            <span className="docmind-folder-meta">
              {childCount > 0 && <span>하위 폴더 {childCount}개</span>}
              {count !== undefined && <span>문서 {count}개</span>}
              {depth >= 4 && (
                <span className="docmind-folder-depth">{depth + 1}단계</span>
              )}
              {selectionNote && (
                <span id={descriptionId} className="docmind-folder-inclusion">
                  {selectionNote}
                </span>
              )}
            </span>
          </span>
        </label>
      </div>
      {hasChildren && expanded && children.length > 0 && (
        <ul
          id={groupId}
          className="docmind-folder-children"
          aria-label={`${folder.name} 하위 폴더`}
        >
          {children}
        </ul>
      )}
    </li>
  );
}

export default function ScopeFolderPicker({
  folders,
  selectedIds,
  onToggle,
}: ScopeFolderPickerProps) {
  const inheritedHintId = useId();
  const [query, setQuery] = useState('');
  const [expansion, setExpansion] = useState<Map<string, boolean>>(
    () => new Map(),
  );
  const rows = useMemo(() => folderRows(folders), [folders]);
  const byId = new Map(rows.map((row) => [row.folder.id, row]));
  const children = new Map<string, string[]>();
  for (const row of rows) {
    const parent = row.ancestors.at(-1);
    if (parent)
      children.set(parent, [...(children.get(parent) ?? []), row.folder.id]);
  }
  const selected = new Set(selectedIds);
  const selectedCount = rows.filter((row) =>
    selected.has(row.folder.id),
  ).length;
  const selectedDescendants = new Map<string, number>();
  for (const row of rows) {
    if (selected.has(row.folder.id)) {
      for (const ancestor of row.ancestors)
        selectedDescendants.set(
          ancestor,
          (selectedDescendants.get(ancestor) ?? 0) + 1,
        );
    }
  }
  const filter = query.trim().toLocaleLowerCase();
  const matching = new Set<string>();
  if (filter) {
    for (const row of rows) {
      if (
        `${row.folder.name} ${row.folder.relative_path ?? ''}`
          .toLocaleLowerCase()
          .includes(filter)
      ) {
        matching.add(row.folder.id);
        for (const ancestor of row.ancestors) matching.add(ancestor);
      }
    }
  }
  const expandedById = new Map(
    rows.map((row) => [
      row.folder.id,
      expansion.get(row.folder.id) ??
        (row.ancestors.length < 2 ||
          (selectedDescendants.get(row.folder.id) ?? 0) > 0),
    ]),
  );
  const handleQuery = (event: ChangeEvent<HTMLInputElement>) =>
    setQuery(event.target.value);
  const handleExpansion = (id: string) =>
    setExpansion((current) => {
      const next = new Map(current);
      next.set(id, !expandedById.get(id));
      return next;
    });
  // Build child elements bottom-up so malformed or deep data never recurses here.
  const elements = new Map<string, ReactNode>();
  for (const row of [...rows].reverse()) {
    const id = row.folder.id;
    if (filter && !matching.has(id)) continue;
    const childIds = children.get(id) ?? [];
    const childElements = childIds
      .filter((child) => elements.has(child))
      .map((child) => elements.get(child)!);
    const path =
      row.folder.relative_path ||
      [...row.ancestors, id]
        .map((part) => byId.get(part)?.folder.name)
        .join(' / ');
    elements.set(
      id,
      <FolderNode
        key={id}
        row={row}
        selected={selected.has(id)}
        inherited={row.ancestors.some((ancestor) => selected.has(ancestor))}
        selectedBelow={selectedDescendants.get(id) ?? 0}
        expanded={
          filter ? childElements.length > 0 : Boolean(expandedById.get(id))
        }
        filtering={Boolean(filter)}
        childCount={childIds.length}
        path={path}
        inheritedHintId={inheritedHintId}
        onToggle={onToggle}
        onExpand={handleExpansion}
      >
        {childElements}
      </FolderNode>,
    );
  }
  const roots = rows.filter(
    (row) => row.ancestors.length === 0 && elements.has(row.folder.id),
  );
  return (
    <section className="docmind-folder-picker" aria-label="검색할 폴더 선택">
      <div className="docmind-folder-summary" role="status">
        폴더 {selectedCount}개 선택
      </div>
      <p className="docmind-folder-hint">
        상위 폴더를 선택하면 하위 폴더도 검색합니다.
      </p>
      <span id={inheritedHintId} className="docmind-folder-sr-only">
        상위 폴더에 포함
      </span>
      <input
        type="search"
        className="docmind-folder-filter"
        aria-label="폴더 찾기"
        placeholder="폴더 이름 또는 경로 찾기"
        value={query}
        onChange={handleQuery}
      />
      {roots.length ? (
        <ul className="docmind-folder-list" aria-label="폴더 목록">
          {roots.map((row) => elements.get(row.folder.id))}
        </ul>
      ) : (
        <p className="docmind-folder-empty">
          {filter ? '일치하는 폴더가 없습니다.' : '선택할 폴더가 없습니다.'}
        </p>
      )}
    </section>
  );
}
