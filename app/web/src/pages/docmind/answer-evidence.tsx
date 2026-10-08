import type { DocMindAnswerResult } from '@/services/docmind-service';
import { buildDocMindDocumentPath } from '@/utils/docmind-workspace';
import { X } from 'lucide-react';
import {
  type KeyboardEvent,
  type ReactNode,
  useId,
  useLayoutEffect,
  useRef,
} from 'react';
import AnswerEvidenceContent from './answer-evidence-content';

type AnswerCitation = DocMindAnswerResult['citations'][number];

export default function AnswerEvidence({
  citation,
  selectionId,
  datasetId,
  onClose,
  renderInspection,
}: {
  citation?: AnswerCitation;
  selectionId?: number;
  datasetId?: string;
  onClose: () => void;
  renderInspection?: (datasetId: string, documentId: string) => ReactNode;
}) {
  const headingId = useId();
  const evidenceRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const evidence = evidenceRef.current;
    if (!citation || !evidence) return;
    evidence.scrollTop = 0;
    const viewport = evidence.closest<HTMLElement>(
      '[data-docmind-search-scroll]',
    );
    if (!viewport) return;
    const bounds = evidence.getBoundingClientRect();
    const visible = viewport.getBoundingClientRect();
    const composerBounds = viewport
      .querySelector<HTMLElement>('[data-docmind-composer]')
      ?.getBoundingClientRect();
    const visibleBottom =
      composerBounds &&
      composerBounds.height > 0 &&
      composerBounds.top > visible.top
        ? Math.min(visible.bottom, composerBounds.top)
        : visible.bottom;
    if (bounds.top < visible.top + 16 || bounds.top > visibleBottom - 128) {
      viewport.scrollTo({
        top: Math.max(0, viewport.scrollTop + bounds.top - visible.top - 16),
        behavior: 'instant',
      });
    }
  }, [citation, selectionId]);
  const closeEvidence = () => {
    onClose();
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    event.stopPropagation();
    closeEvidence();
  };
  const path = buildDocMindDocumentPath(datasetId, citation?.doc_id);
  if (!citation) return null;
  return (
    <aside
      ref={evidenceRef}
      aria-labelledby={headingId}
      onKeyDown={handleKeyDown}
      className="docmind-answer-evidence min-w-0 text-text-primary"
    >
      <div className="docmind-evidence-header">
        <div className="min-w-0">
          <p className="mb-1 text-xs text-text-secondary">
            인용 근거 [{citation?.number}]
          </p>
          <h2 id={headingId} className="break-words text-lg font-semibold">
            {citation?.document_name}
          </h2>
          {citation.document_relative_path && <p className="docmind-evidence-path">{citation.document_relative_path}</p>}
        </div>
        <button
          type="button"
          onClick={closeEvidence}
          aria-label="근거 닫기"
          className="shrink-0 rounded-lg p-2 hover:bg-bg-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
        >
          <X className="size-5" />
        </button>
      </div>
      <div className="docmind-evidence-body">
        <AnswerEvidenceContent citation={citation} datasetId={datasetId} />
      </div>
      {citation && path && (
        <div className="docmind-evidence-footer">
          {renderInspection && datasetId ? (
            renderInspection(datasetId, citation.doc_id)
          ) : (
            <a
              href={path}
              className="rounded-lg border border-border-button px-3 py-2 text-sm text-accent-primary"
            >
              원문 · 청크 확인
            </a>
          )}
        </div>
      )}
    </aside>
  );
}
