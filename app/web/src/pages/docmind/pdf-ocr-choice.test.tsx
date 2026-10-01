import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { DocMindHierarchyNode } from '@/services/docmind-service';
import { PdfOcrChoice } from './pdf-ocr-choice';

const node: DocMindHierarchyNode = {
  file_id: 'file-1', document_id: 'document-1', name: 'scan.pdf',
  relative_path: 'scan.pdf', depth: 1, type: 'file', document_exists: true,
  index_state: 'FAILED', index_pdf_ocr_option: 'zero_text',
  index_pdf_ocr_version_id: 'version-1',
};

it('offers a real consent choice and leaves the document unchanged when deferred', async () => {
  const onConfirm = jest.fn().mockResolvedValue(undefined);
  render(<PdfOcrChoice node={node} onConfirm={onConfirm} />);
  fireEvent.click(screen.getByRole('button', { name: 'OCR 선택' }));
  expect(screen.getByText(/검색 가능한 글자를 찾지 못했습니다/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '나중에' }));
  expect(onConfirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'OCR 선택' }));
  fireEvent.click(screen.getByRole('button', { name: 'OCR로 인덱싱' }));
  await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
  expect(onConfirm.mock.calls[0].slice(0, 2)).toEqual(['document-1', 'version-1']);
});

it('offers OCR for partially indexed images but never for unrelated failures', () => {
  const onConfirm = jest.fn();
  const { rerender } = render(<PdfOcrChoice node={{ ...node, index_state: 'INDEXED', index_pdf_ocr_option: 'partial_images' }} onConfirm={onConfirm} />);
  fireEvent.click(screen.getByRole('button', { name: 'OCR 선택' }));
  expect(screen.getByText(/일반 글자는 이미 검색 가능합니다/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '나중에' }));
  rerender(<PdfOcrChoice node={{ ...node, index_pdf_ocr_option: null }} onConfirm={onConfirm} />);
  expect(screen.queryByRole('button', { name: 'OCR 선택' })).not.toBeInTheDocument();
  expect(onConfirm).not.toHaveBeenCalled();
});

it('holds the consented version when the hierarchy changes and ignores a second click', async () => {
  let finish!: () => void;
  const onConfirm = jest.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  const { rerender } = render(<PdfOcrChoice node={node} onConfirm={onConfirm} />);
  fireEvent.click(screen.getByRole('button', { name: 'OCR 선택' }));
  rerender(<PdfOcrChoice node={{ ...node, index_pdf_ocr_version_id: 'version-2' }} onConfirm={onConfirm} />);
  const submit = screen.getByRole('button', { name: 'OCR로 인덱싱' });
  fireEvent.click(submit);
  fireEvent.click(submit);
  expect(onConfirm).toHaveBeenCalledTimes(1);
  expect(onConfirm.mock.calls[0].slice(0, 2)).toEqual(['document-1', 'version-1']);
  finish();
  await waitFor(() => expect(submit).not.toBeVisible());
});

it('retries a failed request with the same consent identity', async () => {
  const onConfirm = jest.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce(undefined);
  render(<PdfOcrChoice node={node} onConfirm={onConfirm} />);
  fireEvent.click(screen.getByRole('button', { name: 'OCR 선택' }));
  fireEvent.click(screen.getByRole('button', { name: 'OCR로 인덱싱' }));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'OCR로 인덱싱' }));
  await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(2));
  expect(onConfirm.mock.calls[0]).toEqual(onConfirm.mock.calls[1]);
});
