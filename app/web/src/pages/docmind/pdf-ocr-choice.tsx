import { Modal } from '@/components/ui/modal/modal';
import type { DocMindHierarchyNode } from '@/services/docmind-service';
import { useRef, useState } from 'react';

type Props = {
  node: DocMindHierarchyNode;
  onConfirm: (documentId: string, versionId: string, key: string) => Promise<void>;
};

export function PdfOcrChoice({ node, onConfirm }: Props) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const inFlight = useRef(false);
  const [choice, setChoice] = useState<{
    documentId: string;
    versionId: string;
    option: 'zero_text' | 'partial_images';
    key: string;
  } | null>(null);
  const available = Boolean(node.document_id && node.index_pdf_ocr_option && node.index_pdf_ocr_version_id);
  if (!available && !open) return null;

  const start = () => {
    if (!node.document_id || !node.index_pdf_ocr_option || !node.index_pdf_ocr_version_id) return;
    setChoice({
      documentId: node.document_id,
      versionId: node.index_pdf_ocr_version_id,
      option: node.index_pdf_ocr_option,
      key: globalThis.crypto?.randomUUID?.() ?? `${node.file_id}-${Date.now()}`,
    });
    setError('');
    setOpen(true);
  };
  const close = () => {
    if (!inFlight.current) setOpen(false);
  };
  const confirm = async () => {
    if (inFlight.current || !choice) return;
    inFlight.current = true;
    setPending(true);
    setError('');
    try {
      await onConfirm(choice.documentId, choice.versionId, choice.key);
      setOpen(false);
    } catch {
      setError('OCR 요청을 시작하지 못했습니다. 다시 시도해 주세요.');
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  };

  return (
    <>
      {available && <button type="button" onClick={start} className="rounded border border-border-button px-2 py-1 text-xs">
        OCR 선택
      </button>}
      <Modal title="PDF 글자 인식" open={open} onCancel={close} showfooter={false} size="small">
        <p className="text-sm text-text-primary">
          {choice?.option === 'zero_text'
            ? '이 PDF에서 검색 가능한 글자를 찾지 못했습니다. OCR로 글자를 인식해 인덱싱하시겠습니까?'
            : '이 PDF의 일반 글자는 이미 검색 가능합니다. 이미지 속 글자가 있을 수 있습니다. OCR로 다시 인덱싱하시겠습니까?'}
        </p>
        {error && <p role="alert" className="mt-3 text-sm text-state-error">{error}</p>}
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={close} disabled={pending} className="rounded border border-border-button px-3 py-2 text-sm">
            나중에
          </button>
          <button type="button" onClick={() => void confirm()} disabled={pending} className="rounded bg-accent-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-40">
            OCR로 인덱싱
          </button>
        </div>
      </Modal>
    </>
  );
}
