import DocumentPreview from '@/components/document-preview';
import HighLightMarkdown from '@/components/highlight-markdown';
import { FileIcon } from '@/components/icon-font';
import { Input } from '@/components/originui/input';
import { Modal } from '@/components/ui/modal/modal';
import {
  useGetChunkHighlights,
  useGetDocumentUrl,
} from '@/hooks/use-document-request';
import type { ITestingChunk } from '@/interfaces/database/dataset';
import type {
  DocMindFolderCatalog,
  DocMindHierarchy,
  DocMindHierarchyNode,
  DocMindRegistration,
  DocMindRegistrationList,
  DocMindSearchRequest,
  DocMindSearchResult,
  DocMindSearchScope,
} from '@/services/docmind-service';
import {
  getDocMindFolders,
  getDocMindHierarchy,
  getDocMindRegistrations,
  registerDocMindDocuments,
  retryDocMindRegistration,
  searchDocMind,
} from '@/services/docmind-service';
import {
  buildDocMindDocumentPath,
  parseDocMindReturnView,
  parseDocMindView,
  type DocMindSection,
} from '@/utils/docmind-workspace';
import { useMutation, useQuery } from '@tanstack/react-query';
import { ChevronDown, Search, X } from 'lucide-react';
import * as React from 'react';
import { type FormEvent, useEffect, useMemo, useState } from 'react';
import { ErrorBoundary } from 'react-error-boundary';
import { useSearchParams } from 'react-router';
import RegistrationPanel from './registration-panel';
import SourceTree from './source-tree';
import { useFolderSelection } from './use-folder-selection';
import { ChunkInspection, WorkspaceSettings } from './workspace-views';
import WorkspaceShell from './workspace-shell';
import './index.less';

const RESULTS_PER_PAGE = 10;
const DocMindKeys = {
  folders: () => ['docmind-folders'] as const,
  hierarchy: () => ['docmind-hierarchy'] as const,
  registrations: () => ['docmind-registrations'] as const,
};

const InspectionNavigation = React.createContext<{
  view: DocMindSection;
  open: (datasetId: string, documentId: string) => void;
} | null>(null);

function WorkspaceViewError() {
  return (
    <p role="alert" className="p-6 text-text-secondary">
      화면을 불러오지 못했습니다. 메뉴에서 다른 화면으로 이동한 후 다시 시도해
      주세요.
    </p>
  );
}

function getChunkLocation(chunk: ITestingChunk): string {
  const hwpLocator = chunk.hwp_locator;
  if (hwpLocator) {
    const section = `섹션 ${hwpLocator.section_index + 1}`;
    if (hwpLocator.table) {
      return `${section} > 표 ${hwpLocator.table.row + 1}행 ${hwpLocator.table.column + 1}열`;
    }
    return hwpLocator.paragraph_index === undefined
      ? `${section} > ${hwpLocator.block_locator}`
      : `${section} > 문단 ${hwpLocator.paragraph_index + 1}`;
  }
  const locator = (chunk as ITestingChunk & { office_locator?: any })
    .office_locator;
  if (locator?.kind === 'docx') {
    const path = Array.isArray(locator.heading_path)
      ? locator.heading_path.join(' > ')
      : '';
    return path || locator.item_locator || 'DOCX 구조 위치';
  }
  if (locator?.kind === 'xlsx') {
    return `${locator.sheet}${locator.cell_range ? `!${locator.cell_range}` : ''}`;
  }
  if (locator?.kind === 'pptx') return `슬라이드 ${locator.slide}`;
  const position = chunk.positions?.find(
    (value) => Array.isArray(value) && Number.isFinite(value[0]),
  );
  return position?.[0] ? `p. ${position[0]}` : '문서 위치';
}

function InspectionLink({
  datasetId,
  documentId,
  compact = false,
}: {
  datasetId?: string;
  documentId?: string;
  compact?: boolean;
}) {
  const navigation = React.useContext(InspectionNavigation);
  const path = buildDocMindDocumentPath(
    datasetId,
    documentId,
    navigation?.view,
  );
  if (!path) return null;
  const open = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (
      !navigation ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    navigation.open(datasetId!, documentId!);
  };
  return (
    <a
      href={path}
      onClick={open}
      className={
        compact
          ? 'rounded border border-accent-primary/30 px-2 py-1 text-xs text-accent-primary hover:bg-accent-primary/10'
          : 'rounded-lg border border-accent-primary/30 px-3 py-1.5 text-sm text-accent-primary hover:bg-accent-primary/10'
      }
    >
      {compact ? '문서 상세' : '원문 · 청크 확인'}
    </a>
  );
}

function DocumentModal({
  chunk,
  datasetId,
  onClose,
}: {
  chunk?: ITestingChunk;
  datasetId?: string;
  onClose: () => void;
}) {
  const getDocumentUrl = useGetDocumentUrl(chunk?.doc_id);
  const { highlights, setWidthAndHeight } = useGetChunkHighlights(
    (chunk ?? {}) as any,
  );
  const name = chunk?.docnm_kwd || chunk?.doc_name || '';
  return (
    <Modal
      title={name}
      open={Boolean(chunk)}
      onCancel={onClose}
      showfooter={false}
    >
      {chunk && (
        <div>
          <div className="mb-3 flex justify-end">
            <InspectionLink datasetId={datasetId} documentId={chunk.doc_id} />
          </div>
          <DocumentPreview
            className="docmind-document-preview !h-[calc(100dvh-300px)] overflow-auto border-none p-0"
            fileType={name.split('.').pop()?.toLowerCase() || ''}
            highlights={highlights}
            setWidthAndHeight={setWidthAndHeight}
            url={getDocumentUrl()}
          />
        </div>
      )}
    </Modal>
  );
}

function RankedResult({
  chunk,
  rank,
  datasetId,
  onOpen,
}: {
  chunk: ITestingChunk;
  rank: number;
  datasetId?: string;
  onOpen: () => void;
}) {
  const name = chunk.docnm_kwd || chunk.doc_name;
  const location = getChunkLocation(chunk);
  const relativePath = chunk.document_relative_path
    ?.trim()
    .replace(/\\/g, '/')
    .split('/')
    .filter(Boolean)
    .join(' > ');
  return (
    <article className="grid grid-cols-[3.5rem_minmax(0,1fr)] gap-x-5 border-b border-border-button py-6 max-sm:grid-cols-[2.5rem_minmax(0,1fr)]">
      <div className="pt-2 text-3xl font-medium text-text-secondary">
        {rank}
      </div>
      <div className="min-w-0">
        <button
          type="button"
          onClick={onOpen}
          className="block w-full min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
          aria-label={`${name} ${location} 원문 열기`}
        >
          <div className="flex items-center gap-2 text-sm text-text-secondary">
            <FileIcon name={name} />
            <span className="truncate">{name}</span>
            <span aria-hidden="true">·</span>
            <span>{location}</span>
          </div>
          {relativePath && (
            <span className="mt-1 block truncate text-xs text-text-secondary">
              {relativePath}
            </span>
          )}
          <span className="mt-2 block text-xl font-semibold text-accent-primary hover:underline">
            {name}
          </span>
          <div className="docmind-snippet mt-2 overflow-hidden text-base leading-7 text-text-primary">
            <HighLightMarkdown>{chunk.content_with_weight}</HighLightMarkdown>
          </div>
        </button>
        <div className="mt-3">
          <InspectionLink datasetId={datasetId} documentId={chunk.doc_id} />
        </div>
      </div>
    </article>
  );
}

function selectionLabel(
  mode: DocMindSearchScope['mode'],
  folderCount: number,
  documentCount: number,
) {
  if (mode === 'folders') return `폴더 ${folderCount}개`;
  if (mode === 'documents') return `문서 ${documentCount}개`;
  return '전체 문서';
}

export default function DocMind() {
  const [params, setParams] = useSearchParams();
  const workspaceView = parseDocMindView(params.get('view'));
  const returnView = parseDocMindReturnView(params.get('return'));
  const activeSection =
    workspaceView === 'document' ? returnView : workspaceView;
  const inspectionDatasetId = params.get('id') ?? undefined;
  const inspectionDocumentId = params.get('doc_id') ?? undefined;
  const validInspection = Boolean(
    buildDocMindDocumentPath(inspectionDatasetId, inspectionDocumentId),
  );
  const inspectionOrigin = React.useRef<HTMLElement | null>(null);
  const [query, setQuery] = useState('');
  const [selectedChunk, setSelectedChunk] = useState<ITestingChunk>();
  const [visibleCount, setVisibleCount] = useState(RESULTS_PER_PAGE);
  const [scopeMode, setScopeMode] = useState<DocMindSearchScope['mode']>('all');
  const [selectedFolderIds, setSelectedFolderIds] = useState<string[]>([]);
  const [selectedDocumentIds, setSelectedDocumentIds] = useState<string[]>([]);
  const [scopeMenuOpen, setScopeMenuOpen] = useState(false);
  const [registrationFolderId, setRegistrationFolderId] = useState('');
  const [registrationFiles, setRegistrationFiles] = useState<File[]>([]);

  const folderCatalog = useQuery<DocMindFolderCatalog, Error>({
    queryKey: DocMindKeys.folders(),
    queryFn: async () => {
      const { data } = await getDocMindFolders();
      if (data.code !== 0)
        throw new Error(data.message || '폴더 목록 조회 실패');
      return data.data as DocMindFolderCatalog;
    },
    staleTime: 10_000,
    refetchInterval:
      workspaceView === 'search' || workspaceView === 'library'
        ? 10_000
        : false,
  });
  const canAdminister = Boolean(folderCatalog.data?.can_administer);
  const hierarchyQuery = useQuery<DocMindHierarchy, Error>({
    queryKey: DocMindKeys.hierarchy(),
    queryFn: async () => {
      const { data } = await getDocMindHierarchy();
      if (data.code !== 0)
        throw new Error(data.message || '문서 목록 조회 실패');
      return data.data as DocMindHierarchy;
    },
    enabled:
      canAdminister &&
      (workspaceView === 'search' || workspaceView === 'library'),
    refetchInterval:
      canAdminister && workspaceView === 'library' ? 10_000 : false,
  });
  const retrieval = useMutation<
    DocMindSearchResult,
    Error,
    DocMindSearchRequest
  >({
    mutationKey: ['docmind-search'],
    mutationFn: async (searchRequest) => {
      const { data } = await searchDocMind(searchRequest);
      if (data.code !== 0) throw new Error(data.message || '문서 검색 실패');
      return data.data as DocMindSearchResult;
    },
    onMutate: () => setVisibleCount(RESULTS_PER_PAGE),
  });
  const registrationQuery = useQuery<DocMindRegistrationList, Error>({
    queryKey: DocMindKeys.registrations(),
    queryFn: async () => {
      const { data } = await getDocMindRegistrations();
      if (data.code !== 0)
        throw new Error(data.message || '등록 현황 조회 실패');
      return data.data as DocMindRegistrationList;
    },
    enabled: canAdminister && workspaceView === 'library',
    refetchInterval:
      canAdminister && workspaceView === 'library' ? 3_000 : false,
  });
  const registrationUpload = useMutation<
    unknown,
    Error,
    { folderId: string; files: File[] }
  >({
    mutationKey: ['docmind-registration-upload'],
    mutationFn: async ({ folderId, files }) => {
      const { data } = await registerDocMindDocuments(folderId, files);
      if (data.code !== 0) throw new Error(data.message || '문서 등록 실패');
      return data.data;
    },
    onSuccess: async () => {
      setRegistrationFiles([]);
      await Promise.all([
        registrationQuery.refetch(),
        hierarchyQuery.refetch(),
      ]);
    },
  });
  const registrationRetry = useMutation<unknown, Error, string>({
    mutationKey: ['docmind-registration-retry'],
    mutationFn: async (registrationId) => {
      const key =
        globalThis.crypto?.randomUUID?.() ??
        `retry-${registrationId}-${Date.now()}`;
      const { data } = await retryDocMindRegistration(registrationId, key);
      if (data.code !== 0)
        throw new Error(data.message || '문서 처리 재시도 실패');
      return data.data;
    },
    onSuccess: async () => registrationQuery.refetch(),
  });

  const folders = useMemo(
    () => folderCatalog.data?.folders ?? [],
    [folderCatalog.data?.folders],
  );
  const refreshLibrary = () => {
    void Promise.all([
      folderCatalog.refetch(),
      hierarchyQuery.refetch(),
      registrationQuery.refetch(),
    ]);
  };
  const refreshFolders = () => {
    void folderCatalog.refetch();
  };
  const documentNodes = useMemo(
    () => folderCatalog.data?.documents ?? [],
    [folderCatalog.data?.documents],
  );
  const canonicalFolderIds = useMemo(
    () =>
      folders
        .filter((folder) => selectedFolderIds.includes(folder.id))
        .map((folder) => folder.id),
    [folders, selectedFolderIds],
  );
  const canonicalDocumentIds = useMemo(
    () =>
      documentNodes
        .filter((document) => selectedDocumentIds.includes(document.id))
        .map((document) => document.id),
    [documentNodes, selectedDocumentIds],
  );
  const explicitScopeEmpty =
    (scopeMode === 'folders' && canonicalFolderIds.length === 0) ||
    (scopeMode === 'documents' && canonicalDocumentIds.length === 0);
  const searchChunks =
    retrieval.data?.ranked_chunks ?? retrieval.data?.chunks ?? [];
  const folderNames = useMemo(
    () => new Map(folders.map((folder) => [folder.id, folder.name])),
    [folders],
  );
  const documentFolderNames = useMemo(
    () =>
      new Map(
        documentNodes.map((document) => [
          document.id,
          folderNames.get(document.folder_id) ?? document.relative_path,
        ]),
      ),
    [documentNodes, folderNames],
  );

  useFolderSelection(
    folders.map((folder) => folder.id),
    registrationFolderId,
    setRegistrationFolderId,
  );
  useEffect(() => {
    if (workspaceView !== 'document' && inspectionOrigin.current) {
      if (inspectionOrigin.current.isConnected) {
        inspectionOrigin.current.focus({ preventScroll: true });
      }
      inspectionOrigin.current = null;
    }
  }, [workspaceView]);

  const openInspection = (datasetId: string, documentId: string) => {
    const path = buildDocMindDocumentPath(datasetId, documentId, activeSection);
    if (!path) return;
    inspectionOrigin.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setSelectedChunk(undefined);
    setParams(new URLSearchParams(path.split('?')[1]));
  };
  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    if (
      !query.trim() ||
      explicitScopeEmpty ||
      folderCatalog.isError ||
      folderCatalog.isPending
    )
      return;
    const scope: DocMindSearchScope =
      scopeMode === 'folders'
        ? { mode: scopeMode, folderIds: canonicalFolderIds }
        : scopeMode === 'documents'
          ? { mode: scopeMode, documentIds: canonicalDocumentIds }
          : { mode: 'all' };
    retrieval.mutate({
      question: query.trim(),
      projectId: folderCatalog.data?.project_id,
      scope,
    });
  };
  const toggle = (
    id: string,
    setter: React.Dispatch<React.SetStateAction<string[]>>,
  ) =>
    setter((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id],
    );
  const submitRegistration = (event: FormEvent) => {
    event.preventDefault();
    if (!registrationFolderId || !registrationFiles.length) return;
    registrationUpload.mutate({
      folderId: registrationFolderId,
      files: registrationFiles,
    });
  };

  return (
    <InspectionNavigation.Provider
      value={{ view: activeSection, open: openInspection }}
    >
      <WorkspaceShell
        activeSection={activeSection}
        canAdminister={canAdminister}
        sharedWorkspace={folderCatalog.data?.workspace_mode === 'shared'}
      >
        <main className="flex h-full min-h-0 flex-col">
          {folderCatalog.isError && (
            <div role="alert" className="p-5 text-state-error">
              <p>
                검색 범위를 불러오지 못했습니다. 연결을 확인한 뒤 다시
                시도하세요.
              </p>
              <button
                type="button"
                onClick={refreshFolders}
                className="mt-2 rounded-lg border border-border-button px-3 py-2"
              >
                검색 범위 다시 불러오기
              </button>
            </div>
          )}
          {workspaceView === 'document' && (
            <div className="min-h-0 flex-1" aria-label="문서 상세">
              {validInspection ? (
                <ErrorBoundary
                  FallbackComponent={WorkspaceViewError}
                  resetKeys={[inspectionDocumentId]}
                >
                  <React.Suspense
                    fallback={
                      <p role="status" className="p-6">
                        원문과 청크를 불러오는 중입니다.
                      </p>
                    }
                  >
                    <ChunkInspection
                      key={`${inspectionDatasetId}:${inspectionDocumentId}`}
                      embedded
                      readOnly
                      onBack={() =>
                        setParams({ view: returnView }, { replace: true })
                      }
                    />
                  </React.Suspense>
                </ErrorBoundary>
              ) : (
                <p role="alert" className="p-6">
                  문서 주소가 올바르지 않습니다.
                </p>
              )}
            </div>
          )}
          {workspaceView === 'settings' && (
            <ErrorBoundary
              FallbackComponent={WorkspaceViewError}
              resetKeys={[workspaceView]}
            >
              <React.Suspense
                fallback={
                  <p role="status" className="p-6">
                    설정을 불러오는 중입니다.
                  </p>
                }
              >
                <WorkspaceSettings
                  canAdminister={canAdminister}
                  sharedWorkspace={
                    folderCatalog.data?.workspace_mode === 'shared'
                  }
                />
              </React.Suspense>
            </ErrorBoundary>
          )}
          {workspaceView === 'search' && (
            <div className="min-h-0 flex-1 overflow-y-auto">
              <div className="mx-auto w-full max-w-6xl px-5 py-8">
                <p className="text-sm text-text-secondary">
                  전체 · 폴더 · 문서 범위를 직접 선택해 근거 청크를 검색합니다.
                </p>
                <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto]">
                  <form
                    id="docmind-search"
                    onSubmit={submitSearch}
                    className="relative"
                  >
                    <Input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="문서에서 찾을 내용을 입력하세요"
                      aria-label="문서 검색 질문"
                      className="h-11 pr-12 text-base"
                    />
                    {query && (
                      <button
                        type="button"
                        onClick={() => setQuery('')}
                        className="absolute right-4 top-1/2 -translate-y-1/2"
                        aria-label="검색어 지우기"
                      >
                        <X className="size-5" />
                      </button>
                    )}
                  </form>
                  <div className="relative">
                    <button
                      type="button"
                      onClick={() => setScopeMenuOpen((open) => !open)}
                      className="flex h-11 min-w-40 items-center justify-between gap-2 rounded-lg border border-border-button bg-bg-card px-4"
                      aria-expanded={scopeMenuOpen}
                      aria-haspopup="dialog"
                    >
                      <span>
                        {selectionLabel(
                          scopeMode,
                          canonicalFolderIds.length,
                          canonicalDocumentIds.length,
                        )}
                      </span>
                      <ChevronDown className="size-5" />
                    </button>
                    {scopeMenuOpen && (
                      <div
                        className="absolute right-0 top-12 z-30 max-h-96 w-80 overflow-y-auto rounded-xl border border-border-button bg-bg-card p-3 shadow-2xl"
                        role="dialog"
                        aria-label="검색 범위 선택"
                      >
                        <fieldset>
                          <legend className="mb-2 text-sm font-semibold">
                            검색 범위
                          </legend>
                          {(['all', 'folders', 'documents'] as const).map(
                            (mode) => (
                              <label
                                key={mode}
                                className="flex items-center gap-2 rounded px-2 py-2"
                              >
                                <input
                                  type="radio"
                                  name="docmind-scope"
                                  checked={scopeMode === mode}
                                  onChange={() => setScopeMode(mode)}
                                />
                                {mode === 'all'
                                  ? '전체 문서'
                                  : mode === 'folders'
                                    ? '선택 폴더와 하위 폴더'
                                    : '선택 문서'}
                              </label>
                            ),
                          )}
                        </fieldset>
                        {scopeMode === 'folders' && (
                          <div className="mt-2 border-t border-border-button pt-2">
                            {folders.map((folder) => (
                              <label
                                key={folder.id}
                                className="flex items-center gap-2 rounded px-2 py-2"
                              >
                                <input
                                  type="checkbox"
                                  checked={selectedFolderIds.includes(
                                    folder.id,
                                  )}
                                  onChange={() =>
                                    toggle(folder.id, setSelectedFolderIds)
                                  }
                                />
                                <span className="truncate">
                                  {'\u00a0'.repeat((folder.depth ?? 0) * 2)}
                                  {folder.name}
                                </span>
                              </label>
                            ))}
                          </div>
                        )}
                        {scopeMode === 'documents' && (
                          <div className="mt-2 border-t border-border-button pt-2">
                            {documentNodes.map((document) => (
                              <label
                                key={document.id}
                                className="flex items-center gap-2 rounded px-2 py-2"
                              >
                                <input
                                  type="checkbox"
                                  checked={selectedDocumentIds.includes(
                                    document.id,
                                  )}
                                  onChange={() =>
                                    toggle(document.id, setSelectedDocumentIds)
                                  }
                                />
                                <span className="truncate">
                                  {document.relative_path || document.name}
                                </span>
                              </label>
                            ))}
                          </div>
                        )}
                        {explicitScopeEmpty && (
                          <p
                            role="alert"
                            className="mt-2 text-sm text-state-warning"
                          >
                            하나 이상 선택하세요.
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                  <button
                    type="submit"
                    form="docmind-search"
                    disabled={
                      !query.trim() ||
                      explicitScopeEmpty ||
                      retrieval.isPending ||
                      folderCatalog.isError ||
                      folderCatalog.isPending
                    }
                    className="grid size-11 place-items-center rounded-lg bg-accent-primary text-primary-foreground disabled:opacity-40"
                    aria-label="검색 실행"
                  >
                    <Search className="size-5" />
                  </button>
                </div>
                {retrieval.isError && (
                  <p role="alert" className="mt-4 text-state-error">
                    {retrieval.error.message}
                  </p>
                )}
                {retrieval.isPending && (
                  <p role="status" className="mt-8 text-text-secondary">
                    검색 후보를 모으고 재정렬하는 중입니다.
                  </p>
                )}
                {retrieval.data && !retrieval.isPending && (
                  <section className="mt-6" aria-label="검색 결과">
                    <p className="text-sm text-text-secondary">
                      결과 {searchChunks.length}개 · 후보{' '}
                      {retrieval.data.candidate_count ?? 0}개
                    </p>
                    {searchChunks.length === 0 ? (
                      <p className="py-12 text-center text-text-secondary">
                        선택한 범위에서 관련 문서를 찾지 못했습니다.
                      </p>
                    ) : (
                      <>
                        {searchChunks
                          .slice(0, visibleCount)
                          .map((chunk, index) => (
                            <RankedResult
                              key={`${chunk.doc_id}:${chunk.chunk_id ?? index}`}
                              chunk={chunk}
                              rank={index + 1}
                              datasetId={retrieval.data?.dataset_id}
                              onOpen={() => setSelectedChunk(chunk)}
                            />
                          ))}
                        {searchChunks.length > visibleCount && (
                          <button
                            type="button"
                            onClick={() =>
                              setVisibleCount(
                                (count) => count + RESULTS_PER_PAGE,
                              )
                            }
                            className="mt-5 rounded-lg border border-border-button px-4 py-2"
                          >
                            결과 더 보기
                          </button>
                        )}
                      </>
                    )}
                  </section>
                )}
              </div>
            </div>
          )}
          {workspaceView === 'library' && canAdminister && (
            <div className="min-h-0 flex-1 overflow-y-auto">
              <div className="mx-auto w-full max-w-6xl px-5 py-8">
                <h2 className="text-2xl font-semibold">자료 관리</h2>
                <p className="mt-1 text-sm text-text-secondary">
                  인덱싱 완료 후 별도 발행 없이 검색됩니다.
                </p>
                <button
                  type="button"
                  onClick={refreshLibrary}
                  className="mt-3 rounded-lg border border-border-button px-3 py-2 text-sm"
                >
                  자료 목록 새로고침
                </button>
                {folderCatalog.data?.can_upload === true ? (
                  <form
                    onSubmit={submitRegistration}
                    className="mt-6 grid gap-3 rounded-xl border border-border-button p-4 md:grid-cols-[14rem_minmax(0,1fr)_auto]"
                  >
                    <select
                      value={registrationFolderId}
                      onChange={(event) =>
                        setRegistrationFolderId(event.target.value)
                      }
                      aria-label="등록할 폴더"
                      className="h-10 rounded-md border border-border-button bg-bg-input px-3"
                    >
                      {folders.map((folder) => (
                        <option key={folder.id} value={folder.id}>
                          {folder.name}
                        </option>
                      ))}
                    </select>
                    <input
                      type="file"
                      multiple
                      onChange={(event) =>
                        setRegistrationFiles(
                          Array.from(event.target.files ?? []),
                        )
                      }
                      aria-label="등록할 문서"
                      className="h-10 rounded-md border border-border-button px-3 py-2 text-sm"
                    />
                    <button
                      type="submit"
                      disabled={
                        !registrationFolderId ||
                        !registrationFiles.length ||
                        registrationUpload.isPending
                      }
                      className="rounded-lg bg-accent-primary px-4 py-2 text-primary-foreground disabled:opacity-40"
                    >
                      {registrationUpload.isPending ? '등록 중' : '문서 등록'}
                    </button>
                  </form>
                ) : (
                  <p className="mt-4 text-sm text-text-secondary">
                    문서 추가·수정은 All-in-One에서 진행하세요. 연결된 원본의
                    처리 상태는 아래에서 확인할 수 있습니다.
                  </p>
                )}
                {registrationUpload.isError && (
                  <p role="alert" className="mt-3 text-state-error">
                    {registrationUpload.error.message}
                  </p>
                )}
                <section className="mt-6 rounded-xl border border-border-button p-4">
                  <h3 className="mb-3 font-semibold">문서 라이브러리</h3>
                  {hierarchyQuery.isError ? (
                    <p role="alert" className="text-state-error">
                      문서 목록을 불러오지 못했습니다. 자료 목록 새로고침으로
                      다시 시도하세요.
                    </p>
                  ) : hierarchyQuery.isPending ? (
                    <p role="status">문서 목록을 불러오는 중입니다.</p>
                  ) : (
                    <SourceTree
                      nodes={hierarchyQuery.data?.nodes ?? []}
                      renderDocumentAction={(node: DocMindHierarchyNode) => (
                        <InspectionLink
                          datasetId={hierarchyQuery.data?.dataset_id}
                          documentId={node.document_id}
                          compact
                        />
                      )}
                    />
                  )}
                </section>
                {registrationQuery.isError ? (
                  <p role="alert" className="mt-3 text-state-error">
                    문서 처리 현황을 불러오지 못했습니다. 자료 목록 새로고침으로
                    다시 시도하세요.
                  </p>
                ) : (
                  <RegistrationPanel
                    registrations={registrationQuery.data?.registrations ?? []}
                    folderNames={folderNames}
                    documentFolderNames={documentFolderNames}
                    retryPending={registrationRetry.isPending}
                    onRetry={(id: string) => registrationRetry.mutate(id)}
                    renderInspection={(registration: DocMindRegistration) => (
                      <InspectionLink
                        datasetId={registrationQuery.data?.dataset_id}
                        documentId={registration.document_id}
                        compact
                      />
                    )}
                  />
                )}
              </div>
            </div>
          )}
        </main>
        <DocumentModal
          chunk={selectedChunk}
          datasetId={retrieval.data?.dataset_id}
          onClose={() => setSelectedChunk(undefined)}
        />
      </WorkspaceShell>
    </InspectionNavigation.Provider>
  );
}
