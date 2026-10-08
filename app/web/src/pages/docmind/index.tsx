import { FileIcon } from '@/components/icon-font';
import { Input } from '@/components/originui/input';
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
  requestDocMindPdfOcr,
  retryDocMindRegistration,
  searchDocMind,
} from '@/services/docmind-service';
import {
  buildDocMindDocumentPath,
  parseDocMindReturnView,
  parseDocMindView,
  type DocMindSection,
} from '@/utils/docmind-workspace';
import { getSourceLocations } from '@/utils/source-location';
import { useMutation, useQuery } from '@tanstack/react-query';
import { ChevronDown, Search, X } from 'lucide-react';
import * as React from 'react';
import { type FormEvent, useEffect, useMemo, useState } from 'react';
import { ErrorBoundary } from 'react-error-boundary';
import { useSearchParams } from 'react-router';
import RegistrationPanel from './registration-panel';
import AnswerPanel from './answer-panel';
import { PdfOcrChoice } from './pdf-ocr-choice';
import SourceTree from './source-tree';
import {
  SourceDocumentStatus,
  SourceHierarchyKeys,
} from './source-document-status';
import { useFolderSelection } from './use-folder-selection';
import { ChunkInspection } from './workspace-views';
import WorkspaceShell from './workspace-shell';
import SearchPreview from './search-preview';
import ScopeFolderPicker from './scope-folder-picker';
import SearchResultContent from './search-result-content';
import './index.less';
import './library.less';

const RESULTS_PER_PAGE = 10;
const DocMindKeys = {
  folders: () => ['docmind-folders'] as const,
  hierarchy: SourceHierarchyKeys.hierarchy,
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

function RankedResult({
  chunk,
  rank,
  datasetId,
  selected,
  onOpen,
}: {
  chunk: ITestingChunk;
  rank: number;
  datasetId?: string;
  selected: boolean;
  onOpen: (chunk: ITestingChunk, origin: HTMLButtonElement) => void;
}) {
  const name = chunk.docnm_kwd || chunk.doc_name;
  const location = getSourceLocations(chunk);
  const relativePath = chunk.document_relative_path?.trim().replace(/\\/g, '/');
  const open = (event: React.MouseEvent<HTMLButtonElement>) =>
    onOpen(chunk, event.currentTarget);
  return (
    <article className="docmind-result" data-selected={selected || undefined}>
      <button
        type="button"
        onClick={open}
        className="docmind-result-open"
        aria-label={`${name} ${location} 원문 열기`}
        aria-expanded={selected}
        aria-controls={selected ? 'docmind-original-panel' : undefined}
      >
        <span className="docmind-result-title">
          <FileIcon name={name} />
          <span>{name}</span>
        </span>
        <span className="docmind-result-meta">
          <span className="docmind-result-rank">{rank}</span>
          {relativePath && <span>{relativePath}</span>}
          {location !== '문서 위치' && <span>{location}</span>}
        </span>
      </button>
      <SearchResultContent content={chunk.content_with_weight} />
      <div className="docmind-result-actions">
        <button type="button" onClick={open}>
          원문 보기
        </button>
        <InspectionLink
          datasetId={datasetId}
          documentId={chunk.doc_id}
          compact
        />
        {selected && (
          <span className="docmind-result-selected">원문 확인 중</span>
        )}
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
  const [searchMode, setSearchMode] = useState<'search' | 'question'>('search');
  const answerPendingRef = React.useRef(false);
  const [answerPending, setAnswerPending] = useState(false);
  const [selectedChunk, setSelectedChunk] = useState<ITestingChunk>();
  const [refreshingPreview, setRefreshingPreview] = useState(false);
  const previewOrigin = React.useRef<HTMLButtonElement | null>(null);
  const [visibleCount, setVisibleCount] = useState(RESULTS_PER_PAGE);
  const [scopeMode, setScopeMode] = useState<DocMindSearchScope['mode']>('all');
  const [selectedFolderIds, setSelectedFolderIds] = useState<string[]>([]);
  const [selectedDocumentIds, setSelectedDocumentIds] = useState<string[]>([]);
  const [scopeMenuOpen, setScopeMenuOpen] = useState(false);
  const scopeTriggerRef = React.useRef<HTMLButtonElement>(null);
  const scopeDialogRef = React.useRef<HTMLDivElement>(null);
  const toggleScopeMenu = () => setScopeMenuOpen((open) => !open);
  const closeScopeMenu = () => {
    setScopeMenuOpen(false);
    scopeTriggerRef.current?.focus();
  };
  const handleScopeKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeScopeMenu();
    }
  };
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
    onSuccess: () => setRefreshingPreview(false),
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
  const pdfOcrRequest = useMutation<
    unknown,
    Error,
    { documentId: string; versionId: string; key: string }
  >({
    mutationKey: ['docmind-pdf-ocr-request'],
    mutationFn: async ({ documentId, versionId, key }) => {
      const { data } = await requestDocMindPdfOcr(documentId, versionId, key);
      if (data.code !== 0) throw new Error(data.message || 'PDF OCR 요청 실패');
      return data.data;
    },
    onSuccess: async () => hierarchyQuery.refetch(),
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
  const renderAnswerInspection = (datasetId: string, documentId: string) => (
    <InspectionLink datasetId={datasetId} documentId={documentId} />
  );
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
  const currentScope: DocMindSearchScope =
    scopeMode === 'folders'
      ? { mode: scopeMode, folderIds: canonicalFolderIds }
      : scopeMode === 'documents'
        ? { mode: scopeMode, documentIds: canonicalDocumentIds }
        : { mode: 'all' };
  const returnedChunks =
    retrieval.data?.ranked_chunks ?? retrieval.data?.chunks ?? [];
  const availableDocumentIds = new Set(
    documentNodes.map((document) => document.id),
  );
  const searchContextValid = Boolean(
    retrieval.variables &&
    !explicitScopeEmpty &&
    !folderCatalog.isError &&
    !folderCatalog.isPending &&
    retrieval.variables.projectId === folderCatalog.data?.project_id &&
    JSON.stringify(retrieval.variables.scope) === JSON.stringify(currentScope),
  );
  const searchResultValid = Boolean(
    retrieval.data &&
    searchContextValid &&
    returnedChunks.every((chunk) => availableDocumentIds.has(chunk.doc_id)),
  );

  const searchChunks = searchResultValid ? returnedChunks : [];
  const currentSelectedChunk =
    (selectedChunk &&
      searchChunks.find(
        (chunk) =>
          chunk.doc_id === selectedChunk.doc_id &&
          chunk.source_version_id === selectedChunk.source_version_id &&
          chunk.chunk_id === selectedChunk.chunk_id,
      )) ||
    (selectedChunk &&
      searchChunks.find(
        (chunk) =>
          chunk.doc_id === selectedChunk.doc_id &&
          chunk.source_version_id === selectedChunk.source_version_id,
      ));
  const previewChunk =
    workspaceView === 'search' &&
    searchMode === 'search' &&
    searchContextValid &&
    selectedChunk &&
    availableDocumentIds.has(selectedChunk.doc_id)
      ? (retrieval.isPending || retrieval.isError) && refreshingPreview
        ? selectedChunk
        : currentSelectedChunk
      : undefined;
  useEffect(() => {
    // The catalog can invalidate the selection after an asynchronous refresh.
    // Clear it so restoring an earlier scope cannot revive a released preview.
    // oxlint-disable-next-line react/set-state-in-effect
    if (selectedChunk && !previewChunk) setSelectedChunk(undefined);
  }, [selectedChunk, previewChunk]);
  const openPreview = (chunk: ITestingChunk, origin: HTMLButtonElement) => {
    previewOrigin.current = origin;
    setSelectedChunk(chunk);
  };
  const closePreview = () => {
    setSelectedChunk(undefined);
    const origin = previewOrigin.current;
    window.requestAnimationFrame(() => {
      if (origin?.isConnected) origin.focus({ preventScroll: true });
      else
        document
          .querySelector<HTMLInputElement>('.docmind-search-input')
          ?.focus();
    });
  };
  const showMoreResults = () =>
    setVisibleCount((count) => count + RESULTS_PER_PAGE);
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
      searchMode !== 'search' ||
      retrieval.isPending ||
      explicitScopeEmpty ||
      folderCatalog.isError ||
      folderCatalog.isPending
    )
      return;
    setSelectedChunk(undefined);
    setRefreshingPreview(false);
    retrieval.mutate({
      question: query.trim(),
      projectId: folderCatalog.data?.project_id,
      scope: currentScope,
    });
  };
  const showSearch = () => setSearchMode('search');
  const showQuestion = () => setSearchMode('question');
  const handleModeKey = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next =
      event.key === 'Home'
        ? 'search'
        : event.key === 'End'
          ? 'question'
          : searchMode === 'search'
            ? 'question'
            : 'search';
    setSearchMode(next);
    document.getElementById(`docmind-${next}-tab`)?.focus();
  };
  const changeQuery = (event: React.ChangeEvent<HTMLInputElement>) => {
    setQuery(event.target.value);
    setSelectedChunk(undefined);
  };
  const clearQuery = () => {
    setQuery('');
    setSelectedChunk(undefined);
  };
  const searchStart = searchMode === 'search' && retrieval.isIdle;
  React.useLayoutEffect(() => {
    if (!scopeMenuOpen) return;
    const fitScopeDialog = () => {
      const dialog = scopeDialogRef.current;
      if (!dialog) return;
      const trigger = scopeTriggerRef.current;
      if (!trigger) return;
      const bounds = trigger.getBoundingClientRect();
      const mobile = window.innerWidth < 768;
      const below = Math.max(0, window.innerHeight - bounds.bottom - 24);
      const above = Math.max(0, bounds.top - 24);
      const placement =
        !mobile && below < Math.min(320, above) ? 'above' : 'below';
      dialog.dataset.placement = placement;
      const available = mobile
        ? window.innerHeight - 80
        : placement === 'above'
          ? above
          : below;
      const height = `min(32rem, ${Math.max(0, available)}px)`;
      if (dialog.style.maxHeight !== height) dialog.style.maxHeight = height;
    };
    fitScopeDialog();
    // The AI composer moves between the entry screen and the bottom of an answer.
    const layout = scopeTriggerRef.current?.closest('.docmind-answer-panel');
    const observer =
      typeof ResizeObserver === 'undefined'
        ? undefined
        : new ResizeObserver(fitScopeDialog);
    if (layout) observer?.observe(layout);
    window.addEventListener('resize', fitScopeDialog);
    window.addEventListener('scroll', fitScopeDialog, true);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', fitScopeDialog);
      window.removeEventListener('scroll', fitScopeDialog, true);
    };
  }, [scopeMenuOpen, searchStart, searchMode]);
  const questionInput = (
    <div className="relative min-w-0 flex-1">
      <Input
        value={query}
        onChange={changeQuery}
        placeholder="문서에 대해 질문하세요"
        aria-label="문서 검색 질문"
        className="docmind-question-input h-11 pr-10 text-base"
      />
      {query && (
        <button
          type="button"
          onClick={clearQuery}
          className="absolute right-3 top-1/2 -translate-y-1/2"
          aria-label="검색어 지우기"
        >
          <X className="size-5" />
        </button>
      )}
    </div>
  );
  const refreshSearchAfterChunkChange = () => {
    if (retrieval.variables) {
      setRefreshingPreview(true);
      retrieval.mutate(retrieval.variables);
    }
  };
  const toggle = (
    id: string,
    setter: React.Dispatch<React.SetStateAction<string[]>>,
  ) =>
    setter((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id],
    );
  const toggleScopeFolder = (id: string) => toggle(id, setSelectedFolderIds);
  const changeScopeMode = (event: React.ChangeEvent<HTMLInputElement>) =>
    setScopeMode(event.target.value as DocMindSearchScope['mode']);
  const submitRegistration = (event: FormEvent) => {
    event.preventDefault();
    if (!registrationFolderId || !registrationFiles.length) return;
    registrationUpload.mutate({
      folderId: registrationFolderId,
      files: registrationFiles,
    });
  };

  const scopeControl = (
    <div className="docmind-scope relative" onKeyDown={handleScopeKey}>
      <button
        ref={scopeTriggerRef}
        type="button"
        onClick={toggleScopeMenu}
        className="docmind-scope-trigger flex h-11 min-w-40 items-center justify-between gap-2 rounded-lg border border-border-button bg-bg-card px-4"
        aria-expanded={scopeMenuOpen}
        aria-haspopup="dialog"
        aria-controls={scopeMenuOpen ? 'docmind-scope-dialog' : undefined}
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
          ref={scopeDialogRef}
          id="docmind-scope-dialog"
          className="docmind-scope-dialog absolute right-0 top-12 z-30 overflow-y-auto border border-border-button bg-bg-component"
          role="dialog"
          aria-label="검색 범위 선택"
        >
          <button
            type="button"
            onClick={closeScopeMenu}
            aria-label="검색 범위 닫기"
            className="docmind-scope-close"
          >
            <X className="size-4" aria-hidden="true" />
          </button>
          <fieldset className="docmind-scope-modes">
            <legend className="mb-3 text-sm font-semibold">검색 범위</legend>
            {(['all', 'folders', 'documents'] as const).map((mode) => (
              <label key={mode} className="docmind-scope-mode">
                <input
                  type="radio"
                  name="docmind-scope"
                  checked={scopeMode === mode}
                  value={mode}
                  aria-label={
                    mode === 'all'
                      ? '전체 문서'
                      : mode === 'folders'
                        ? '선택 폴더와 하위 폴더'
                        : '선택 문서'
                  }
                  onChange={changeScopeMode}
                />
                {mode === 'all' ? '전체' : mode === 'folders' ? '폴더' : '문서'}
              </label>
            ))}
          </fieldset>
          {scopeMode === 'folders' && (
            <ScopeFolderPicker
              folders={folders}
              selectedIds={canonicalFolderIds}
              onToggle={toggleScopeFolder}
            />
          )}
          {scopeMode === 'all' && (
            <p className="docmind-scope-hint">
              검색 가능한 모든 문서에서 찾습니다.
            </p>
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
                    checked={selectedDocumentIds.includes(document.id)}
                    onChange={() => toggle(document.id, setSelectedDocumentIds)}
                  />
                  <span className="truncate">
                    {document.relative_path || document.name}
                  </span>
                </label>
              ))}
            </div>
          )}
          {explicitScopeEmpty && (
            <p role="alert" className="mt-2 text-sm text-state-warning">
              하나 이상 선택하세요.
            </p>
          )}
        </div>
      )}
    </div>
  );

  return (
    <InspectionNavigation.Provider
      value={{ view: activeSection, open: openInspection }}
    >
      <WorkspaceShell
        activeSection={activeSection}
        canAdminister={canAdminister}
        sharedWorkspace={folderCatalog.data?.workspace_mode === 'shared'}
      >
        <main
          className={`docmind-main flex h-full min-h-0 flex-col${previewChunk ? ' docmind-has-preview' : ''}`}
        >
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
            <div
              className="flex min-h-0 flex-1 flex-col"
              aria-label="문서 상세"
            >
              {validInspection &&
                inspectionDocumentId &&
                canAdminister &&
                folderCatalog.data?.source_sync && (
                  <SourceDocumentStatus documentId={inspectionDocumentId} />
                )}
              {validInspection ? (
                <div className="min-h-0 flex-1">
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
                </div>
              ) : (
                <p role="alert" className="p-6">
                  문서 주소가 올바르지 않습니다.
                </p>
              )}
            </div>
          )}
          {workspaceView === 'search' && (
            <div
              data-docmind-search-scroll
              role="region"
              aria-label="검색 작업 영역"
              tabIndex={0}
              className="min-h-0 flex-1 overflow-y-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-primary"
            >
              <div
                className={`docmind-search-layout mx-auto w-full max-w-6xl px-5 py-8${searchStart ? ' docmind-search-start' : searchMode === 'search' ? ' docmind-search-results' : ' docmind-question-layout'}`}
              >
                {(searchStart || searchMode === 'question') && (
                  <header className="docmind-search-intro">
                    <h1>
                      {searchMode === 'search'
                        ? '필요한 문서를 찾아보세요'
                        : '문서에 질문하세요'}
                    </h1>
                    <p>
                      {searchMode === 'search'
                        ? '검색 범위를 선택하고, 문서 속 내용을 찾아보세요.'
                        : '검색 범위를 선택하고, 문서의 근거를 바탕으로 답변을 확인하세요.'}
                    </p>
                  </header>
                )}
                <div
                  role="tablist"
                  aria-label="검색 방식"
                  className="docmind-search-tabs mb-5 flex gap-2"
                >
                  <button
                    type="button"
                    role="tab"
                    id="docmind-search-tab"
                    aria-controls="docmind-search-panel"
                    aria-selected={searchMode === 'search'}
                    tabIndex={searchMode === 'search' ? 0 : -1}
                    onClick={showSearch}
                    onKeyDown={handleModeKey}
                    className={`rounded-lg border border-border-button px-4 py-2 ${searchMode === 'search' ? 'bg-bg-card font-semibold text-accent-primary' : 'text-text-secondary'}`}
                  >
                    문서 검색
                  </button>
                  <button
                    type="button"
                    role="tab"
                    id="docmind-question-tab"
                    aria-controls="docmind-question-panel"
                    aria-selected={searchMode === 'question'}
                    tabIndex={searchMode === 'question' ? 0 : -1}
                    onClick={showQuestion}
                    onKeyDown={handleModeKey}
                    className={`rounded-lg border border-border-button px-4 py-2 ${searchMode === 'question' ? 'bg-bg-card font-semibold text-accent-primary' : 'text-text-secondary'}`}
                  >
                    AI 질문
                  </button>
                </div>
                <div
                  role="tabpanel"
                  id={`docmind-${searchMode}-panel`}
                  aria-labelledby={`docmind-${searchMode}-tab`}
                >
                  <p className="text-sm text-text-secondary" hidden>
                    {searchMode === 'search'
                      ? '전체 · 폴더 · 문서 범위를 직접 선택해 근거 청크를 검색합니다.'
                      : '질문을 입력하면 선택한 범위에서 근거를 찾아 AI가 답변합니다.'}
                  </p>
                  <div
                    hidden={searchMode !== 'search'}
                    className={
                      searchMode === 'search'
                        ? 'docmind-search-controls mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto]'
                        : 'mt-3 flex justify-end'
                    }
                  >
                    {searchMode === 'search' && (
                      <form
                        id="docmind-search"
                        onSubmit={submitSearch}
                        className="docmind-search-form relative"
                      >
                        <Input
                          value={query}
                          onChange={changeQuery}
                          form="docmind-search"
                          placeholder="문서에서 찾을 내용을 입력하세요"
                          aria-label="문서 검색 질문"
                          className="docmind-search-input h-11 pr-12 text-base"
                        />
                        {query && (
                          <button
                            type="button"
                            onClick={clearQuery}
                            className="absolute right-4 top-1/2 -translate-y-1/2"
                            aria-label="검색어 지우기"
                          >
                            <X className="size-5" />
                          </button>
                        )}
                      </form>
                    )}
                    {searchMode === 'search' && scopeControl}
                    {searchMode === 'search' && (
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
                        className="docmind-search-submit flex h-11 items-center justify-center gap-2 rounded-lg bg-accent-primary px-4 text-primary-foreground disabled:opacity-40"
                        aria-label="검색 실행"
                      >
                        <Search className="size-4" aria-hidden="true" />
                        <span>검색</span>
                      </button>
                    )}
                  </div>
                  {(searchStart || searchMode === 'question') &&
                    folderCatalog.isPending && (
                      <p
                        role="status"
                        className="mt-4 text-sm text-text-secondary"
                      >
                        검색 범위를 불러오는 중입니다.
                      </p>
                    )}
                  {searchMode === 'question' && (
                    <AnswerPanel
                      invalidationKey={JSON.stringify([
                        documentNodes,
                        folderCatalog.isError,
                        folderCatalog.isPending,
                      ])}
                      request={{
                        question: query.trim(),
                        projectId: folderCatalog.data?.project_id,
                        scope: currentScope,
                      }}
                      disabled={
                        explicitScopeEmpty ||
                        folderCatalog.isError ||
                        folderCatalog.isPending
                      }
                      pending={answerPending}
                      pendingRef={answerPendingRef}
                      onPendingChange={setAnswerPending}
                      composer={questionInput}
                      scopeControl={scopeControl}
                      renderInspection={renderAnswerInspection}
                    />
                  )}
                  {searchMode === 'search' && retrieval.isError && (
                    <div
                      role="alert"
                      className="docmind-search-state docmind-search-error"
                    >
                      <h2>검색을 완료하지 못했습니다</h2>
                      <p>{retrieval.error.message}</p>
                      <p>연결 상태를 확인한 뒤 다시 검색해 주세요.</p>
                    </div>
                  )}
                  {searchMode === 'search' && retrieval.isPending && (
                    <div
                      role="status"
                      className="docmind-search-state"
                      aria-live="polite"
                    >
                      <span
                        className="docmind-search-pulse"
                        aria-hidden="true"
                      />
                      <h2>문서에서 관련 내용을 찾고 있습니다</h2>
                      <p>선택한 검색 범위를 확인하고 있습니다.</p>
                    </div>
                  )}
                  {searchMode === 'search' &&
                    retrieval.data &&
                    !retrieval.isPending &&
                    !searchResultValid && (
                      <p role="status" className="docmind-search-state">
                        검색 범위가 변경되었습니다. 다시 검색해 주세요.
                      </p>
                    )}
                  {searchMode === 'search' &&
                    retrieval.data &&
                    !retrieval.isPending &&
                    searchResultValid && (
                      <section
                        className="docmind-results"
                        aria-label="검색 결과"
                      >
                        <p className="docmind-results-count" role="status">
                          결과 {searchChunks.length}개
                          {typeof retrieval.data.candidate_count ===
                            'number' && (
                            <span>
                              {' '}
                              · 검색 후보 {retrieval.data.candidate_count}개
                            </span>
                          )}
                        </p>
                        {searchChunks.length === 0 ? (
                          <div className="docmind-search-state">
                            <h2>
                              선택한 범위에서 관련 문서를 찾지 못했습니다.
                            </h2>
                            <p>
                              다른 검색어를 입력하거나 검색 범위를 넓혀보세요.
                            </p>
                          </div>
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
                                  selected={previewChunk === chunk}
                                  onOpen={openPreview}
                                />
                              ))}
                            {searchChunks.length > visibleCount && (
                              <button
                                type="button"
                                onClick={showMoreResults}
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
            </div>
          )}
          {workspaceView === 'library' && canAdminister && (
            <div
              data-docmind-library-scroll
              className="min-h-0 flex-1 overflow-y-auto"
            >
              <div className="docmind-library-layout">
                <header className="docmind-library-heading">
                  <div>
                    <h2>자료 관리</h2>
                    <p>연결된 문서를 둘러보고 검색 준비 상태를 확인하세요.</p>
                  </div>
                  <button type="button" onClick={refreshLibrary}>
                    자료 목록 새로고침
                  </button>
                </header>
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
                  <p className="docmind-library-origin">
                    문서 추가·수정은 All-in-One에서 진행하세요. 연결된 원본의
                    처리 상태는 이 화면에서 확인할 수 있습니다.
                  </p>
                )}
                {registrationUpload.isError && (
                  <p role="alert" className="mt-3 text-state-error">
                    {registrationUpload.error.message}
                  </p>
                )}
                <div className="docmind-library-columns">
                  <section className="docmind-library-documents">
                    <h3>문서 라이브러리</h3>
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
                          <span className="flex items-center gap-2">
                            <InspectionLink
                              datasetId={hierarchyQuery.data?.dataset_id}
                              documentId={node.document_id}
                              compact
                            />
                            <PdfOcrChoice
                              node={node}
                              onConfirm={async (documentId, versionId, key) => {
                                await pdfOcrRequest.mutateAsync({
                                  documentId,
                                  versionId,
                                  key,
                                });
                              }}
                            />
                          </span>
                        )}
                      />
                    )}
                  </section>
                  <aside
                    className="docmind-library-processing"
                    aria-label="처리 현황 영역"
                  >
                    {registrationQuery.isError ? (
                      <p role="alert" className="mt-3 text-state-error">
                        문서 처리 현황을 불러오지 못했습니다. 자료 목록
                        새로고침으로 다시 시도하세요.
                      </p>
                    ) : registrationQuery.isPending ? (
                      <p role="status">문서 처리 현황을 불러오는 중입니다.</p>
                    ) : (
                      <RegistrationPanel
                        registrations={
                          registrationQuery.data?.registrations ?? []
                        }
                        folderNames={folderNames}
                        documentFolderNames={documentFolderNames}
                        retryPending={registrationRetry.isPending}
                        onRetry={(id: string) => registrationRetry.mutate(id)}
                        renderInspection={(
                          registration: DocMindRegistration,
                        ) => (
                          <InspectionLink
                            datasetId={registrationQuery.data?.dataset_id}
                            documentId={registration.document_id}
                            compact
                          />
                        )}
                      />
                    )}
                  </aside>
                </div>
              </div>
            </div>
          )}
        </main>
        <SearchPreview
          chunk={previewChunk}
          evidenceState={
            refreshingPreview
              ? retrieval.isError
                ? 'error'
                : 'pending'
              : 'ready'
          }
          inspection={
            <InspectionLink
              datasetId={retrieval.data?.dataset_id}
              documentId={previewChunk?.doc_id}
              compact
            />
          }
          onClose={closePreview}
          onChunkSetChanged={refreshSearchAfterChunkChange}
        />
      </WorkspaceShell>
    </InspectionNavigation.Provider>
  );
}
