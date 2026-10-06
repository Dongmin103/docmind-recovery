import type { DocMindAnswerResult } from '@/services/docmind-service';
import { buildDocMindDocumentPath } from '@/utils/docmind-workspace';
import * as Dialog from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import { type ReactNode, useRef } from 'react';
import AnswerMarkdown from './answer-markdown';

type AnswerCitation = DocMindAnswerResult['citations'][number];

export default function AnswerEvidence({
  citation,
  datasetId,
  onClose,
  trigger,
  renderInspection,
}: {
  citation?: AnswerCitation;
  datasetId?: string;
  onClose: () => void;
  trigger: HTMLButtonElement | null;
  renderInspection?: (datasetId: string, documentId: string) => ReactNode;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const handleOpenChange = (open: boolean) => {
    if (!open) onClose();
  };
  const focusClose = (event: Event) => {
    event.preventDefault();
    closeRef.current?.focus();
  };
  const restoreFocus = (event: Event) => {
    event.preventDefault();
    if (trigger?.isConnected) trigger.focus({ preventScroll: true });
  };
  const path = buildDocMindDocumentPath(datasetId, citation?.doc_id);
  return (
    <Dialog.Root open={Boolean(citation)} onOpenChange={handleOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-bg-base/70" />
        <Dialog.Content
          onOpenAutoFocus={focusClose}
          onCloseAutoFocus={restoreFocus}
          className="fixed inset-y-0 right-0 z-50 flex w-full min-w-0 flex-col border-l border-border-button bg-bg-card text-text-primary shadow-2xl sm:max-w-xl"
        >
          <div className="flex shrink-0 items-start justify-between gap-3 border-b border-border-button p-5">
            <div className="min-w-0">
              <p className="mb-1 text-xs text-text-secondary">
                인용 근거 [{citation?.number}]
              </p>
              <Dialog.Title className="break-words text-lg font-semibold">
                {citation?.document_name}
              </Dialog.Title>
              <Dialog.Description className="mt-1 break-all text-sm text-text-secondary">
                {citation?.document_relative_path || '반환된 문서 근거'}
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button
                ref={closeRef}
                type="button"
                aria-label="근거 닫기"
                className="shrink-0 rounded-lg p-2 hover:bg-bg-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
              >
                <X className="size-5" />
              </button>
            </Dialog.Close>
          </div>
          <div className="min-h-0 min-w-0 flex-1 overflow-y-auto p-5">
            {citation && <AnswerMarkdown content={citation.content} />}
          </div>
          {citation && path && (
            <div className="shrink-0 border-t border-border-button p-5">
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
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
