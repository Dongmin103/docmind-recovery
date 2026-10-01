import DocumentPreview from '@/components/document-preview';
import type { CreatedPreview, PreviewSession } from '@/interfaces/database/docmind-preview';
import {
  closePreview,
  createPreview,
  fetchPreviewBlob,
  getPreview,
  heartbeatPreview,
  PreviewRequestError,
} from '@/services/docmind-preview-service';
import { ChevronLeft, ChevronRight, ZoomIn, ZoomOut } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { IHighlight } from 'react-pdf-highlighter';

interface Props {
  documentId: string;
  sourceFormat?: string;
  sourceVersionId?: string;
  chunkSetId?: string;
  highlights?: IHighlight[];
  setWidthAndHeight?: (width: number, height: number) => void;
  className?: string;
  onChunkSetChanged?: () => void;
}

const TerminalStatuses = new Set([
  'FAILED',
  'CANCELLED',
  'EXPIRED',
  'CLEANUP_FAILED',
]);
const StageLabels: Record<string, string> = {
  QUEUED: '미리보기 대기 중입니다. 최대 120초 동안 기다립니다.',
  DECRYPTING: '원문 복호화 중입니다.',
  PROCESSING: '페이지 준비 중입니다.',
  READY: '원문을 불러오는 중입니다.',
};

function previewError(error: unknown): string {
  if (error instanceof PreviewRequestError) {
    const messages: Record<string, string> = {
      PREVIEW_DISABLED: '원문 미리보기 기능이 비활성화되어 있습니다.',
      PREVIEW_FORMAT_UNSUPPORTED: '이 형식은 원문 미리보기를 지원하지 않습니다.',
      PREVIEW_QUEUE_FULL: '미리보기 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요.',
      PREVIEW_QUEUE_TIMEOUT: '미리보기 대기 시간이 120초를 초과했습니다. 다시 시도해 주세요.',
      PREVIEW_PROCESS_TIMEOUT: '페이지 준비 시간이 60초를 초과했습니다.',
      PREVIEW_PROCESSOR_FAILED: '한글 문서 표시 서비스에 연결할 수 없거나 처리가 중단되었습니다.',
      PREVIEW_PROCESSING_FAILED: '문서 표시 중 오류가 발생했습니다. 다시 시도해 주세요.',
      PREVIEW_INPUT_TOO_LARGE: '원파일이 미리보기 크기 제한인 64MiB를 초과했습니다.',
      PREVIEW_DERIVED_TOO_LARGE: '페이지가 미리보기 용량 제한을 초과했습니다.',
      PREVIEW_SVG_TOO_LARGE: '페이지가 미리보기 용량 제한을 초과했습니다.',
      PREVIEW_CAPACITY_EXCEEDED: '미리보기 임시 공간이 부족합니다. 다른 원문을 닫고 다시 시도해 주세요.',
      PREVIEW_TMPFS_UNAVAILABLE: '미리보기 임시 공간을 사용할 수 없습니다.',
      PREVIEW_READER_BUSY: '이전 페이지를 전달 중입니다. 잠시 후 다시 시도해 주세요.',
      PREVIEW_PROCESSOR_BUSY: '다른 페이지를 준비 중입니다. 잠시 후 다시 시도해 주세요.',
    };
    if (messages[error.code]) return messages[error.code];
    if (error.code === 'SOURCE_VERSION_CHANGED') {
      return '원본이 변경되었습니다. 다시 검색한 뒤 원문을 열어 주세요.';
    }
    if (error.code === 'CHUNK_SET_CHANGED') {
      return '청크 목록이 갱신되었습니다. 새 목록에서 원문을 다시 열어 주세요.';
    }
    if (error.status === 410 || error.code === 'EXPIRED' || error.code === 'PREVIEW_EXPIRED') {
      return '미리보기 시간이 만료되었습니다. 다시 시도해 주세요.';
    }
    if (error.status === 403) return '원문 열람 권한이 없습니다.';
    if (error.status === 429) {
      return '미리보기 작업이 많습니다. 잠시 후 다시 시도해 주세요.';
    }
    return `원문을 열지 못했습니다. (${error.code})`;
  }
  return '원문 연결이 끊겼습니다. 다시 시도해 주세요.';
}

function HwpPageViewer({
  session,
  token,
}: {
  session: PreviewSession;
  token: string;
}) {
  const [page, setPage] = useState(1);
  const [attempt, setAttempt] = useState(0);
  const [zoom, setZoom] = useState(100);
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  const pageCount = session.page_count ?? 1;
  useEffect(() => {
    const controller = new AbortController();
    let currentUrl = '';
    void fetchPreviewBlob(session.preview_id, token, controller.signal, page)
      .then((blob) => {
        if (controller.signal.aborted) return;
        currentUrl = URL.createObjectURL(blob);
        setUrl(currentUrl);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(previewError(cause));
      });
    return () => {
      controller.abort();
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [attempt, page, session.preview_id, token]);

  const previousPage = () => {
    setUrl('');
    setError('');
    setPage((current) => Math.max(1, current - 1));
  };
  const nextPage = () => {
    setUrl('');
    setError('');
    setPage((current) => Math.min(pageCount, current + 1));
  };
  const zoomOut = () => setZoom((current) => Math.max(50, current - 25));
  const zoomIn = () => setZoom((current) => Math.min(200, current + 25));
  const retryPage = () => {
    setError('');
    setAttempt((current) => current + 1);
  };
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-center gap-3 border-b border-border-button py-2">
        <button type="button" onClick={previousPage} disabled={page <= 1} aria-label="이전 쪽">
          <ChevronLeft size={18} />
        </button>
        <span className="text-sm">{page} / {pageCount}</span>
        <button type="button" onClick={nextPage} disabled={page >= pageCount} aria-label="다음 쪽">
          <ChevronRight size={18} />
        </button>
        <button type="button" onClick={zoomOut} disabled={zoom <= 50} aria-label="축소">
          <ZoomOut size={18} />
        </button>
        <span className="text-sm">{zoom}%</span>
        <button type="button" onClick={zoomIn} disabled={zoom >= 200} aria-label="확대">
          <ZoomIn size={18} />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto bg-bg-base p-3 text-center">
        {error ? (
          <div role="alert">
            <p>{error}</p>
            <button type="button" onClick={retryPage} className="mt-2 rounded-md border border-border-button px-3 py-1">
              다시 시도
            </button>
          </div>
        ) : url ? (
          <img
            src={url}
            alt={`한글 문서 ${page}쪽`}
            className="mx-auto h-auto max-w-none"
            style={{ width: `${zoom}%` }}
          />
        ) : <p role="status">쪽을 불러오는 중입니다.</p>}
      </div>
    </div>
  );
}

export default function TemporaryOriginalPreview({
  documentId,
  sourceFormat,
  sourceVersionId,
  chunkSetId,
  highlights,
  setWidthAndHeight,
  className,
  onChunkSetChanged,
}: Props) {
  const [session, setSession] = useState<CreatedPreview | null>(null);
  const [contentUrl, setContentUrl] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const sessionRef = useRef<CreatedPreview | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const contentUrlRef = useRef('');
  const sequenceRef = useRef(0);
  const chunkSetRef = useRef(chunkSetId);
  const identityRef = useRef({ documentId, sourceVersionId });
  const onChunkSetChangedRef = useRef(onChunkSetChanged);
  useEffect(() => {
    onChunkSetChangedRef.current = onChunkSetChanged;
  }, [onChunkSetChanged]);

  const release = useCallback(() => {
    sequenceRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    if (contentUrlRef.current) URL.revokeObjectURL(contentUrlRef.current);
    contentUrlRef.current = '';
    const previous = sessionRef.current;
    sessionRef.current = null;
    if (previous) {
      void closePreview(
        previous.preview_id,
        previous.preview_token,
        new AbortController().signal,
      ).catch(() => undefined);
    }
  }, []);

  useEffect(() => release, [release]);
  useEffect(() => {
    window.addEventListener('pagehide', release);
    return () => window.removeEventListener('pagehide', release);
  }, [release]);
  useEffect(() => {
    if (
      identityRef.current.documentId !== documentId ||
      identityRef.current.sourceVersionId !== sourceVersionId
    ) {
      identityRef.current = { documentId, sourceVersionId };
      release();
      setSession(null);
      setContentUrl('');
      setError('');
      setBusy(false);
    }
  }, [documentId, sourceVersionId, release]);
  useEffect(() => {
    chunkSetRef.current = chunkSetId;
  }, [chunkSetId]);

  const start = useCallback(() => {
    if (!sourceVersionId || !chunkSetId || busy || ['doc', 'ppt'].includes(sourceFormat?.toLowerCase() ?? '')) return;
    release();
    setSession(null);
    setContentUrl('');
    setError('');
    setBusy(true);
    const controller = new AbortController();
    controllerRef.current = controller;
    const sequence = sequenceRef.current;
    const isCurrent = () =>
      !controller.signal.aborted && sequenceRef.current === sequence;
    const fail = (cause: unknown) => {
      if (!isCurrent()) return;
      if (cause instanceof PreviewRequestError && cause.code === 'CHUNK_SET_CHANGED') {
        onChunkSetChangedRef.current?.();
      }
      release();
      setBusy(false);
      setError(previewError(cause));
    };
    const acceptStatus = (next: PreviewSession) => {
      if (!isCurrent()) return false;
      if (next.source_version_id !== sourceVersionId) {
        fail(new PreviewRequestError('SOURCE_VERSION_CHANGED', 409));
        return false;
      }
      if (next.chunk_set_id && next.chunk_set_id !== chunkSetRef.current) {
        chunkSetRef.current = next.chunk_set_id;
        onChunkSetChangedRef.current?.();
      }
      if (TerminalStatuses.has(next.status)) {
        fail(new PreviewRequestError(next.error_code || next.status, 500));
        return false;
      }
      setSession((current) => current ? { ...current, ...next } : current);
      return true;
    };
    void (async () => {
      let created: CreatedPreview;
      try {
        created = await createPreview(
          documentId,
          sourceVersionId,
          chunkSetId,
          globalThis.crypto?.randomUUID?.() ??
            `preview-${documentId}-${Date.now()}`,
          controller.signal,
        );
      } catch (cause) {
        fail(cause);
        return;
      }
      if (!isCurrent()) {
        void closePreview(created.preview_id, created.preview_token, new AbortController().signal).catch(() => undefined);
        return;
      }
      sessionRef.current = created;
      setSession(created);
      const poll = async (initial: PreviewSession) => {
        let current = initial;
        while (isCurrent()) {
          if (!acceptStatus(current)) return;
          if (current.status === 'READY') {
            if (current.viewer_kind === 'hwp') {
              setBusy(false);
              return;
            }
            try {
              const blob = await fetchPreviewBlob(
                created.preview_id,
                created.preview_token,
                controller.signal,
              );
              if (!isCurrent()) return;
              // Native viewers read this local URL. The only server request
              // uses the session token and this abortable controller.
              const url = URL.createObjectURL(blob);
              contentUrlRef.current = url;
              setContentUrl(url);
              setBusy(false);
            } catch (cause) {
              fail(cause);
            }
            return;
          }
          await new Promise<void>((resolve) => {
            const timer = window.setTimeout(resolve, 2000);
            controller.signal.addEventListener('abort', () => {
              window.clearTimeout(timer);
              resolve();
            }, { once: true });
          });
          if (!isCurrent()) return;
          try {
            current = await getPreview(
              created.preview_id,
              created.preview_token,
              controller.signal,
            );
          } catch (cause) {
            fail(cause);
            return;
          }
        }
      };
      void poll(created);
      const heartbeat = window.setInterval(() => {
        if (!isCurrent()) {
          window.clearInterval(heartbeat);
          return;
        }
        void heartbeatPreview(
          created.preview_id,
          created.preview_token,
          controller.signal,
        ).then(acceptStatus).catch(fail);
      }, 30000);
      controller.signal.addEventListener(
        'abort',
        () => window.clearInterval(heartbeat),
        { once: true },
      );
    })();
  }, [busy, chunkSetId, documentId, release, sourceVersionId, sourceFormat]);

  const stop = () => {
    release();
    setSession(null);
    setContentUrl('');
    setBusy(false);
    setError('');
  };

  const ready = session?.status === 'READY' && !error;
  const displayFormat = session?.display_format?.toLowerCase() ?? '';
  const safeHighlights =
    session?.chunk_set_id && session.chunk_set_id !== chunkSetId
      ? []
      : highlights;
  const canStart = Boolean(sourceVersionId && chunkSetId);
  if (['doc', 'ppt'].includes(sourceFormat?.toLowerCase() ?? '')) {
    return <p role="status" className="p-5 text-sm text-text-secondary">이 형식은 원문 미리보기를 지원하지 않습니다.</p>;
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      {!session || error ? (
        <div className="flex h-full min-h-48 flex-col items-center justify-center gap-3 p-4 text-center">
          {error && <p role="alert" className="text-state-error">{error}</p>}
          {!canStart && (
            <p className="text-sm text-text-secondary">
              원본 버전 정보가 없습니다. 문서를 다시 검색해 주세요.
            </p>
          )}
          <button
            type="button"
            onClick={start}
            disabled={!canStart || busy}
            className="rounded-md bg-accent-primary px-4 py-2 text-primary-foreground disabled:opacity-40"
          >
            {busy ? '원문 요청 중' : error ? '원문 다시 보기' : '원문 보기'}
          </button>
        </div>
      ) : (
        <>
          <div className="flex shrink-0 justify-end border-b border-border-button px-3 py-2">
            <button
              type="button"
              onClick={stop}
              className="rounded-md border border-border-button px-3 py-1 text-sm text-text-secondary"
            >
              {ready ? '원문 닫기' : '취소'}
            </button>
          </div>
          {!ready || (session.viewer_kind !== 'hwp' && !contentUrl) ? (
            <p role="status" className="p-5 text-sm text-text-secondary">
              {StageLabels[session.status] ?? '원문을 준비하는 중입니다.'}
            </p>
          ) : session.viewer_kind === 'hwp' ? (
            <HwpPageViewer key={session.preview_id} session={session} token={session.preview_token} />
          ) : (
            <div className="min-h-0 flex-1 overflow-hidden [&>section]:h-full [&>section]:min-h-0">
              <DocumentPreview
                className={className}
                fileType={displayFormat}
                highlights={safeHighlights}
                setWidthAndHeight={setWidthAndHeight}
                url={contentUrl}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}
