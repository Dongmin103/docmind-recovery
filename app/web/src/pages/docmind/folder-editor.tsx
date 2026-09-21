import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  createDocMindHierarchyFolder,
  updateDocMindHierarchyFolder,
  deleteDocMindHierarchyFolder,
  type DocMindHierarchyNode,
  type DocMindFolderMutationResult,
} from '@/services/docmind-service';
import { useFolderSelection } from './use-folder-selection';

const Messages: Record<string, string> = {
  DOCMIND_SOURCE_ROOT_IMMUTABLE:
    '원본 루트 폴더는 이름·위치를 변경하거나 삭제할 수 없습니다.',
  DOCMIND_FOLDER_PHYSICALLY_PROTECTED:
    '이 폴더 또는 하위 폴더가 Catalog 초안·검색 버전에서 사용 중이어서 변경할 수 없습니다.',
  DOCMIND_VERSION_PROTECTED:
    '폴더 안에 Catalog가 사용 중인 문서가 있어 변경할 수 없습니다.',
  DOCMIND_FOLDER_DELETE_NONEMPTY:
    '문서나 하위 폴더가 남아 있습니다. 비어 있는 폴더만 삭제할 수 있습니다.',
  DOCMIND_FOLDER_NOT_FOUND:
    '폴더가 없거나 접근할 수 없습니다. 목록을 새로고침하세요.',
  DOCMIND_FOLDER_PARENT_INVALID: '등록할 위치를 다시 선택해 주세요.',
  DOCMIND_FOLDER_OUTSIDE_PROJECT:
    '현재 원본 폴더 안의 위치만 선택할 수 있습니다.',
  DOCMIND_FOLDER_MOVE_CYCLE:
    '자기 자신이나 하위 폴더 안으로 이동할 수 없습니다.',
  DOCMIND_FOLDER_NAME_INVALID:
    '폴더 이름을 확인하세요. /, \\와 빈 이름은 사용할 수 없습니다.',
  DOCMIND_IMPORT_PATH_CONFLICT: '선택한 위치에 같은 이름의 항목이 있습니다.',
  DOCMIND_FOLDER_UPDATE_EMPTY: '바꿀 이름 또는 새 위치를 선택하세요.',
};

const Modes = [
  { id: 'create', label: '폴더 추가' },
  { id: 'rename', label: '이름 바꾸기' },
  { id: 'move', label: '위치 옮기기' },
  { id: 'delete', label: '삭제' },
] as const;
type Mode = (typeof Modes)[number]['id'];
type Props = {
  nodes: DocMindHierarchyNode[];
  rootId?: string;
  loading?: boolean;
  onChanged: () => Promise<void>;
};
const normalizeName = (name: string) => name.normalize('NFC').trim();
const validName = (name: string) =>
  Boolean(name) &&
  !['.', '..'].includes(name) &&
  !name.includes('/') &&
  !name.includes('\\') &&
  !name.includes('\0') &&
  Array.from(name).length <= 255;

function result(response: {
  data: { code: number; message?: string; data: DocMindFolderMutationResult };
}) {
  if (response.data.code !== 0)
    throw new Error(response.data.message || 'DOCMIND_FOLDER_REQUEST_FAILED');
  return response.data.data;
}

export default function FolderEditor({
  nodes,
  rootId,
  loading = false,
  onChanged,
}: Props) {
  const folders = useMemo(
    () => nodes.filter((node) => node.type === 'folder'),
    [nodes],
  );
  const ids = useMemo(() => folders.map((folder) => folder.file_id), [folders]);
  const [mode, setMode] = useState<Mode>('create');
  const [createParentId, setCreateParentId] = useState('');
  const [newName, setNewName] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [editName, setEditName] = useState('');
  const [destinationId, setDestinationId] = useState('');
  const [pendingSelection, setPendingSelection] =
    useState<DocMindFolderMutationResult>();
  const [notice, setNotice] = useState<{ text: string; kind: string }>();
  const [error, setError] = useState('');
  const [refreshError, setRefreshError] = useState(false);
  const editInput = useRef<HTMLInputElement>(null);
  const selected = folders.find((folder) => folder.file_id === selectedId);
  const createParent = folders.find(
    (folder) => folder.file_id === createParentId,
  );
  useFolderSelection(ids, createParentId, setCreateParentId, rootId);

  const destinations = useMemo(() => {
    const blocked = new Set([selectedId]);
    const children = new Map<string, string[]>();
    for (const folder of folders) {
      const siblings = children.get(folder.parent_file_id || '') || [];
      siblings.push(folder.file_id);
      children.set(folder.parent_file_id || '', siblings);
    }
    const queue = [selectedId];
    while (queue.length)
      for (const child of children.get(queue.pop()!) || []) {
        if (!blocked.has(child)) {
          blocked.add(child);
          queue.push(child);
        }
      }
    return folders.filter((folder) => !blocked.has(folder.file_id));
  }, [folders, selectedId]);

  useEffect(() => {
    if (pendingSelection) {
      if (
        folders.some((folder) => folder.file_id === pendingSelection.folder_id)
      )
        setPendingSelection(undefined);
      return;
    }
    if (selectedId && !selected && !loading) {
      setSelectedId('');
      setEditName('');
      setDestinationId('');
    }
  }, [folders, selectedId, selected, pendingSelection, loading]);

  useLayoutEffect(() => {
    if (mode === 'rename' && notice?.kind === 'created')
      editInput.current?.focus();
  }, [mode, notice, selected?.file_id]);

  const refresh = async () => {
    try {
      await onChanged();
      setRefreshError(false);
    } catch {
      setRefreshError(true);
    }
  };
  const starting = () => {
    setError('');
    setNotice(undefined);
    setRefreshError(false);
  };
  const failed = (failure: Error) => {
    setError(failure.message);
    void refresh();
  };

  const create = useMutation({
    mutationKey: ['docmind-folder-editor-create'],
    mutationFn: async ({
      parentId,
      name,
    }: {
      parentId: string;
      name: string;
    }) => result(await createDocMindHierarchyFolder(parentId, name)),
    onMutate: starting,
    onError: failed,
    onSuccess: async (created) => {
      setNewName('');
      setPendingSelection(created);
      setSelectedId(created.folder_id);
      setEditName(created.name || '');
      setDestinationId(created.parent_file_id || '');
      setMode('rename');
      setNotice({
        text: `“${created.name}” 폴더를 추가하고 이름 바꾸기 대상으로 선택했습니다.`,
        kind: 'created',
      });
      await refresh();
    },
  });
  const update = useMutation({
    mutationKey: ['docmind-folder-editor-update'],
    mutationFn: async ({
      id,
      name,
      parentId,
    }: {
      id: string;
      name?: string;
      parentId?: string;
    }) =>
      result(
        await updateDocMindHierarchyFolder(id, {
          name,
          parentFileId: parentId,
        }),
      ),
    onMutate: starting,
    onError: failed,
    onSuccess: async (updated, request) => {
      setEditName(updated.name || '');
      setDestinationId(updated.parent_file_id || '');
      setNotice({
        text: request.name
          ? `폴더 이름을 “${updated.name}”로 바꿨습니다.`
          : `“${updated.name}” 폴더 위치를 옮겼습니다.`,
        kind: 'updated',
      });
      await refresh();
    },
  });
  const remove = useMutation({
    mutationKey: ['docmind-folder-editor-delete'],
    mutationFn: async ({ id }: { id: string; name: string }) =>
      result(await deleteDocMindHierarchyFolder(id)),
    onMutate: starting,
    onError: failed,
    onSuccess: async (_deleted, request) => {
      setSelectedId('');
      setEditName('');
      setDestinationId('');
      setNotice({
        text: `“${request.name}” 빈 폴더를 삭제했습니다.`,
        kind: 'deleted',
      });
      await refresh();
    },
  });
  const busy = create.isPending || update.isPending || remove.isPending;
  const name = normalizeName(mode === 'create' ? newName : editName);
  const changedName = Boolean(
    selected && name !== normalizeName(selected.name),
  );
  const changedParent = Boolean(
    selected && destinationId && destinationId !== selected.parent_file_id,
  );
  const validDestination = destinations.some(
    (folder) => folder.file_id === destinationId,
  );
  const canCreate =
    !loading &&
    !busy &&
    !refreshError &&
    createParent?.mutation_capabilities?.can_create_child === true &&
    validName(name);
  const canRename =
    !loading &&
    !busy &&
    !refreshError &&
    selected?.mutation_capabilities?.can_edit === true &&
    validName(name) &&
    changedName;
  const canMove =
    !loading &&
    !busy &&
    !refreshError &&
    selected?.mutation_capabilities?.can_edit === true &&
    validDestination &&
    changedParent;
  const canDelete =
    !loading &&
    !busy &&
    !refreshError &&
    selected?.mutation_capabilities?.can_delete === true;
  const blocker =
    mode === 'delete'
      ? selected?.mutation_capabilities?.delete_blocker_code
      : selected?.mutation_capabilities?.edit_blocker_code;
  const protectionMessage =
    selected &&
    mode !== 'create' &&
    (mode === 'delete'
      ? !selected.mutation_capabilities?.can_delete
      : !selected.mutation_capabilities?.can_edit)
      ? Messages[blocker || ''] ||
        '편집 가능 여부를 확인할 수 없습니다. 목록을 새로고침하세요.'
      : '';

  const changeMode = (event: React.MouseEvent<HTMLButtonElement>) => {
    setMode(event.currentTarget.value as Mode);
    setError('');
  };
  const selectFolder = (event: React.ChangeEvent<HTMLSelectElement>) => {
    const folder = folders.find((item) => item.file_id === event.target.value);
    setSelectedId(folder?.file_id || '');
    setEditName(folder?.name || '');
    setDestinationId(folder?.parent_file_id || '');
    setPendingSelection(undefined);
    setNotice(undefined);
    setError('');
  };
  const changeCreateParent = (event: React.ChangeEvent<HTMLSelectElement>) =>
    setCreateParentId(event.target.value);
  const changeNewName = (event: React.ChangeEvent<HTMLInputElement>) =>
    setNewName(event.target.value);
  const changeEditName = (event: React.ChangeEvent<HTMLInputElement>) =>
    setEditName(event.target.value);
  const changeDestination = (event: React.ChangeEvent<HTMLSelectElement>) =>
    setDestinationId(event.target.value);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (mode === 'create' && canCreate)
      create.mutate({ parentId: createParentId, name });
    if (mode === 'rename' && canRename && selected)
      update.mutate({
        id: selected.file_id,
        name,
      });
    if (mode === 'move' && canMove && selected)
      update.mutate({
        id: selected.file_id,
        parentId: destinationId,
      });
    if (
      mode === 'delete' &&
      canDelete &&
      selected &&
      window.confirm(`“${selected.name}” 빈 폴더를 삭제할까요?`)
    )
      remove.mutate({ id: selected.file_id, name: selected.name });
  };
  const refreshList = () => {
    void refresh();
  };
  const option = (folder: DocMindHierarchyNode) => (
    <option key={folder.file_id} value={folder.file_id}>
      {folder.relative_path || folder.name}
    </option>
  );
  const fieldClass =
    'h-10 w-full rounded-lg border border-border-button bg-bg-input px-3 text-sm disabled:opacity-50';

  return (
    <section aria-label="폴더 편집" className="mt-4 space-y-4">
      <div
        className="flex flex-wrap gap-2"
        role="group"
        aria-label="폴더 작업 선택"
      >
        {Modes.map((item) => (
          <button
            key={item.id}
            value={item.id}
            type="button"
            onClick={changeMode}
            disabled={busy}
            aria-pressed={mode === item.id}
            aria-label={`작업 선택: ${item.label}`}
            className="rounded-lg border border-border-button px-3 py-2 text-sm text-text-secondary aria-pressed:border-accent-primary aria-pressed:text-accent-primary disabled:opacity-50"
          >
            {item.label}
          </button>
        ))}
      </div>
      {notice && (
        <p role="status" className="text-sm text-state-success">
          {notice.text}
        </p>
      )}
      {refreshError && (
        <p role="alert" className="text-sm text-state-warning">
          목록을 새로고침하지 못했습니다. 성공한 작업을 다시 실행하지 말고
          목록을 갱신해 주세요.
        </p>
      )}
      {error && (
        <div role="alert" className="text-sm text-state-warning">
          <p>
            {Messages[error] ||
              '폴더 작업을 완료하지 못했습니다. 연결 상태와 선택한 폴더를 확인하세요.'}
          </p>
          <details className="mt-1 text-xs text-text-secondary">
            <summary>오류 상세</summary>
            {error}
          </details>
        </div>
      )}
      <form onSubmit={submit} className="space-y-4">
        {mode === 'create' ? (
          <>
            <label className="grid gap-1.5 text-sm">
              만들 위치
              <select
                className={fieldClass}
                aria-label="새 폴더의 부모"
                value={createParentId}
                onChange={changeCreateParent}
                disabled={busy}
              >
                <option value="" disabled>
                  폴더를 선택하세요
                </option>
                {folders.map(option)}
              </select>
            </label>
            <label className="grid gap-1.5 text-sm">
              새 폴더 이름
              <input
                className={fieldClass}
                aria-label="추가할 폴더 이름"
                value={newName}
                onChange={changeNewName}
                placeholder="예: 세척 밸리데이션"
                disabled={busy}
              />
            </label>
            {createParent && !createParent.mutation_capabilities && (
              <p className="text-xs text-text-secondary">
                폴더 권한을 확인하려면 목록을 새로고침하세요.
              </p>
            )}
          </>
        ) : (
          <>
            <label className="grid gap-1.5 text-sm">
              {mode === 'delete'
                ? '삭제할 폴더'
                : mode === 'rename'
                  ? '이름을 바꿀 폴더'
                  : '위치를 옮길 폴더'}
              <select
                className={fieldClass}
                aria-label={
                  mode === 'delete'
                    ? '삭제할 폴더'
                    : mode === 'rename'
                      ? '이름을 바꿀 폴더'
                      : '위치를 옮길 폴더'
                }
                value={selectedId}
                onChange={selectFolder}
                disabled={busy}
              >
                <option value="">폴더를 선택하세요</option>
                {pendingSelection && !selected && (
                  <option value={pendingSelection.folder_id}>
                    {pendingSelection.name} (목록 갱신 대기)
                  </option>
                )}
                {folders.map(option)}
              </select>
            </label>
            {selected && (
              <p className="text-xs text-text-secondary">
                현재 위치: {selected.relative_path || selected.name}
              </p>
            )}
            {protectionMessage && (
              <p role="note" className="text-sm text-state-warning">
                {protectionMessage}
              </p>
            )}
            {mode === 'rename' && selected && (
              <>
                <label className="grid gap-1.5 text-sm">
                  새 이름
                  <input
                    ref={editInput}
                    className={fieldClass}
                    aria-label="폴더 새 이름"
                    value={editName}
                    onChange={changeEditName}
                    disabled={busy || !selected.mutation_capabilities?.can_edit}
                  />
                </label>
                {!protectionMessage && !changedName && (
                  <p className="text-xs text-text-secondary">
                    다른 이름을 입력하면 이름을 바꿀 수 있습니다.
                  </p>
                )}
              </>
            )}
            {mode === 'move' && selected && (
              <>
                <label className="grid gap-1.5 text-sm">
                  새 위치
                  <select
                    className={fieldClass}
                    aria-label="새 위치의 부모 폴더"
                    value={destinationId}
                    onChange={changeDestination}
                    disabled={busy || !selected.mutation_capabilities?.can_edit}
                  >
                    <option value="" disabled>
                      폴더를 선택하세요
                    </option>
                    {destinations.map(option)}
                  </select>
                </label>
                {!protectionMessage && !changedParent && (
                  <p className="text-xs text-text-secondary">
                    다른 부모 폴더를 선택하면 위치를 옮길 수 있습니다.
                  </p>
                )}
              </>
            )}
            {mode === 'delete' && !protectionMessage && selected && (
              <p className="text-xs text-text-secondary">
                비어 있는 이 폴더만 삭제합니다. 실행 전에 다시 확인합니다.
              </p>
            )}
          </>
        )}
        {name &&
          !validName(name) &&
          (mode === 'create' || mode === 'rename') && (
            <p className="text-xs text-state-warning">
              이름은 255자 이내여야 하며 /, \\는 사용할 수 없습니다.
            </p>
          )}
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={
              mode === 'create'
                ? !canCreate
                : mode === 'rename'
                  ? !canRename
                  : mode === 'move'
                    ? !canMove
                    : !canDelete
            }
            className={`rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-40 ${mode === 'delete' ? 'border border-state-error text-state-error' : 'bg-accent-primary text-primary-foreground'}`}
          >
            {busy
              ? '처리 중…'
              : mode === 'create'
                ? '폴더 추가 실행'
                : mode === 'rename'
                  ? '이름 바꾸기'
                  : mode === 'move'
                    ? '위치 옮기기'
                    : '빈 폴더 삭제'}
          </button>
          <button
            type="button"
            onClick={refreshList}
            disabled={busy}
            className="rounded px-2 py-2 text-xs text-text-secondary"
          >
            목록 새로고침
          </button>
        </div>
      </form>
    </section>
  );
}
