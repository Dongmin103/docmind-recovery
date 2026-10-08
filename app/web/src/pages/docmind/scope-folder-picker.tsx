import type { DocMindFolder } from '@/services/docmind-service';
import {
  useId,
  useMemo,
  useState,
  type ChangeEvent,
  type CSSProperties,
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

function FolderRow({
  row,
  selected,
  inherited,
  selectedBelow,
  expanded,
  filtering,
  onToggle,
  onExpand,
}: {
  row: FolderRowData;
  selected: boolean;
  inherited: boolean;
  selectedBelow: number;
  expanded: boolean;
  filtering: boolean;
  onToggle: (id: string) => void;
  onExpand: (id: string) => void;
}) {
  const descriptionId = useId();
  const { folder, ancestors, hasChildren } = row;
  const handleSelection = () => onToggle(folder.id);
  const handleExpansion = () => onExpand(folder.id);
  const description =
    !selected && inherited
      ? '상위 폴더에 포함'
      : selectedBelow > 0
        ? `하위 ${selectedBelow}개 선택`
        : '';
  const count =
    Number.isFinite(folder.document_count) && folder.document_count >= 0
      ? folder.document_count
      : undefined;
  return (
    <li
      className="docmind-folder-row"
      data-selected={selected || undefined}
      data-included={(!selected && inherited) || undefined}
      data-nested={ancestors.length > 0 || undefined}
      style={
        { '--folder-depth': Math.min(ancestors.length, 3) } as CSSProperties
      }
    >
      {hasChildren ? (
        <button
          type="button"
          className="docmind-folder-expand"
          onClick={handleExpansion}
          aria-label={`${folder.name} 하위 폴더 ${expanded ? '접기' : '펼치기'}`}
          aria-expanded={expanded}
          disabled={filtering}
        >
          <svg
            aria-hidden="true"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
          >
            <path d={expanded ? 'm4 6 4 4 4-4' : 'm6 4 4 4-4 4'} />
          </svg>
        </button>
      ) : (
        <span className="docmind-folder-expand-space" aria-hidden="true" />
      )}
      <label
        className="docmind-folder-label"
        title={folder.relative_path || folder.name}
      >
        <input
          type="checkbox"
          checked={selected}
          onChange={handleSelection}
          aria-label={folder.name}
          aria-describedby={description ? descriptionId : undefined}
        />
        <svg
          className="docmind-folder-icon"
          aria-hidden="true"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinejoin="round"
        >
          <path d="M2.5 5a1.5 1.5 0 0 1 1.5-1.5h4l2 2H16A1.5 1.5 0 0 1 17.5 7v8a1.5 1.5 0 0 1-1.5 1.5H4A1.5 1.5 0 0 1 2.5 15Z" />
        </svg>
        <span className="docmind-folder-name-block">
          <span className="docmind-folder-name">{folder.name}</span>
          {description && (
            <span id={descriptionId} className="docmind-folder-inclusion">
              {description}
            </span>
          )}
        </span>
        {count !== undefined && (
          <span className="docmind-folder-count">문서 {count}개</span>
        )}
      </label>
    </li>
  );
}

export default function ScopeFolderPicker({
  folders,
  selectedIds,
  onToggle,
}: ScopeFolderPickerProps) {
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const rows = useMemo(() => folderRows(folders), [folders]);
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
  const visible = rows.filter((row) =>
    filter
      ? matching.has(row.folder.id)
      : !row.ancestors.some((id) => collapsed.has(id)),
  );
  const handleQuery = (event: ChangeEvent<HTMLInputElement>) =>
    setQuery(event.target.value);
  const handleExpansion = (id: string) =>
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  return (
    <section className="docmind-folder-picker" aria-label="검색할 폴더 선택">
      <div className="docmind-folder-summary" role="status">
        폴더 {selectedCount}개 선택
      </div>
      <p className="docmind-folder-hint">
        상위 폴더를 선택하면 하위 폴더도 검색합니다.
      </p>
      <input
        type="search"
        className="docmind-folder-filter"
        aria-label="폴더 찾기"
        placeholder="폴더 이름 또는 경로 찾기"
        value={query}
        onChange={handleQuery}
      />
      {visible.length ? (
        <ul className="docmind-folder-list" aria-label="폴더 목록">
          {visible.map((row) => (
            <FolderRow
              key={row.folder.id}
              row={row}
              selected={selected.has(row.folder.id)}
              inherited={row.ancestors.some((id) => selected.has(id))}
              selectedBelow={selectedDescendants.get(row.folder.id) ?? 0}
              expanded={Boolean(filter) || !collapsed.has(row.folder.id)}
              filtering={Boolean(filter)}
              onToggle={onToggle}
              onExpand={handleExpansion}
            />
          ))}
        </ul>
      ) : (
        <p className="docmind-folder-empty">
          {filter ? '일치하는 폴더가 없습니다.' : '선택할 폴더가 없습니다.'}
        </p>
      )}
    </section>
  );
}
