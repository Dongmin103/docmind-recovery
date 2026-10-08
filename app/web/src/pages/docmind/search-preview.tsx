import TemporaryOriginalPreview from '@/components/document-preview/temporary-original-preview';
import ChunkContent from '@/components/chunk-content';
import HighLightMarkdown from '@/components/highlight-markdown';
import { useGetChunkHighlights } from '@/hooks/use-document-request';
import type { IChunk, ITestingChunk } from '@/interfaces/database/dataset';
import { getSourceLocations } from '@/utils/source-location';
import { X } from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type SyntheticEvent, type KeyboardEvent } from 'react';

const CompactQuery = '(max-width: 1199px)';

export default function SearchPreview({ chunk, evidenceState, inspection, onClose, onChunkSetChanged }: {
  chunk?: ITestingChunk;
  evidenceState: 'pending' | 'error' | 'ready';
  inspection: ReactNode;
  onClose: () => void;
  onChunkSetChanged: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [compact, setCompact] = useState(() => window.matchMedia?.(CompactQuery).matches ?? false);
  const { highlights, setWidthAndHeight } = useGetChunkHighlights((chunk ?? {}) as IChunk);
  const open = Boolean(chunk);

  useEffect(() => {
    const media = window.matchMedia?.(CompactQuery);
    if (!media) return;
    const update = () => setCompact(media.matches);
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog || !open) return;
    // Promote the same viewer to the top layer on narrow screens. Keeping its
    // DOM mounted preserves an active original session when the viewport changes.
    const previousFocus = dialog.contains(document.activeElement)
      ? document.activeElement as HTMLElement : null;
    if (dialog.open) dialog.close();
    if (compact) dialog.showModal();
    else dialog.show();
    (previousFocus ?? closeRef.current)?.focus({ preventScroll: true });
    return () => dialog.close();
  }, [compact, open]);

  const cancel = (event: SyntheticEvent) => {
    event.preventDefault();
    onClose();
  };
  const handleKey = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    }
  };

  if (!chunk) return null;
  const name = chunk.docnm_kwd || chunk.doc_name || '';
  const location = getSourceLocations(chunk);
  const hasTable = /<table(?:\s[^>]*)?>/i.test(chunk.content_with_weight);
  return (
    <dialog ref={dialogRef} id="docmind-original-panel" className="docmind-original-panel"
      aria-labelledby="docmind-original-title" aria-modal={compact || undefined}
      onCancel={cancel} onKeyDown={handleKey}>
      <header className="docmind-original-header">
        <div>
          <h2 id="docmind-original-title">근거 · 원문</h2>
          <p className="docmind-original-name">{name}</p>
          {location !== '문서 위치' && <p className="docmind-result-meta">{location}</p>}
        </div>
        <button ref={closeRef} type="button" onClick={onClose} aria-label="원문 패널 닫기">
          <X size={20} />
        </button>
      </header>
      <div className="docmind-original-context">
        {evidenceState === 'ready' ? <details>
          <summary>검색된 내용</summary>
          <div className="docmind-original-excerpt">
            {hasTable ? <ChunkContent content={chunk.content_with_weight} html />
              : <HighLightMarkdown>{chunk.content_with_weight}</HighLightMarkdown>}
          </div>
        </details> : <p role="status" className="docmind-result-meta">
          {evidenceState === 'error'
            ? '검색된 내용을 갱신하지 못했습니다. 원문은 계속 확인할 수 있습니다.'
            : '검색된 내용을 갱신하고 있습니다.'}
        </p>}
        {inspection}
      </div>
      <div className="docmind-original-body">
        <TemporaryOriginalPreview
          key={`${chunk.doc_id}:${chunk.source_version_id ?? ''}`}
          documentId={chunk.doc_id}
          sourceFormat={name.split('.').pop()?.toLowerCase()}
          sourceVersionId={chunk.source_version_id}
          chunkSetId={chunk.chunk_set_id}
          onChunkSetChanged={onChunkSetChanged}
          className="docmind-document-preview !h-full min-h-0 overflow-auto border-none p-0"
          highlights={evidenceState === 'ready' ? highlights : []}
          setWidthAndHeight={setWidthAndHeight}
        />
      </div>
    </dialog>
  );
}
