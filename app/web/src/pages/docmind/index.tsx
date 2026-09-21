import DocumentPreview from '@/components/document-preview';
import HighLightMarkdown from '@/components/highlight-markdown';
import { FileIcon } from '@/components/icon-font';
import { Input } from '@/components/originui/input';
import { Modal } from '@/components/ui/modal/modal';
import {
  useGetChunkHighlights,
  useGetDocumentUrl,
} from '@/hooks/use-document-request';
import { ITestingChunk } from '@/interfaces/database/dataset';
import type {
  DocMindCatalogVersion,
  DocMindCatalogVersionList,
  DocMindDraft,
  DocMindDraftChangeRequest,
  DocMindDraftList,
  DocMindFolderCatalog,
  DocMindHierarchy,
  DocMindHierarchyNode,
  DocMindImportJob,
  DocMindManualCardRevisionRequest,
  DocMindManualCardRevisionResponse,
  DocMindManualRoutingCard,
  DocMindRegistration,
  DocMindRegistrationList,
  DocMindSearchRequest,
  DocMindSearchResult,
} from '@/services/docmind-service';
import {
  bootstrapDocMindWorkspace,
  captureDocMindHierarchyDraft,
  changeDocMindDraft,
  createDocMindDraft,
  createDocMindManualCardRevision,
  deleteDocMindCatalogVersion,
  generateDocMindDraft,
  getDocMindCatalogVersions,
  getDocMindDraft,
  getDocMindDrafts,
  getDocMindFolders,
  getDocMindHierarchy,
  getDocMindImportJobs,
  getDocMindRegistrations,
  importDocMindLocalFolder,
  publishDocMindCatalogVersion,
  registerDocMindDocuments,
  retryDocMindLocalFolderImport,
  retryDocMindRegistration,
  rollbackDocMindCatalogVersion,
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
import { FormEvent, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router';
import { ErrorBoundary } from 'react-error-boundary';
import WorkspaceShell from './workspace-shell';
import RegistrationPanel from './registration-panel';
import SourceTree from './source-tree';
import FolderEditor from './folder-editor';
import { useFolderSelection } from './use-folder-selection';
import { ChunkInspection, WorkspaceSettings } from './workspace-views';
import './index.less';

const InspectionNavigation = React.createContext<{
  view: DocMindSection;
  open: (datasetId: string, documentId: string) => void;
} | null>(null);

const DocMindKeys = {
  folders: () => ['docmind-folders'] as const,
  hierarchy: () => ['docmind-hierarchy'] as const,
  imports: () => ['docmind-hierarchy-imports'] as const,
  registrations: () => ['docmind-registrations'] as const,
  drafts: () => ['docmind-drafts'] as const,
  versions: () => ['docmind-catalog-versions'] as const,
  preview: (id?: string) => ['docmind-draft-preview', id] as const,
};

function WorkspaceViewError() {
  return (
    <p role="alert" className="p-6 text-text-secondary">
      화면을 불러오지 못했습니다. 메뉴에서 다른 화면으로 이동한 후 다시 시도해
      주세요.
    </p>
  );
}

const draftStateLabel = {
  DRAFT: '변경 작성 중',
  GENERATING: 'routing digest와 L0/L1 생성 중',
  GENERATED: 'L0/L1 생성 완료',
  VALIDATING: '검색 검증 중',
  READY: '검증 통과',
  FAILED: '생성 또는 검증 실패',
} as const;

const draftChangeErrorMessage: Record<string, string> = {
  DOCMIND_DRAFT_DOCUMENT_ALREADY_IN_PARENT:
    '이 문서는 현재 Catalog에 이미 포함되어 있어 다시 추가할 필요가 없습니다.',
  DOCMIND_DRAFT_ADD_DUPLICATE: '이 문서는 현재 초안에 이미 포함되어 있습니다.',
  DOCMIND_DRAFT_PARENT_MEMBERSHIP_CONFLICT:
    '초안의 기준 Catalog가 변경되었습니다. 초안을 새로고침한 후 다시 확인하세요.',
};

const generationErrorMessage: Record<string, string> = {
  DOCMIND_OPENVIKING_SIDECAR_GENERATION_FAILED:
    'OpenViking이 L0/L1을 생성하지 못했습니다. OpenViking 모델 키와 사용 한도를 확인하세요.',
  DOCMIND_OPENVIKING_SIDECAR_LANGUAGE_INVALID:
    'L0/L1이 한국어 작성 규칙을 충족하지 못했습니다. 초안을 다시 생성하세요.',
  DOCMIND_ROUTER_REGRESSION:
    '새 L0/L1이 폴더 라우팅 정확도 기준을 통과하지 못했습니다.',
};

const manualCardRevisionErrorMessage: Record<string, string> = {
  DOCMIND_MANUAL_CARD_SOURCE_INVALID:
    '수정할 수 있는 READY 후보가 아닙니다. 후보를 다시 선택하세요.',
  DOCMIND_MANUAL_CARD_SOURCE_STALE:
    '기준 Catalog가 변경되었습니다. 목록을 새로고침한 후 다시 확인하세요.',
  DOCMIND_MANUAL_CARD_SCHEMA_INVALID:
    '5개 폴더의 L0/L1 작성 규칙을 확인하세요.',
  DOCMIND_MANUAL_CARD_LANGUAGE_INVALID:
    '각 L0/L1 설명에는 한국어가 한 글자 이상 포함되어야 합니다.',
  DOCMIND_MANUAL_CARD_ROOT_UNSUPPORTED:
    'OpenViking이 관리자 작성본 저장을 지원하지 않습니다.',
  DOCMIND_MANUAL_CARD_ROOT_DRIFT:
    '저장된 OpenViking 설명이 입력 내용과 일치하지 않습니다.',
  DOCMIND_MANUAL_CARD_CONFLICT:
    '같은 후보의 다른 수정이 먼저 저장되었습니다. 목록을 새로고침하세요.',
};

type EditableRoutingCard = DocMindManualRoutingCard & {
  folder_name: string;
};

type AdminIngestionPath = 'folder' | 'documents';

function hasKorean(value: string): boolean {
  return /[\uac00-\ud7a3]/.test(value);
}

function hasForbiddenManualCardValue(value: string): boolean {
  return /viking:\/\/|api[_ -]?key|credential/i.test(value);
}

function isValidManualCardText(value: string, maxLength: number): boolean {
  return (
    value.trim().length > 0 &&
    value.length <= maxLength &&
    hasKorean(value) &&
    !hasForbiddenManualCardValue(value)
  );
}

const getPageNumber = (chunk: ITestingChunk) => {
  const position = chunk.positions?.find(
    (value) => Array.isArray(value) && Number.isFinite(value[0]),
  );
  return position?.[0];
};

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
  const locator = (chunk as any).office_locator;
  if (locator?.kind === 'docx') {
    const path = Array.isArray(locator.heading_path)
      ? locator.heading_path.join(' > ')
      : '';
    return path || locator.item_locator || 'DOCX 구조 위치';
  }
  if (locator?.kind === 'xlsx') {
    return `${locator.sheet}${locator.cell_range ? `!${locator.cell_range}` : ''}`;
  }
  if (locator?.kind === 'pptx') {
    return `슬라이드 ${locator.slide}`;
  }
  const page = getPageNumber(chunk);
  return page ? `p. ${page}` : '문서 위치';
}

function RagFlowInspectionLink({
  datasetId,
  documentId,
  compact = false,
  label,
}: {
  datasetId?: string;
  documentId?: string;
  compact?: boolean;
  label?: string;
}) {
  const navigation = React.useContext(InspectionNavigation);
  const inspectionPath = buildDocMindDocumentPath(
    datasetId,
    documentId,
    navigation?.view,
  );
  if (!inspectionPath) return null;

  const openInspection = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (
      !navigation ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    event.preventDefault();
    navigation.open(datasetId!, documentId!);
  };

  return (
    <a
      href={inspectionPath}
      onClick={openInspection}
      className={
        compact
          ? 'rounded border border-accent-primary/30 px-2 py-1 text-xs text-accent-primary hover:bg-accent-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary'
          : 'rounded-lg border border-accent-primary/30 px-3 py-1.5 text-sm text-accent-primary hover:bg-accent-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary'
      }
    >
      {label ?? (compact ? '문서 상세' : '원문 · 청크 확인')}
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
  const documentName = chunk?.docnm_kwd || chunk?.doc_name || '';
  const fileType = documentName.split('.').pop()?.toLowerCase() || '';
  const hasPositions = highlights.length > 0;

  return (
    <Modal
      title={documentName}
      open={Boolean(chunk)}
      onCancel={onClose}
      showfooter={false}
    >
      {chunk && (
        <div>
          <div className="mb-3 flex justify-end">
            <RagFlowInspectionLink
              datasetId={datasetId}
              documentId={chunk.doc_id}
            />
          </div>
          {fileType === 'pdf' && !hasPositions && (
            <p className="mb-3 rounded-md border border-state-warning/30 bg-state-warning/10 px-3 py-2 text-sm text-state-warning">
              원문 좌표가 없어 자동 강조할 수 없습니다.
            </p>
          )}
          <DocumentPreview
            className="docmind-document-preview !h-[calc(100dvh-300px)] overflow-auto border-none p-0"
            fileType={fileType}
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
  const documentName = chunk.docnm_kwd || chunk.doc_name;
  const location = getChunkLocation(chunk);
  const documentRelativePath = chunk.document_relative_path
    ?.trim()
    .replace(/\\/g, '/')
    .split('/')
    .filter(Boolean)
    .join(' > ');

  return (
    <article className="grid grid-cols-[3.5rem_minmax(0,1fr)] gap-x-5 border-b border-border-button py-6 max-sm:grid-cols-[2.5rem_minmax(0,1fr)]">
      <div className="pt-2 text-3xl font-medium tracking-tight text-text-secondary">
        {rank}
      </div>
      <div className="min-w-0">
        <button
          type="button"
          onClick={onOpen}
          className="block w-full min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
          aria-label={`${documentName} ${location} 원문 열기`}
        >
          <div className="flex max-w-full items-center gap-2 text-sm text-text-secondary hover:text-text-primary">
            <FileIcon name={documentName} />
            <span className="truncate">{documentName}</span>
            <span aria-hidden="true">·</span>
            <span>{location}</span>
          </div>
          {documentRelativePath && (
            <span
              className="mt-1 block truncate text-xs text-text-secondary"
              title={documentRelativePath}
              aria-label={`문서 경로: ${documentRelativePath}`}
            >
              {documentRelativePath}
            </span>
          )}
          <span className="mt-2 block text-xl font-semibold tracking-tight text-accent-primary hover:underline">
            {documentName}
          </span>
          <div className="docmind-snippet mt-2 overflow-hidden text-base leading-7 text-text-primary">
            <HighLightMarkdown>{chunk.content_with_weight}</HighLightMarkdown>
          </div>
        </button>
        <div className="mt-3">
          <RagFlowInspectionLink
            datasetId={datasetId}
            documentId={chunk.doc_id}
          />
        </div>
      </div>
    </article>
  );
}

export default function DocMind() {
  const resultsPerPage = 10;
  const [workspaceParams, setWorkspaceParams] = useSearchParams();
  const workspaceView = parseDocMindView(workspaceParams.get('view'));
  const returnView = parseDocMindReturnView(workspaceParams.get('return'));
  const activeSection =
    workspaceView === 'document' ? returnView : workspaceView;
  const inspectionOrigin = React.useRef<HTMLElement | null>(null);
  const inspectionDatasetId = workspaceParams.get('id') ?? undefined;
  const inspectionDocumentId = workspaceParams.get('doc_id') ?? undefined;
  const validInspection = Boolean(
    buildDocMindDocumentPath(inspectionDatasetId, inspectionDocumentId),
  );
  const [query, setQuery] = useState('');
  const [selectedChunk, setSelectedChunk] = useState<ITestingChunk>();
  const [visibleResultCount, setVisibleResultCount] = useState(resultsPerPage);
  const [selectedFolderIds, setSelectedFolderIds] = useState<string[]>([]);
  const [folderMenuOpen, setFolderMenuOpen] = useState(false);
  const adminOpen = workspaceView === 'library' || workspaceView === 'catalog';
  const [adminIngestionPath, setAdminIngestionPath] =
    useState<AdminIngestionPath>('folder');
  const [showVersionHistory, setShowVersionHistory] = useState(false);
  const [publishCandidateId, setPublishCandidateId] = useState<string>();
  const [editingRoutingCards, setEditingRoutingCards] = useState(false);
  const [routingCardEdits, setRoutingCardEdits] = useState<
    EditableRoutingCard[]
  >([]);
  const manualRevisionAttemptRef = React.useRef<{
    fingerprint: string;
    idempotencyKey: string;
  }>();
  const manualRevisionKeySequenceRef = React.useRef(0);
  const [registrationFolderId, setRegistrationFolderId] = useState('');
  const [registrationFiles, setRegistrationFiles] = useState<File[]>([]);
  const [localFolderFiles, setLocalFolderFiles] = useState<File[]>([]);
  const folderCatalog = useQuery<DocMindFolderCatalog, Error>({
    queryKey: DocMindKeys.folders(),
    queryFn: async () => {
      const { data } = await getDocMindFolders();
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind folder list failed');
      }
      return data.data as DocMindFolderCatalog;
    },
    staleTime: 60_000,
  });
  const retrieval = useMutation<
    DocMindSearchResult,
    Error,
    DocMindSearchRequest
  >({
    mutationKey: ['docmind-search'],
    mutationFn: async (searchRequest) => {
      const { data } = await searchDocMind(searchRequest);
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind search failed');
      }
      return data.data as DocMindSearchResult;
    },
    onMutate: () => setVisibleResultCount(resultsPerPage),
  });
  const bootstrapWorkspace = useMutation<unknown, Error>({
    mutationKey: ['docmind-workspace-bootstrap'],
    mutationFn: async () => {
      const { data } = await bootstrapDocMindWorkspace();
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind workspace bootstrap failed');
      }
      return data.data;
    },
    onSuccess: async () => {
      await folderCatalog.refetch();
    },
  });

  const canAdminister = Boolean(folderCatalog.data?.can_administer);
  const hierarchyQuery = useQuery<DocMindHierarchy, Error>({
    queryKey: DocMindKeys.hierarchy(),
    queryFn: async () => {
      const { data } = await getDocMindHierarchy();
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind hierarchy read failed');
      }
      return data.data as DocMindHierarchy;
    },
    enabled: canAdminister && adminOpen,
  });
  const importJobsQuery = useQuery<{ jobs: DocMindImportJob[] }, Error>({
    queryKey: DocMindKeys.imports(),
    queryFn: async () => {
      const { data } = await getDocMindImportJobs();
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind import history failed');
      }
      return data.data as { jobs: DocMindImportJob[] };
    },
    enabled: canAdminister && adminOpen,
    refetchInterval: canAdminister && adminOpen ? 3_000 : false,
  });
  const localFolderImport = useMutation<DocMindImportJob, Error, File[]>({
    mutationKey: ['docmind-local-folder-import'],
    mutationFn: async (files) => {
      const idempotencyKey =
        globalThis.crypto?.randomUUID?.() ?? `folder-import-${Date.now()}`;
      const { data } = await importDocMindLocalFolder(files, idempotencyKey);
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind folder import failed');
      }
      return data.data as DocMindImportJob;
    },
    onSuccess: async () => {
      setLocalFolderFiles([]);
      await Promise.all([hierarchyQuery.refetch(), importJobsQuery.refetch()]);
    },
  });

  const localFolderRetry = useMutation<
    DocMindImportJob,
    Error,
    { job: DocMindImportJob; files: File[] }
  >({
    mutationKey: ['docmind-local-folder-import-retry'],
    mutationFn: async ({ job, files }) => {
      const failedPaths = new Set(
        job.items
          .filter((item) => item.state === 'FAILED')
          .map((item) => item.relative_path),
      );
      const retryFiles = files.filter((file) =>
        failedPaths.has(file.webkitRelativePath || file.name),
      );
      if (!retryFiles.length) {
        throw new Error(
          '실패한 파일이 포함된 같은 로컬 폴더를 다시 선택하세요.',
        );
      }
      const idempotencyKey =
        globalThis.crypto?.randomUUID?.() ??
        `folder-import-retry-${job.job_id}-${Date.now()}`;
      const { data } = await retryDocMindLocalFolderImport(
        job.job_id,
        retryFiles,
        idempotencyKey,
      );
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind folder retry failed');
      }
      return data.data as DocMindImportJob;
    },
    onSuccess: async () => {
      await Promise.all([hierarchyQuery.refetch(), importJobsQuery.refetch()]);
    },
  });

  const registrationQuery = useQuery<DocMindRegistrationList, Error>({
    queryKey: DocMindKeys.registrations(),
    queryFn: async () => {
      const { data } = await getDocMindRegistrations();
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind registration list failed');
      }
      return data.data as DocMindRegistrationList;
    },
    enabled: canAdminister && adminOpen,
    refetchInterval: canAdminister && adminOpen ? 3_000 : false,
  });
  const registrationUpload = useMutation<
    unknown,
    Error,
    { folderId: string; files: File[] }
  >({
    mutationKey: ['docmind-registration-upload'],
    mutationFn: async ({ folderId, files }) => {
      const { data } = await registerDocMindDocuments(folderId, files);
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind registration failed');
      }
      return data.data;
    },
    onSuccess: async () => {
      setRegistrationFiles([]);
      await registrationQuery.refetch();
    },
  });
  const registrationRetry = useMutation<
    unknown,
    Error,
    { registrationId: string; idempotencyKey: string }
  >({
    mutationKey: ['docmind-registration-retry'],
    mutationFn: async ({ registrationId, idempotencyKey }) => {
      const { data } = await retryDocMindRegistration(
        registrationId,
        idempotencyKey,
      );
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind registration retry failed');
      }
      return data.data;
    },
    onSuccess: async () => registrationQuery.refetch(),
  });
  const draftQuery = useQuery<DocMindDraftList, Error>({
    queryKey: DocMindKeys.drafts(),
    queryFn: async () => {
      const { data } = await getDocMindDrafts();
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind draft list failed');
      }
      return data.data as DocMindDraftList;
    },
    enabled: canAdminister && adminOpen,
    refetchInterval: canAdminister && adminOpen ? 3_000 : false,
  });
  const draftCreate = useMutation<unknown, Error, string>({
    mutationKey: ['docmind-draft-create'],
    mutationFn: async (parentVersionId) => {
      const idempotencyKey =
        globalThis.crypto?.randomUUID?.() ??
        `draft-${parentVersionId}-${Date.now()}`;
      const { data } = await createDocMindDraft(
        parentVersionId,
        idempotencyKey,
      );
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind draft creation failed');
      }
      return data.data;
    },
    onSuccess: async () => draftQuery.refetch(),
  });
  const hierarchyCapture = useMutation<unknown, Error, string>({
    mutationKey: ['docmind-hierarchy-capture'],
    mutationFn: async (activeVersionId) => {
      const idempotencyKey =
        globalThis.crypto?.randomUUID?.() ??
        `hierarchy-capture-${activeVersionId}-${Date.now()}`;
      const { data } = await captureDocMindHierarchyDraft(
        activeVersionId,
        idempotencyKey,
      );
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind hierarchy capture failed');
      }
      return data.data;
    },
    onSuccess: async () => draftQuery.refetch(),
  });
  const draftChange = useMutation<
    unknown,
    Error,
    { draftId: string; change: DocMindDraftChangeRequest }
  >({
    mutationKey: ['docmind-draft-change'],
    mutationFn: async ({ draftId, change }) => {
      const idempotencyKey =
        globalThis.crypto?.randomUUID?.() ??
        `draft-change-${draftId}-${change.documentId}-${Date.now()}`;
      const { data } = await changeDocMindDraft(
        draftId,
        change,
        idempotencyKey,
      );
      if (data.code !== 0) {
        const code = data.message || 'DOCMIND_DRAFT_CHANGE_FAILED';
        throw new Error(draftChangeErrorMessage[code] ?? code);
      }
      return data.data;
    },
    onSuccess: async () => draftQuery.refetch(),
  });
  const draftGenerate = useMutation<unknown, Error, string>({
    mutationKey: ['docmind-draft-generate'],
    mutationFn: async (draftId) => {
      const { data } = await generateDocMindDraft(draftId);
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind draft generation failed');
      }
      return data.data;
    },
    onSuccess: async () => draftQuery.refetch(),
  });
  const versionQuery = useQuery<DocMindCatalogVersionList, Error>({
    queryKey: DocMindKeys.versions(),
    queryFn: async () => {
      const { data } = await getDocMindCatalogVersions();
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind version list failed');
      }
      return data.data as DocMindCatalogVersionList;
    },
    enabled: canAdminister && adminOpen,
    refetchInterval: canAdminister && adminOpen ? 1_000 : false,
  });
  const publishPreviewQuery = useQuery<DocMindDraft, Error>({
    queryKey: DocMindKeys.preview(publishCandidateId),
    queryFn: async () => {
      if (!publishCandidateId) throw new Error('Publish candidate is required');
      const { data } = await getDocMindDraft(publishCandidateId);
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind draft preview failed');
      }
      return data.data as DocMindDraft;
    },
    enabled:
      canAdminister &&
      workspaceView === 'catalog' &&
      Boolean(publishCandidateId),
  });
  const manualCardRevision = useMutation<
    DocMindManualCardRevisionResponse,
    Error,
    {
      revision: DocMindManualCardRevisionRequest;
      idempotencyKey: string;
    }
  >({
    mutationKey: ['docmind-manual-card-revision'],
    mutationFn: async ({ revision, idempotencyKey }) => {
      const { data } = await createDocMindManualCardRevision(
        revision,
        idempotencyKey,
      );
      if (data.code !== 0) {
        const code = data.message || 'DOCMIND_MANUAL_CARD_SAVE_FAILED';
        throw new Error(manualCardRevisionErrorMessage[code] ?? code);
      }
      return data.data as DocMindManualCardRevisionResponse;
    },
    onSuccess: async (revision) => {
      manualRevisionAttemptRef.current = undefined;
      setEditingRoutingCards(false);
      setRoutingCardEdits([]);
      await Promise.all([versionQuery.refetch(), draftQuery.refetch()]);
      setPublishCandidateId(revision.draft_id);
    },
  });
  const publishVersion = useMutation<
    unknown,
    Error,
    { versionId: string; expectedActiveVersionId: string }
  >({
    mutationKey: ['docmind-catalog-publish'],
    mutationFn: async ({ versionId, expectedActiveVersionId }) => {
      const idempotencyKey =
        globalThis.crypto?.randomUUID?.() ??
        `publish-${versionId}-${Date.now()}`;
      const { data } = await publishDocMindCatalogVersion(
        versionId,
        expectedActiveVersionId,
        idempotencyKey,
      );
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind publish failed');
      }
      return data.data;
    },
    onSuccess: async () => {
      setPublishCandidateId(undefined);
      await Promise.all([
        versionQuery.refetch(),
        draftQuery.refetch(),
        folderCatalog.refetch(),
      ]);
    },
  });
  const rollbackVersion = useMutation<
    unknown,
    Error,
    { versionId: string; expectedActiveVersionId: string }
  >({
    mutationKey: ['docmind-catalog-rollback'],
    mutationFn: async ({ versionId, expectedActiveVersionId }) => {
      const idempotencyKey =
        globalThis.crypto?.randomUUID?.() ??
        `rollback-${versionId}-${Date.now()}`;
      const { data } = await rollbackDocMindCatalogVersion(
        versionId,
        expectedActiveVersionId,
        idempotencyKey,
      );
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind rollback failed');
      }
      return data.data;
    },
    onSuccess: async () => {
      await Promise.all([
        versionQuery.refetch(),
        draftQuery.refetch(),
        folderCatalog.refetch(),
      ]);
    },
  });
  const deleteVersion = useMutation<
    unknown,
    Error,
    {
      versionId: string;
      versionLabel: string;
      expectedActiveVersionId: string;
    }
  >({
    mutationKey: ['docmind-catalog-version-delete'],
    mutationFn: async ({
      versionId,
      versionLabel,
      expectedActiveVersionId,
    }) => {
      const { data } = await deleteDocMindCatalogVersion(
        versionId,
        expectedActiveVersionId,
        versionLabel,
      );
      if (data.code !== 0) {
        throw new Error(data.message || 'DocMind version deletion failed');
      }
      return data.data;
    },
    onSettled: async () => {
      await Promise.all([versionQuery.refetch(), draftQuery.refetch()]);
    },
  });
  const chunks = React.useMemo(() => {
    const rankedChunks = (retrieval.data?.ranked_chunks ??
      retrieval.data?.chunks ??
      []) as ITestingChunk[];
    const seenDocumentIds = new Set<string>();
    return rankedChunks.filter((chunk) => {
      if (!chunk.doc_id) return true;
      if (seenDocumentIds.has(chunk.doc_id)) return false;
      seenDocumentIds.add(chunk.doc_id);
      return true;
    });
  }, [retrieval.data]);
  const visibleChunks = React.useMemo(
    () => chunks.slice(0, visibleResultCount),
    [chunks, visibleResultCount],
  );
  const remainingResultCount = Math.max(
    chunks.length - visibleChunks.length,
    0,
  );
  const folders = React.useMemo(
    () => folderCatalog.data?.folders ?? [],
    [folderCatalog.data?.folders],
  );
  const catalogInitialized = folderCatalog.data?.initialized !== false;
  const registrationFolders = React.useMemo(() => {
    const hierarchyFolders = (hierarchyQuery.data?.nodes ?? [])
      .filter(
        (node) => node.type === 'folder' && Boolean(node.semantic_folder_id),
      )
      .map((node) => ({
        id: node.semantic_folder_id as string,
        name: node.name,
        depth: node.depth,
      }));
    return hierarchyFolders.length ? hierarchyFolders : folders;
  }, [folders, hierarchyQuery.data?.nodes]);
  const activeCatalogVersionId =
    draftQuery.data?.active_version_id ??
    registrationQuery.data?.catalog_version_id;
  const expectedActiveCatalogVersionId = activeCatalogVersionId ?? '';
  const activeDraft = React.useMemo(() => {
    const candidates = (draftQuery.data?.drafts ?? []).filter(
      (draft) =>
        (draft.parent_version_id ?? '') === expectedActiveCatalogVersionId &&
        draft.active_parent_is_current,
    );
    return (
      candidates.find((draft) => draft.lifecycle_state !== 'FAILED') ??
      candidates[0]
    );
  }, [draftQuery.data?.drafts, expectedActiveCatalogVersionId]);
  const recaptureFailedHierarchy =
    activeDraft?.lifecycle_state === 'FAILED' &&
    activeDraft.snapshot_schema_version === 2;
  const draftedDocumentIds = React.useMemo(
    () =>
      new Set(activeDraft?.changes.map((change) => change.document_id) ?? []),
    [activeDraft?.changes],
  );
  const registrations = registrationQuery.data?.registrations ?? [];
  const catalogVersions = versionQuery.data?.versions ?? [];
  const historicalCatalogVersions = catalogVersions.filter(
    (version) =>
      !version.active && version.version_id !== activeDraft?.draft_id,
  );
  const visibleCatalogVersions = showVersionHistory
    ? historicalCatalogVersions
    : [];
  const activeVersion = catalogVersions.find((version) => version.active);
  const publishCandidate = catalogVersions.find(
    (version) => version.version_id === publishCandidateId,
  );
  const canonicalRoutingCards = React.useMemo(() => {
    const routingCards = publishPreviewQuery.data?.routing_cards ?? [];
    const folderOrdinals = new Map(
      (publishPreviewQuery.data?.folders ?? []).map((folder) => [
        folder.id,
        folder.ordinal,
      ]),
    );
    return [...routingCards].sort(
      (left, right) =>
        (folderOrdinals.get(left.folder_id) ?? Number.MAX_SAFE_INTEGER) -
        (folderOrdinals.get(right.folder_id) ?? Number.MAX_SAFE_INTEGER),
    );
  }, [publishPreviewQuery.data]);
  const manualCardEditsValid =
    routingCardEdits.length > 0 &&
    routingCardEdits.length === canonicalRoutingCards.length &&
    routingCardEdits.every(
      (card) =>
        isValidManualCardText(card.l0, 800) &&
        isValidManualCardText(card.l1, 3500),
    );
  const folderNames = React.useMemo(() => {
    const names = new Map(folders.map((folder) => [folder.id, folder.name]));
    for (const folder of activeDraft?.folders ?? []) {
      names.set(folder.id, folder.name);
    }
    for (const node of hierarchyQuery.data?.nodes ?? []) {
      if (node.type === 'folder' && node.semantic_folder_id) {
        names.set(node.semantic_folder_id, node.name);
      }
    }
    return names;
  }, [activeDraft?.folders, folders, hierarchyQuery.data?.nodes]);
  const documentFolderNames = React.useMemo(() => {
    const nodes = hierarchyQuery.data?.nodes ?? [];
    const names = new Map(
      nodes
        .filter((node) => node.type === 'folder')
        .map((node) => [node.file_id, node.relative_path || node.name]),
    );
    return new Map(
      nodes.flatMap((node) => {
        const name = names.get(node.parent_file_id || '');
        return node.type === 'file' && node.document_id && name
          ? [[node.document_id, name] as const]
          : [];
      }),
    );
  }, [hierarchyQuery.data?.nodes]);
  const groupedDraftChanges = React.useMemo(() => {
    const groups = new Map<
      string,
      {
        key: string;
        operation: string;
        fromName?: string;
        toName: string;
        count: number;
      }
    >();
    for (const change of activeDraft?.changes ?? []) {
      const fromName = change.from_folder_id
        ? (folderNames.get(change.from_folder_id) ?? '폴더 이름 확인 필요')
        : undefined;
      const toName = change.to_folder_id
        ? (folderNames.get(change.to_folder_id) ?? '폴더 이름 확인 필요')
        : '검색 범위에서 제외';
      const key = [change.operation, fromName ?? '', toName].join(':');
      const current = groups.get(key);
      if (current) {
        current.count += 1;
      } else {
        groups.set(key, {
          key,
          operation: change.operation,
          fromName,
          toName,
          count: 1,
        });
      }
    }
    return Array.from(groups.values()).sort(
      (left, right) =>
        right.count - left.count || left.key.localeCompare(right.key),
    );
  }, [activeDraft?.changes, folderNames]);

  const canonicalSelectedFolderIds = React.useMemo(
    () =>
      folders
        .filter((folder) => selectedFolderIds.includes(folder.id))
        .map((folder) => folder.id),
    [folders, selectedFolderIds],
  );
  const selectedFolderLabel =
    canonicalSelectedFolderIds.length === 0
      ? '전체(자동)'
      : canonicalSelectedFolderIds.length === 1
        ? folderNames.get(canonicalSelectedFolderIds[0])
        : `${canonicalSelectedFolderIds.length}개 폴더`;
  const effectiveScopeLabel = React.useMemo(() => {
    if (!retrieval.data) return '';
    const names = retrieval.data.effective_folder_ids.map(
      (folderId) => folderNames.get(folderId) ?? folderId,
    );
    const prefix =
      retrieval.data.scope_mode === 'automatic' ? '자동 선택' : '검색 범위';
    return `${prefix}: ${names.join(', ')}`;
  }, [folderNames, retrieval.data]);
  const staleFolderError =
    retrieval.isError &&
    retrieval.error.message.includes('DOCMIND_FOLDER_SELECTION_STALE');

  React.useLayoutEffect(() => {
    if (workspaceView !== 'document' && inspectionOrigin.current) {
      if (inspectionOrigin.current.isConnected)
        inspectionOrigin.current.focus({ preventScroll: true });
      inspectionOrigin.current = null;
    }
  }, [workspaceView]);

  const openLibrary = () => setWorkspaceParams({ view: 'library' });
  const openCatalog = () => setWorkspaceParams({ view: 'catalog' });
  const toggleVersionHistory = () => setShowVersionHistory((value) => !value);
  const renderSourceInspection = (node: DocMindHierarchyNode) => (
    <RagFlowInspectionLink
      datasetId={hierarchyQuery.data?.dataset_id}
      documentId={node.document_id}
      compact
    />
  );
  const renderRegistrationInspection = (registration: DocMindRegistration) => (
    <RagFlowInspectionLink
      datasetId={registrationQuery.data?.dataset_id}
      documentId={registration.document_id}
      compact
    />
  );
  const closeInspection = () =>
    setWorkspaceParams({ view: returnView }, { replace: true });
  const openInspection = (datasetId: string, documentId: string) => {
    const path = buildDocMindDocumentPath(datasetId, documentId, activeSection);
    if (!path) return;
    inspectionOrigin.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setSelectedChunk(undefined);
    setWorkspaceParams(new URLSearchParams(path.split('?')[1]));
  };

  const registrationFolderIds = React.useMemo(
    () => registrationFolders.map((folder) => folder.id),
    [registrationFolders],
  );
  useFolderSelection(
    registrationFolderIds,
    registrationFolderId,
    setRegistrationFolderId,
  );
  const refreshFolderTree = async () => {
    const response = await hierarchyQuery.refetch();
    if (response.error) throw response.error;
  };

  useEffect(() => {
    manualRevisionAttemptRef.current = undefined;
    setEditingRoutingCards(false);
    setRoutingCardEdits([]);
  }, [publishCandidateId]);

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    if (query.trim()) {
      retrieval.mutate({
        question: query.trim(),
        ...(canonicalSelectedFolderIds.length
          ? {
              folderIds: canonicalSelectedFolderIds,
              catalogVersionId: folderCatalog.data?.catalog_version_id,
            }
          : {}),
      });
    }
  };

  const toggleFolder = (folderId: string) => {
    setSelectedFolderIds((current) =>
      current.includes(folderId)
        ? current.filter((id) => id !== folderId)
        : [...current, folderId],
    );
  };

  const retryAcrossAllFolders = () => {
    setSelectedFolderIds([]);
    if (query.trim()) retrieval.mutate({ question: query.trim() });
  };

  const refreshStaleFolders = async () => {
    setSelectedFolderIds([]);
    setFolderMenuOpen(false);
    await folderCatalog.refetch();
  };

  const submitRegistration = (event: FormEvent) => {
    event.preventDefault();
    if (
      !registrationFolderId ||
      registrationFiles.length < 1 ||
      registrationFiles.length > 5
    ) {
      return;
    }
    registrationUpload.mutate({
      folderId: registrationFolderId,
      files: registrationFiles,
    });
  };

  const retryRegistration = (registrationId: string) => {
    const idempotencyKey =
      globalThis.crypto?.randomUUID?.() ??
      `retry-${registrationId}-${Date.now()}`;
    registrationRetry.mutate({ registrationId, idempotencyKey });
  };

  const createDraft = () => {
    if (recaptureFailedHierarchy) {
      hierarchyCapture.mutate(expectedActiveCatalogVersionId);
      return;
    }
    const parentVersionId = activeCatalogVersionId;
    if (parentVersionId) draftCreate.mutate(parentVersionId);
  };

  const captureHierarchy = () => {
    hierarchyCapture.mutate(expectedActiveCatalogVersionId);
  };

  const addRegistrationToDraft = (registration: DocMindRegistration) => {
    if (!activeDraft || !registration.folder_id) return;
    draftChange.mutate({
      draftId: activeDraft.draft_id,
      change: {
        operation: 'ADD',
        documentId: registration.document_id,
        registrationId: registration.registration_id,
        toFolderId: registration.folder_id,
      },
    });
  };

  const cancelDraftAdd = (documentId: string, folderId?: string) => {
    if (!activeDraft || !folderId) return;
    draftChange.mutate({
      draftId: activeDraft.draft_id,
      change: {
        operation: 'REMOVE',
        documentId,
        fromFolderId: folderId,
      },
    });
  };

  const generateDraft = () => {
    if (activeDraft?.can_generate) {
      draftGenerate.mutate(activeDraft.draft_id);
    }
  };

  const confirmPublish = () => {
    if (
      publishCandidate &&
      !manualCardRevision.isPending &&
      !editingRoutingCards
    ) {
      publishVersion.mutate({
        versionId: publishCandidate.version_id,
        expectedActiveVersionId: activeVersion?.version_id ?? '',
      });
    }
  };

  const startRoutingCardEdit = () => {
    if (!canonicalRoutingCards.length) return;
    manualCardRevision.reset?.();
    setRoutingCardEdits(
      canonicalRoutingCards.map((card) => ({
        folder_id: card.folder_id,
        folder_name: card.folder_name,
        l0: card.l0,
        l1: card.l1,
      })),
    );
    setEditingRoutingCards(true);
  };

  const cancelRoutingCardEdit = () => {
    if (manualCardRevision.isPending) return;
    manualRevisionAttemptRef.current = undefined;
    setEditingRoutingCards(false);
    setRoutingCardEdits([]);
    manualCardRevision.reset?.();
  };

  const updateRoutingCardEdit = (
    folderId: string,
    field: 'l0' | 'l1',
    value: string,
  ) => {
    manualRevisionAttemptRef.current = undefined;
    setRoutingCardEdits((current) =>
      current.map((card) =>
        card.folder_id === folderId ? { ...card, [field]: value } : card,
      ),
    );
  };

  const saveRoutingCardEdits = () => {
    if (
      !publishCandidateId ||
      !activeVersion ||
      !publishPreviewQuery.data ||
      !manualCardEditsValid ||
      manualCardRevision.isPending
    ) {
      return;
    }
    const revision: DocMindManualCardRevisionRequest = {
      sourceReadyVersionId: publishCandidateId,
      expectedActiveVersionId: activeVersion.version_id,
      expectedSourceSnapshotHash: publishPreviewQuery.data.snapshot_hash,
      cards: routingCardEdits.map(({ folder_id, l0, l1 }) => ({
        folder_id,
        l0,
        l1,
      })),
    };
    const fingerprint = JSON.stringify(revision);
    if (manualRevisionAttemptRef.current?.fingerprint !== fingerprint) {
      manualRevisionAttemptRef.current = {
        fingerprint,
        idempotencyKey:
          globalThis.crypto?.randomUUID?.() ??
          `manual-card-revision-${publishCandidateId}-${Date.now()}-${++manualRevisionKeySequenceRef.current}`,
      };
    }
    manualCardRevision.mutate({
      idempotencyKey: manualRevisionAttemptRef.current.idempotencyKey,
      revision,
    });
  };

  const closePublishReview = () => {
    if (manualCardRevision.isPending) return;
    manualRevisionAttemptRef.current = undefined;
    setPublishCandidateId(undefined);
  };

  const rollbackToVersion = (versionId: string) => {
    if (activeVersion) {
      rollbackVersion.mutate({
        versionId,
        expectedActiveVersionId: activeVersion.version_id,
      });
    }
  };

  const deleteFailedVersion = (version: DocMindCatalogVersion) => {
    if (
      !activeVersion ||
      !version.delete_allowed ||
      !window.confirm(
        `${version.version_label} 실패 이력을 삭제할까요?\n연결된 초안 데이터와 전용 OpenViking 루트도 함께 정리됩니다.`,
      )
    ) {
      return;
    }
    deleteVersion.mutate({
      versionId: version.version_id,
      versionLabel: version.version_label,
      expectedActiveVersionId: activeVersion.version_id,
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
        <main className="docmind flex h-full min-h-0 flex-col overflow-hidden bg-bg-base text-text-primary">
          <div hidden={workspaceView !== 'search'} className="shrink-0">
            <header className="z-10 flex-none grid grid-cols-[8rem_minmax(12rem,1fr)_auto] items-center gap-5 border-b border-border-button bg-bg-base px-7 py-3 max-sm:flex max-sm:flex-wrap">
              <h2 className="text-xl font-semibold tracking-tight">검색</h2>
              <form
                id="docmind-search"
                onSubmit={submitSearch}
                className="relative w-full max-w-[46rem] justify-self-center"
              >
                <Search className="pointer-events-none absolute left-5 top-1/2 size-6 -translate-y-1/2 text-text-secondary" />
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="문서에서 찾고 싶은 내용을 입력하세요"
                  className="h-12 rounded-full border border-border-button bg-bg-input pl-12 pr-12 text-base text-text-primary placeholder:text-text-secondary focus-visible:ring-accent-primary"
                  style={{
                    backgroundColor: 'var(--bg-input)',
                    color: 'rgb(var(--text-primary))',
                    caretColor: 'rgb(var(--text-primary))',
                  }}
                />
                {query && (
                  <button
                    type="button"
                    onClick={() => setQuery('')}
                    className="absolute right-5 top-1/2 -translate-y-1/2 text-text-secondary hover:text-text-primary"
                    aria-label="검색어 지우기"
                  >
                    <X />
                  </button>
                )}
              </form>
              <div className="flex items-center gap-4 justify-self-end">
                {!canAdminister &&
                  folderCatalog.data?.initialized === false && (
                    <button
                      type="button"
                      onClick={() => bootstrapWorkspace.mutate()}
                      disabled={bootstrapWorkspace.isPending}
                      className="h-11 rounded-lg border border-accent-primary/30 bg-accent-primary/10 px-4 text-sm font-medium text-accent-primary hover:bg-accent-primary/20 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      문서 등록 준비
                    </button>
                  )}
                {canAdminister && (
                  <button
                    type="button"
                    onClick={openLibrary}
                    className="h-11 rounded-lg border border-accent-primary/30 bg-accent-primary/10 px-4 text-sm font-medium text-accent-primary hover:bg-accent-primary/20"
                  >
                    문서 등록
                  </button>
                )}
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => setFolderMenuOpen((open) => !open)}
                    className="flex h-11 items-center gap-2 rounded-lg border border-border-button bg-bg-card px-4 text-base text-text-primary hover:bg-text-primary/5"
                    aria-expanded={folderMenuOpen}
                    aria-haspopup="listbox"
                  >
                    <span>폴더: {selectedFolderLabel}</span>
                    <ChevronDown className="size-5" />
                  </button>
                  {folderMenuOpen && (
                    <div
                      className="absolute right-0 top-12 z-30 w-80 rounded-xl border border-border-button bg-bg-card p-2 shadow-2xl"
                      role="listbox"
                      aria-label="검색 폴더 선택"
                      aria-multiselectable="true"
                    >
                      <button
                        type="button"
                        onClick={() => setSelectedFolderIds([])}
                        className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-left hover:bg-text-primary/5"
                        role="option"
                        aria-selected={canonicalSelectedFolderIds.length === 0}
                      >
                        <span>전체(자동)</span>
                        <span className="text-xs text-text-secondary">
                          OpenViking 선택
                        </span>
                      </button>
                      {folders.map((folder) => (
                        <label
                          key={folder.id}
                          className="flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 hover:bg-text-primary/5"
                          role="option"
                          aria-selected={selectedFolderIds.includes(folder.id)}
                        >
                          <input
                            type="checkbox"
                            checked={selectedFolderIds.includes(folder.id)}
                            onChange={() => toggleFolder(folder.id)}
                          />
                          <span className="min-w-0 flex-1 truncate">
                            {'\u00a0'.repeat((folder.depth ?? 0) * 2)}
                            {folder.name}
                          </span>
                          <span className="text-xs text-text-secondary">
                            {folder.document_count}개
                          </span>
                        </label>
                      ))}
                      {folderCatalog.isError && (
                        <p className="px-3 py-2 text-sm text-state-error">
                          폴더 목록을 불러오지 못했습니다.
                        </p>
                      )}
                    </div>
                  )}
                </div>
                <button
                  type="submit"
                  form="docmind-search"
                  disabled={
                    !catalogInitialized || !query.trim() || retrieval.isPending
                  }
                  className="grid size-11 place-items-center rounded-lg bg-accent-primary text-primary-foreground hover:bg-accent-primary/90 disabled:cursor-not-allowed disabled:opacity-40"
                  aria-label="검색 실행"
                  title="검색 실행"
                >
                  <Search className="size-5" />
                </button>
              </div>
            </header>
          </div>

          {workspaceView === 'document' && (
            <div className="min-h-0 flex-1" aria-label="문서 상세">
              {validInspection ? (
                <ErrorBoundary
                  FallbackComponent={WorkspaceViewError}
                  resetKeys={[inspectionDocumentId]}
                >
                  <React.Suspense
                    fallback={
                      <p role="status" className="p-6 text-text-secondary">
                        원문과 청크를 불러오는 중입니다.
                      </p>
                    }
                  >
                    <ChunkInspection
                      key={`${inspectionDatasetId}:${inspectionDocumentId}`}
                      embedded
                      readOnly
                      onBack={closeInspection}
                    />
                  </React.Suspense>
                </ErrorBoundary>
              ) : (
                <div className="p-6">
                  <p role="alert">문서 주소가 올바르지 않습니다.</p>
                  <button
                    type="button"
                    onClick={closeInspection}
                    className="mt-4 rounded-lg border border-border-button px-4 py-2"
                  >
                    이전 화면으로
                  </button>
                </div>
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
                  <p role="status" className="p-6 text-text-secondary">
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

          <section
            hidden={
              workspaceView === 'document' || workspaceView === 'settings'
            }
            className="mx-auto min-h-0 w-full max-w-[72rem] flex-1 overflow-y-auto overscroll-contain px-7 py-5 max-sm:px-5"
          >
            {adminOpen && !canAdminister && (
              <p role="status" className="p-6 text-text-secondary">
                {folderCatalog.isPending
                  ? '접근 권한을 확인하는 중입니다.'
                  : '이 화면은 관리자만 사용할 수 있습니다.'}
              </p>
            )}
            {canAdminister && adminOpen && (
              <section className="mb-6 space-y-5">
                <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="text-lg font-semibold">
                      {workspaceView === 'catalog' ? 'Catalog' : '자료 관리'}
                    </h2>
                    <p className="mt-1 text-sm text-text-secondary">
                      {workspaceView === 'catalog'
                        ? '폴더 설명을 검토하고 Publish한 버전을 검색에 반영합니다.'
                        : '폴더와 문서를 등록하고 원문·청크와 처리 상태를 확인합니다.'}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => registrationQuery.refetch()}
                    className="rounded-lg border border-border-button px-3 py-2 text-sm text-text-primary hover:bg-text-primary/5"
                  >
                    상태 새로고침
                  </button>
                </div>

                {workspaceView === 'catalog' && (
                  <section
                    aria-label="현재 검색 버전"
                    className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-bg-card px-4 py-3"
                  >
                    <div>
                      <p className="text-xs text-text-secondary">
                        현재 검색에 사용 중
                      </p>
                      <p className="mt-1 text-sm font-medium">
                        {activeVersion?.version_label ??
                          (versionQuery.isPending
                            ? '불러오는 중'
                            : '버전 정보 없음')}
                      </p>
                    </div>
                    {activeVersion && (
                      <span className="text-sm text-text-secondary">
                        문서 {activeVersion.membership_count}개 ·{' '}
                        {activeVersion.health_state === 'VALID'
                          ? '검색 반영됨'
                          : activeVersion.health_state}
                      </span>
                    )}
                    {activeVersion?.routing_cards?.length ? (
                      <details className="basis-full rounded-lg border border-border-button bg-text-primary/5 p-3">
                        <summary className="cursor-pointer text-sm font-medium text-accent-primary">
                          게시된 L0/L1 {activeVersion.routing_cards.length}개
                          보기
                        </summary>
                        <div className="mt-3 grid gap-3">
                          {activeVersion.routing_cards.map((card) => (
                            <article
                              key={card.folder_id}
                              className="rounded-lg border border-border-button bg-bg-card p-3"
                            >
                              <h4 className="font-medium">
                                {card.relative_path || card.folder_name}
                              </h4>
                              <div className="mt-2">
                                <p className="text-xs font-semibold text-accent-primary">
                                  L0 · 짧은 폴더 설명
                                </p>
                                <p className="mt-1 whitespace-pre-wrap text-sm text-text-primary">
                                  {card.l0}
                                </p>
                              </div>
                              <details className="mt-3">
                                <summary className="cursor-pointer text-xs font-semibold text-accent-primary">
                                  L1 · 상세 라우팅 설명 보기
                                </summary>
                                <p className="mt-2 whitespace-pre-wrap text-sm text-text-secondary">
                                  {card.l1}
                                </p>
                              </details>
                            </article>
                          ))}
                        </div>
                      </details>
                    ) : activeVersion && !versionQuery.isPending ? (
                      <p className="basis-full text-xs text-text-secondary">
                        게시된 L0/L1 정보가 없습니다.
                      </p>
                    ) : null}
                  </section>
                )}
                <div hidden={workspaceView !== 'library'}>
                  <div className="mb-3 text-xs text-text-secondary">
                    등록하면 인덱싱이 시작됩니다. 검색 반영은 Catalog에서
                    Publish하세요.
                  </div>

                  <div
                    className="mb-4 flex flex-wrap gap-2"
                    aria-label="자료 추가 방식 선택"
                  >
                    <button
                      type="button"
                      onClick={() => setAdminIngestionPath('folder')}
                      aria-pressed={adminIngestionPath === 'folder'}
                      aria-label="폴더 전체 가져오기 선택"
                      className={`rounded-lg border px-4 py-2 text-sm transition-colors ${
                        adminIngestionPath === 'folder'
                          ? 'border-accent-primary/50 bg-accent-primary/10 text-accent-primary'
                          : 'border-border-button bg-text-primary/5 text-text-secondary hover:bg-text-primary/5'
                      }`}
                    >
                      <span className="block font-semibold">
                        폴더 전체 가져오기
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => setAdminIngestionPath('documents')}
                      aria-pressed={adminIngestionPath === 'documents'}
                      aria-label="기존 폴더에 문서 추가 선택"
                      className={`rounded-lg border px-4 py-2 text-sm transition-colors ${
                        adminIngestionPath === 'documents'
                          ? 'border-accent-primary/50 bg-accent-primary/10 text-accent-primary'
                          : 'border-border-button bg-text-primary/5 text-text-secondary hover:bg-text-primary/5'
                      }`}
                    >
                      <span className="block font-semibold">
                        기존 폴더에 문서 추가
                      </span>
                    </button>
                  </div>
                </div>

                <div hidden={workspaceView !== 'library'}>
                  {adminIngestionPath === 'folder' && (
                    <>
                      <section className="mb-5 rounded-lg border border-border-button p-4">
                        <div className="flex flex-wrap items-start justify-between gap-3">
                          <div>
                            <h3 className="font-semibold text-accent-primary">
                              폴더 가져오기
                            </h3>
                            <p className="mt-1 text-sm text-text-secondary">
                              하위 폴더 구조와 문서를 함께 가져옵니다.
                            </p>
                            <p className="mt-1 text-xs font-medium text-accent-primary">
                              최대 100개 파일
                            </p>
                          </div>
                          <button
                            type="button"
                            onClick={() => hierarchyQuery.refetch()}
                            className="rounded-lg border border-border-button px-3 py-2 text-sm hover:bg-text-primary/5"
                          >
                            트리 새로고침
                          </button>
                        </div>
                        <div className="mt-4 grid gap-3 md:grid-cols-[minmax(18rem,2fr)_auto]">
                          <label className="grid gap-1 text-sm text-text-secondary">
                            로컬 폴더
                            <input
                              type="file"
                              multiple
                              {...({
                                webkitdirectory: '',
                                directory: '',
                              } as React.InputHTMLAttributes<HTMLInputElement>)}
                              onChange={(event) =>
                                setLocalFolderFiles(
                                  Array.from(event.target.files ?? []),
                                )
                              }
                              className="h-11 rounded-lg border border-border-button bg-bg-input px-3 py-2 text-sm text-text-primary file:mr-3 file:rounded file:border-0 file:px-3 file:py-1"
                              aria-label="가져올 로컬 폴더"
                            />
                          </label>
                          <button
                            type="button"
                            onClick={() =>
                              localFolderImport.mutate(localFolderFiles)
                            }
                            disabled={
                              localFolderImport.isPending ||
                              localFolderFiles.length === 0 ||
                              localFolderFiles.length > 100
                            }
                            className="mt-5 h-11 rounded-lg bg-accent-primary px-5 font-medium disabled:opacity-40"
                          >
                            {localFolderImport.isPending
                              ? '가져오는 중'
                              : localFolderFiles.length
                                ? `폴더 전체 가져오기 · ${localFolderFiles.length}개 문서`
                                : '폴더 전체 가져오기'}
                          </button>
                        </div>
                        <details className="mt-2 text-xs text-text-secondary">
                          <summary className="w-fit cursor-pointer">
                            가져오기 안내
                          </summary>
                          <p className="mt-2">
                            브라우저가 전달하지 않는 빈 폴더는 자동으로 만들 수
                            없습니다. 아래의 수동 폴더 추가로 보완할 수
                            있습니다.
                          </p>
                        </details>
                        {localFolderFiles.length > 100 && (
                          <p className="mt-2 text-sm text-state-error">
                            한 번에 최대 100개 파일까지 가져올 수 있습니다.
                            폴더를 나누어 실행하세요.
                          </p>
                        )}
                        {localFolderImport.isError && (
                          <p className="mt-2 text-sm text-state-error">
                            {localFolderImport.error.message}
                          </p>
                        )}
                        {localFolderImport.data && (
                          <p role="status" className="mt-3 text-sm">
                            등록 {localFolderImport.data.succeeded_count}개 ·
                            실패 {localFolderImport.data.failed_count}개.
                            아래에서 인덱싱 상태를 확인하세요.
                          </p>
                        )}
                        {(importJobsQuery.data?.jobs?.length ?? 0) > 0 && (
                          <details className="mt-3 text-xs text-text-secondary">
                            <summary className="w-fit cursor-pointer">
                              가져오기 기록 {importJobsQuery.data?.jobs.length}
                              건
                            </summary>
                            <div className="max-h-64 overflow-y-auto">
                              {(importJobsQuery.data?.jobs ?? []).map((job) => (
                                <div
                                  key={job.job_id}
                                  className="mt-3 rounded-lg border border-border-button px-3 py-2 text-sm"
                                >
                                  <div className="flex justify-between gap-3">
                                    <span>{job.state}</span>
                                    <span className="text-text-secondary">
                                      성공 {job.succeeded_count} · 실패{' '}
                                      {job.failed_count}
                                    </span>
                                  </div>
                                  {job.items
                                    .filter((item) => item.state === 'FAILED')
                                    .map((item) => (
                                      <p
                                        key={item.relative_path}
                                        className="mt-1 text-state-error"
                                      >
                                        {item.relative_path} · {item.error_code}
                                      </p>
                                    ))}
                                  {job.failed_count > 0 && (
                                    <button
                                      type="button"
                                      onClick={() =>
                                        localFolderRetry.mutate({
                                          job,
                                          files: localFolderFiles,
                                        })
                                      }
                                      disabled={
                                        localFolderRetry.isPending ||
                                        localFolderFiles.length === 0
                                      }
                                      className="mt-2 rounded border border-state-error/30 px-3 py-1 text-state-error disabled:opacity-40"
                                    >
                                      실패 항목만 재시도
                                    </button>
                                  )}
                                </div>
                              ))}
                            </div>
                          </details>
                        )}
                        {localFolderRetry.isError && (
                          <p className="mt-2 text-sm text-state-error">
                            {localFolderRetry.error.message}
                          </p>
                        )}
                      </section>

                      <section className="mb-5 border-t border-border-button pt-4">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <h3 className="font-semibold">폴더와 문서</h3>
                            <p className="mt-1 text-sm text-text-secondary">
                              폴더를 펼쳐 문서를 확인하세요.
                            </p>
                          </div>
                          <button
                            type="button"
                            onClick={captureHierarchy}
                            disabled={hierarchyCapture.isPending}
                            className="rounded-lg border border-state-success/30 bg-state-success/10 px-3 py-2 text-sm text-state-success disabled:opacity-40"
                          >
                            {hierarchyCapture.isPending
                              ? '초안 캡처 중'
                              : '계층 Catalog 초안 만들기'}
                          </button>
                        </div>
                        <SourceTree
                          nodes={hierarchyQuery.data?.nodes ?? []}
                          renderDocumentAction={renderSourceInspection}
                        />
                        <details className="mt-3 rounded-lg border border-border-button bg-text-primary/5 p-3">
                          <summary className="cursor-pointer text-sm font-medium text-text-primary">
                            고급 폴더 편집 · 추가, 이름 바꾸기, 위치 옮기기,
                            삭제
                          </summary>
                          <FolderEditor
                            nodes={hierarchyQuery.data?.nodes ?? []}
                            rootId={hierarchyQuery.data?.source_root_file_id}
                            loading={hierarchyQuery.isFetching}
                            onChanged={refreshFolderTree}
                          />
                        </details>
                        {(hierarchyQuery.isError ||
                          hierarchyCapture.isError) && (
                          <p className="mt-2 text-sm text-state-error">
                            {hierarchyQuery.error?.message ??
                              hierarchyCapture.error?.message}
                          </p>
                        )}
                      </section>
                    </>
                  )}

                  {adminIngestionPath === 'documents' && (
                    <section className="mb-5 rounded-xl border border-border-button bg-text-primary/5 p-4">
                      <div className="mb-3">
                        <h3 className="font-semibold">문서 추가</h3>
                        <p className="mt-1 text-sm text-text-secondary">
                          선택한 폴더에 문서 1~5개를 추가합니다.
                        </p>
                        <p className="mt-1 text-xs font-medium text-text-secondary">
                          기존 폴더에 추가 · 하위 폴더 구조 변경 없음
                        </p>
                      </div>
                      <form
                        onSubmit={submitRegistration}
                        className="grid gap-3 md:grid-cols-[minmax(12rem,1fr)_minmax(16rem,2fr)_auto]"
                      >
                        <label className="grid gap-1 text-sm text-text-secondary">
                          대상 폴더
                          <select
                            value={registrationFolderId}
                            onChange={(event) =>
                              setRegistrationFolderId(event.target.value)
                            }
                            className="h-11 rounded-lg border border-border-button bg-bg-input px-3 text-text-primary"
                            aria-label="등록 대상 폴더"
                          >
                            <option value="" disabled>
                              폴더를 선택하세요
                            </option>
                            {registrationFolders.map((folder) => (
                              <option key={folder.id} value={folder.id}>
                                {'- '.repeat(folder.depth ?? 0)} {folder.name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="grid gap-1 text-sm text-text-secondary">
                          추가할 문서 1~5개
                          <input
                            type="file"
                            multiple
                            onChange={(event) =>
                              setRegistrationFiles(
                                Array.from(event.target.files ?? []),
                              )
                            }
                            className="h-11 rounded-lg border border-border-button bg-bg-input px-3 py-2 text-sm text-text-primary file:mr-3 file:rounded file:border-0 file:px-3 file:py-1"
                            aria-label="등록할 문서"
                          />
                        </label>
                        <button
                          type="submit"
                          disabled={
                            registrationUpload.isPending ||
                            registrationFiles.length < 1 ||
                            registrationFiles.length > 5 ||
                            !registrationFolderId
                          }
                          className="mt-5 h-11 rounded-lg bg-accent-primary px-5 font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          {registrationUpload.isPending
                            ? '추가 중'
                            : '선택한 폴더에 문서 추가'}
                        </button>
                      </form>
                      {registrationFiles.length > 5 && (
                        <p className="mt-2 text-sm text-state-error">
                          한 번에 최대 5개까지만 등록할 수 있습니다.
                        </p>
                      )}
                      {registrationUpload.isError && (
                        <p className="mt-2 text-sm text-state-error">
                          {registrationUpload.error.message}
                        </p>
                      )}
                    </section>
                  )}

                  <button
                    type="button"
                    onClick={openCatalog}
                    className="mb-3 rounded-lg border border-border-button px-4 py-2 text-sm"
                  >
                    Catalog에서 검색 반영 검토
                  </button>
                </div>
                <section
                  hidden={workspaceView !== 'catalog'}
                  className="mt-5 rounded-lg border border-border-button p-4"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h3 className="font-semibold">Catalog 초안</h3>
                      <p className="mt-1 text-sm text-text-secondary">
                        변경은 Publish 전까지 현재 검색 결과에 영향을 주지
                        않습니다.
                      </p>
                    </div>
                    {(!activeDraft ||
                      activeDraft.lifecycle_state === 'FAILED') && (
                      <button
                        type="button"
                        onClick={createDraft}
                        disabled={
                          recaptureFailedHierarchy
                            ? hierarchyCapture.isPending
                            : draftCreate.isPending || !activeCatalogVersionId
                        }
                        className="rounded-lg border border-accent-primary/30 bg-accent-primary/10 px-3 py-2 text-sm text-accent-primary hover:bg-accent-primary/20 disabled:opacity-40"
                      >
                        {recaptureFailedHierarchy
                          ? hierarchyCapture.isPending
                            ? '계층 초안 다시 만드는 중'
                            : '계층 초안 다시 만들기'
                          : draftCreate.isPending
                            ? '초안 생성 중'
                            : '새 초안 만들기'}
                      </button>
                    )}
                  </div>
                  {draftQuery.isError && (
                    <p className="mt-3 text-sm text-state-error">
                      {draftQuery.error.message}
                    </p>
                  )}
                  {draftCreate.isError && (
                    <p className="mt-3 text-sm text-state-error">
                      {draftCreate.error.message}
                    </p>
                  )}
                  {draftChange.isError && (
                    <p className="mt-3 text-sm text-state-error">
                      {draftChange.error.message}
                    </p>
                  )}
                  {draftGenerate.isError && (
                    <p className="mt-3 text-sm text-state-error">
                      {draftGenerate.error.message}
                    </p>
                  )}
                  {activeDraft ? (
                    <div className="mt-4 grid gap-3">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="flex flex-wrap items-center gap-3">
                          <span className="text-lg font-semibold">
                            변경 {activeDraft.change_count}건
                          </span>
                          <span className="text-xs text-text-secondary">
                            {activeDraft.lifecycle_state === 'READY'
                              ? activeDraft.health_state === 'VALID'
                                ? 'L0/L1 준비됨'
                                : '초안 상태 확인 필요'
                              : draftStateLabel[activeDraft.lifecycle_state]}
                          </span>
                        </div>
                        {activeDraft.can_generate && (
                          <button
                            type="button"
                            onClick={generateDraft}
                            disabled={draftGenerate.isPending}
                            className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
                          >
                            {draftGenerate.isPending
                              ? '실행 준비 중'
                              : activeDraft.snapshot_schema_version === 2
                                ? '계층 L0/L1 생성 및 저장'
                                : '초안 생성·검증 실행'}
                          </button>
                        )}
                      </div>
                      {['GENERATING', 'VALIDATING'].includes(
                        activeDraft.lifecycle_state,
                      ) && (
                        <div>
                          <div className="mb-1 flex justify-between text-xs text-text-secondary">
                            <span>
                              {activeDraft.lifecycle_state === 'READY' &&
                              activeDraft.readiness_mode === 'ADMIN_SAVED'
                                ? '관리자 저장 완료'
                                : draftStateLabel[activeDraft.lifecycle_state]}
                            </span>
                            <span>{activeDraft.generation_progress}%</span>
                          </div>
                          <div className="h-2 overflow-hidden rounded-full bg-text-primary/5">
                            <div
                              className="h-full rounded-full bg-accent-primary transition-all"
                              style={{
                                width: `${Math.max(0, Math.min(100, activeDraft.generation_progress))}%`,
                              }}
                            />
                          </div>
                        </div>
                      )}
                      {activeDraft.lifecycle_state === 'READY' &&
                        activeDraft.health_state === 'VALID' && (
                          <div className="flex flex-wrap items-center justify-between gap-3 py-2 text-sm">
                            <span>
                              내용을 확인한 뒤 Publish하면 검색에 반영됩니다.
                            </span>
                            <button
                              type="button"
                              onClick={() =>
                                setPublishCandidateId(activeDraft.draft_id)
                              }
                              className="rounded-lg bg-accent-primary px-4 py-2 font-medium text-primary-foreground"
                            >
                              Publish 검토
                            </button>
                          </div>
                        )}
                      {activeDraft.lifecycle_state === 'FAILED' && (
                        <p className="rounded-lg border border-state-error/20 bg-state-error/10 px-3 py-2 text-sm text-state-error">
                          {activeDraft.health_reason
                            ? (generationErrorMessage[
                                activeDraft.health_reason
                              ] ?? activeDraft.health_reason)
                            : 'DOCMIND_GENERATION_FAILED'}
                        </p>
                      )}
                      <details className="text-xs text-text-secondary">
                        <summary className="w-fit cursor-pointer">
                          초안 정보
                        </summary>
                        <div className="mt-2 grid gap-2">
                          <p>
                            {activeDraft.version_label} · 문서{' '}
                            {activeDraft.membership_count}개 ·{' '}
                            {activeDraft.health_state}
                          </p>
                          <p>
                            routing digest {activeDraft.digest_ready_count}/
                            {activeDraft.membership_count} · L0/L1 카드{' '}
                            {activeDraft.card_ready_count}/
                            {activeDraft.folders.length}
                          </p>
                          <p>
                            {activeDraft.readiness_mode === 'ADMIN_SAVED'
                              ? '관리자 저장본 · 검색 검증 안 함'
                              : draftStateLabel[activeDraft.lifecycle_state]}
                          </p>
                          {activeDraft.folders.map((folder) => (
                            <p key={folder.id}>
                              {folder.name} · 문서 {folder.document_count}개
                            </p>
                          ))}
                        </div>
                      </details>
                      {activeDraft.changes.length === 0 ? (
                        <p className="text-sm text-text-secondary">
                          아직 초안 변경이 없습니다. 인덱싱 완료 문서를 추가할
                          수 있습니다.
                        </p>
                      ) : (
                        <div className="grid gap-3">
                          <div
                            className="flex flex-wrap gap-3 text-sm text-text-secondary"
                            aria-label="Catalog 초안 변경 요약"
                          >
                            {(['ADD', 'MOVE', 'REMOVE'] as const)
                              .filter((operation) =>
                                activeDraft.changes.some(
                                  (change) => change.operation === operation,
                                ),
                              )
                              .map((operation) => (
                                <div
                                  key={operation}
                                  className="flex items-center gap-1.5"
                                >
                                  <p className="text-xs text-text-secondary">
                                    {operation}
                                  </p>
                                  <p className="font-medium">
                                    {
                                      activeDraft.changes.filter(
                                        (change) =>
                                          change.operation === operation,
                                      ).length
                                    }
                                    건
                                  </p>
                                </div>
                              ))}
                          </div>
                          <div className="grid gap-2">
                            {groupedDraftChanges.map((group) => (
                              <div
                                key={group.key}
                                className="border-b border-border-button py-2 text-sm"
                                aria-label={`${group.operation} ${
                                  group.fromName ? `${group.fromName}에서 ` : ''
                                }${group.toName}로 ${group.count}개`}
                              >
                                <span className="font-medium">
                                  {group.operation}
                                </span>
                                <span className="text-text-secondary">
                                  {' · '}
                                  {group.fromName ? `${group.fromName} → ` : ''}
                                  {group.toName} · {group.count}개
                                </span>
                              </div>
                            ))}
                          </div>
                          <details className="rounded-lg border border-border-button bg-text-primary/5 p-3">
                            <summary className="cursor-pointer text-sm font-medium text-text-primary">
                              상세 변경 {activeDraft.changes.length}건 보기
                            </summary>
                            <div className="mt-3 grid gap-2">
                              {activeDraft.changes.map((change) => (
                                <div
                                  key={change.document_id}
                                  className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border-button px-3 py-2"
                                >
                                  <div className="min-w-0">
                                    <p className="truncate text-sm font-medium">
                                      {change.operation} ·{' '}
                                      {change.document_name ??
                                        change.document_id}
                                    </p>
                                    <p className="text-xs text-text-secondary">
                                      {change.from_folder_id
                                        ? `${folderNames.get(change.from_folder_id) ?? '폴더 이름 확인 필요'} → `
                                        : ''}
                                      {change.to_folder_id
                                        ? (folderNames.get(
                                            change.to_folder_id,
                                          ) ?? '폴더 이름 확인 필요')
                                        : '검색 범위에서 제외'}
                                    </p>
                                  </div>
                                  {change.operation === 'ADD' && (
                                    <button
                                      type="button"
                                      onClick={() =>
                                        cancelDraftAdd(
                                          change.document_id,
                                          change.to_folder_id,
                                        )
                                      }
                                      disabled={draftChange.isPending}
                                      className="rounded-lg border border-border-button px-3 py-1 text-xs hover:bg-text-primary/5 disabled:opacity-40"
                                    >
                                      추가 취소
                                    </button>
                                  )}
                                </div>
                              ))}
                            </div>
                          </details>
                        </div>
                      )}
                    </div>
                  ) : (
                    <p className="mt-3 text-sm text-text-secondary">
                      현재 활성 Catalog를 부모로 새 초안을 만든 뒤 변경을
                      추가하세요.
                    </p>
                  )}
                  {publishVersion.isError && (
                    <p className="mt-3 text-sm text-state-error">
                      {publishVersion.error.message}
                    </p>
                  )}
                  {publishCandidate && (
                    <div className="mt-4 rounded-xl border border-state-success/25 bg-text-primary/5 p-4">
                      <h4 className="font-semibold">Publish 최종 확인</h4>
                      <p className="mt-2 text-sm text-text-secondary">
                        {activeVersion?.version_label ?? '첫 Catalog'} 문서{' '}
                        {activeVersion?.membership_count ?? 0}개 →{' '}
                        {publishCandidate.version_label} 문서{' '}
                        {publishCandidate.membership_count}개
                      </p>
                      <p className="mt-1 text-sm text-text-secondary">
                        {publishCandidate.readiness_mode === 'ADMIN_SAVED'
                          ? '저장 무결성 보고서 '
                          : '검증 보고서 '}
                        {publishCandidate.validation_report_hash?.slice(0, 16)}…
                        · 롤백 대상{' '}
                        {activeVersion?.version_label ?? '없음 (첫 Publish)'}
                      </p>
                      {(publishCandidate.readiness_mode === 'ADMIN_SAVED' ||
                        publishPreviewQuery.data?.readiness_mode ===
                          'ADMIN_SAVED') && (
                        <span className="mt-3 inline-flex rounded-full bg-state-warning/15 px-3 py-1 text-sm font-medium text-state-warning">
                          관리자 저장본 · 검색 검증 안 함
                        </span>
                      )}
                      <div className="mt-4 grid gap-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <h5 className="text-sm font-semibold text-state-success">
                            폴더별 L0/L1 확인
                          </h5>
                          {!editingRoutingCards && (
                            <button
                              type="button"
                              onClick={startRoutingCardEdit}
                              disabled={
                                manualCardRevision.isPending ||
                                publishVersion.isPending ||
                                canonicalRoutingCards.length === 0
                              }
                              className="rounded-lg border border-accent-primary/30 px-3 py-1 text-sm text-accent-primary hover:bg-accent-primary/10 disabled:opacity-40"
                            >
                              수정
                            </button>
                          )}
                        </div>
                        {editingRoutingCards && (
                          <p className="rounded-lg border border-accent-primary/20 bg-accent-primary/5 px-3 py-2 text-sm text-accent-primary">
                            각 L0/L1은 비워 둘 수 없고 한국어를 포함해야 합니다.
                            L0는 800자, L1은 3500자까지 저장할 수 있습니다.
                            viking URI와 API 키·credential 문자열은 입력할 수
                            없습니다.
                          </p>
                        )}
                        {publishPreviewQuery.isPending && (
                          <p className="text-sm text-text-secondary">
                            L0/L1을 불러오는 중입니다.
                          </p>
                        )}
                        {publishPreviewQuery.isError && (
                          <p className="text-sm text-state-error">
                            {publishPreviewQuery.error.message}
                          </p>
                        )}
                        {(editingRoutingCards
                          ? routingCardEdits
                          : canonicalRoutingCards
                        ).map((card) => (
                          <article
                            key={card.folder_id}
                            className="rounded-lg border border-border-button bg-text-primary/5 p-3"
                          >
                            <h6 className="font-medium">{card.folder_name}</h6>
                            <div className="mt-2">
                              <p className="text-sm font-semibold text-accent-primary">
                                L0 · 짧은 폴더 설명
                              </p>
                              {editingRoutingCards ? (
                                <>
                                  <textarea
                                    aria-label={`${card.folder_name} L0`}
                                    value={card.l0}
                                    maxLength={800}
                                    disabled={manualCardRevision.isPending}
                                    onChange={(event) =>
                                      updateRoutingCardEdit(
                                        card.folder_id,
                                        'l0',
                                        event.target.value,
                                      )
                                    }
                                    className="mt-1 min-h-24 w-full rounded-md border border-border-button bg-bg-input p-3 text-sm text-text-primary disabled:opacity-50"
                                  />
                                  <p className="mt-1 text-sm text-text-secondary">
                                    {card.l0.length}/800 ·{' '}
                                    {hasKorean(card.l0)
                                      ? '한국어 포함'
                                      : '한국어 필요'}
                                  </p>
                                </>
                              ) : (
                                <p className="mt-1 whitespace-pre-wrap text-sm text-text-primary">
                                  {card.l0}
                                </p>
                              )}
                            </div>
                            {editingRoutingCards ? (
                              <div className="mt-3">
                                <p className="text-sm font-semibold text-accent-primary">
                                  L1 · 상세 라우팅 설명
                                </p>
                                <textarea
                                  aria-label={`${card.folder_name} L1`}
                                  value={card.l1}
                                  maxLength={3500}
                                  disabled={manualCardRevision.isPending}
                                  onChange={(event) =>
                                    updateRoutingCardEdit(
                                      card.folder_id,
                                      'l1',
                                      event.target.value,
                                    )
                                  }
                                  className="mt-1 min-h-48 w-full rounded-md border border-border-button bg-bg-input p-3 text-sm text-text-primary disabled:opacity-50"
                                />
                                <p className="mt-1 text-sm text-text-secondary">
                                  {card.l1.length}/3500 ·{' '}
                                  {hasKorean(card.l1)
                                    ? '한국어 포함'
                                    : '한국어 필요'}
                                </p>
                              </div>
                            ) : (
                              <details className="mt-3">
                                <summary className="cursor-pointer text-sm font-semibold text-accent-primary">
                                  L1 · 상세 라우팅 설명 보기
                                </summary>
                                <p className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md bg-text-primary/5 p-3 text-sm text-text-secondary">
                                  {card.l1}
                                </p>
                              </details>
                            )}
                          </article>
                        ))}
                      </div>
                      {manualCardRevision.isError && (
                        <p className="mt-3 text-sm text-state-error">
                          {manualCardRevision.error.message}
                        </p>
                      )}
                      <div className="mt-3 flex gap-2">
                        <button
                          type="button"
                          onClick={closePublishReview}
                          disabled={
                            manualCardRevision.isPending || editingRoutingCards
                          }
                          className="rounded-lg border border-border-button px-4 py-2 text-sm"
                        >
                          검토 닫기
                        </button>
                        {editingRoutingCards && (
                          <>
                            <button
                              type="button"
                              onClick={cancelRoutingCardEdit}
                              disabled={manualCardRevision.isPending}
                              className="rounded-lg border border-border-button px-4 py-2 text-sm disabled:opacity-40"
                            >
                              수정 취소
                            </button>
                            <button
                              type="button"
                              onClick={saveRoutingCardEdits}
                              disabled={
                                manualCardRevision.isPending ||
                                !manualCardEditsValid
                              }
                              className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
                            >
                              {manualCardRevision.isPending
                                ? '저장 중'
                                : '저장'}
                            </button>
                          </>
                        )}
                        <button
                          type="button"
                          onClick={confirmPublish}
                          disabled={
                            publishVersion.isPending ||
                            manualCardRevision.isPending ||
                            editingRoutingCards
                          }
                          className="rounded-lg bg-state-success px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
                        >
                          {publishVersion.isPending
                            ? 'Publish 중'
                            : 'Publish 실행'}
                        </button>
                      </div>
                    </div>
                  )}
                </section>

                {workspaceView === 'library' && (
                  <>
                    {registrationQuery.isError && (
                      <p role="alert" className="text-sm text-state-error">
                        {registrationQuery.error.message}
                      </p>
                    )}
                    <RegistrationPanel
                      registrations={registrations}
                      folderNames={folderNames}
                      documentFolderNames={documentFolderNames}
                      hasDraft={Boolean(activeDraft)}
                      draftedDocumentIds={draftedDocumentIds}
                      retryPending={registrationRetry.isPending}
                      addPending={draftChange.isPending}
                      onRetry={retryRegistration}
                      onAdd={addRegistrationToDraft}
                      renderInspection={renderRegistrationInspection}
                    />
                  </>
                )}

                <section
                  hidden={workspaceView !== 'catalog'}
                  className="mt-5 rounded-xl border border-border-button p-4"
                  aria-label="Catalog 이력"
                >
                  <button
                    type="button"
                    onClick={toggleVersionHistory}
                    aria-expanded={showVersionHistory}
                    className="flex w-full items-center justify-between gap-3 text-left text-sm text-text-secondary hover:text-text-primary"
                  >
                    <span>
                      {showVersionHistory
                        ? '이전 버전·기록 닫기'
                        : `이전 버전·기록 ${historicalCatalogVersions.length}개`}
                    </span>
                    <span aria-hidden="true">
                      {showVersionHistory ? '−' : '+'}
                    </span>
                  </button>
                  {showVersionHistory && (
                    <p className="mt-2 text-xs text-text-secondary">
                      이전 초안, 실패 기록과 롤백 대상을 모았습니다. 기록을
                      접어도 삭제되지 않습니다.
                    </p>
                  )}
                  {versionQuery.isError && (
                    <p className="mt-2 text-sm text-state-error">
                      {versionQuery.error.message}
                    </p>
                  )}
                  {rollbackVersion.isError && (
                    <p className="mt-2 text-sm text-state-error">
                      {rollbackVersion.error.message}
                    </p>
                  )}
                  {deleteVersion.isError && (
                    <p className="mt-2 text-sm text-state-error">
                      {deleteVersion.error.message}
                    </p>
                  )}
                  <div className="mt-3 grid gap-2">
                    {visibleCatalogVersions.map((version) => (
                      <div
                        key={version.version_id}
                        className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border-button px-3 py-2"
                      >
                        <div>
                          <p className="text-sm font-medium">
                            {version.version_label}{' '}
                            {version.active
                              ? '· 활성'
                              : `· ${version.lifecycle_state}`}
                          </p>
                          <p className="text-xs text-text-secondary">
                            문서 {version.membership_count}개 ·{' '}
                            {version.health_state}
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          {version.rollback_allowed && activeVersion && (
                            <button
                              type="button"
                              onClick={() =>
                                rollbackToVersion(version.version_id)
                              }
                              disabled={rollbackVersion.isPending}
                              className="rounded-lg border border-state-warning/30 px-3 py-1 text-xs text-state-warning disabled:opacity-40"
                            >
                              이 버전으로 롤백
                            </button>
                          )}
                          {version.lifecycle_state === 'FAILED' &&
                            version.delete_allowed && (
                              <button
                                type="button"
                                onClick={() => deleteFailedVersion(version)}
                                disabled={deleteVersion.isPending}
                                className="rounded-lg border border-state-error/30 px-3 py-1 text-xs text-state-error disabled:opacity-40"
                              >
                                실패 이력 삭제
                              </button>
                            )}
                          {version.deletion_pending && (
                            <span className="rounded-lg border border-accent-primary/30 px-3 py-1 text-xs text-accent-primary">
                              삭제 중…
                            </span>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              </section>
            )}
            <div hidden={workspaceView !== 'search'}>
              {retrieval.isPending && (
                <p className="text-text-secondary">
                  관련 문서를 찾는 중입니다.
                </p>
              )}
              {!retrieval.isPending && chunks.length > 0 && (
                <>
                  <p className="mb-2 text-sm text-accent-primary">
                    {effectiveScopeLabel}
                  </p>
                  <p className="mb-1 text-sm text-text-secondary">
                    검색 문서 {chunks.length}개 중 {visibleChunks.length}개 표시
                  </p>
                  <ol>
                    {visibleChunks.map((chunk, index) => (
                      <li key={chunk.chunk_id}>
                        <RankedResult
                          chunk={chunk}
                          rank={index + 1}
                          datasetId={retrieval.data?.dataset_id}
                          onOpen={() => setSelectedChunk(chunk)}
                        />
                      </li>
                    ))}
                  </ol>
                  {remainingResultCount > 0 && (
                    <button
                      type="button"
                      onClick={() =>
                        setVisibleResultCount((count) =>
                          Math.min(count + resultsPerPage, chunks.length),
                        )
                      }
                      className="mt-6 w-full rounded-lg border border-border-button bg-bg-card px-4 py-3 text-sm font-medium text-text-primary hover:bg-text-primary/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
                    >
                      결과 {Math.min(resultsPerPage, remainingResultCount)}개 더
                      보기
                    </button>
                  )}
                </>
              )}
              {!retrieval.isPending &&
                retrieval.isSuccess &&
                chunks.length === 0 && (
                  <div className="pt-24 text-center text-text-secondary">
                    {effectiveScopeLabel && (
                      <p className="mb-2 text-sm text-accent-primary">
                        {effectiveScopeLabel}
                      </p>
                    )}
                    <p>일치하는 문서를 찾지 못했습니다.</p>
                    {retrieval.data?.scope_mode === 'manual' && (
                      <button
                        type="button"
                        onClick={retryAcrossAllFolders}
                        className="mt-4 rounded-lg border border-border-button bg-bg-card px-4 py-2 text-text-primary hover:bg-text-primary/5"
                      >
                        전체 폴더에서 다시 검색
                      </button>
                    )}
                  </div>
                )}
              {retrieval.isError && (
                <div className="pt-24 text-center text-state-error">
                  <p>
                    {staleFolderError
                      ? '폴더 목록이 변경되었습니다. 다시 선택해 주세요.'
                      : retrieval.error.message}
                  </p>
                  {staleFolderError && (
                    <button
                      type="button"
                      onClick={refreshStaleFolders}
                      className="mt-4 rounded-lg border border-border-button bg-bg-card px-4 py-2 text-text-primary hover:bg-text-primary/5"
                    >
                      폴더 목록 새로고침
                    </button>
                  )}
                </div>
              )}
              {!retrieval.isPending && !retrieval.isSuccess && (
                <div className="pt-28 text-center text-text-secondary">
                  <p className="text-xl text-text-primary">
                    문서 근거를 검색하세요.
                  </p>
                  <p className="mt-2">
                    답변 대신, 관련도 순으로 원문과 문서 위치를 보여드립니다.
                  </p>
                </div>
              )}
            </div>
          </section>
          {workspaceView === 'search' && (
            <DocumentModal
              chunk={selectedChunk}
              datasetId={retrieval.data?.dataset_id}
              onClose={() => setSelectedChunk(undefined)}
            />
          )}
        </main>
      </WorkspaceShell>
    </InspectionNavigation.Provider>
  );
}
