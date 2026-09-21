import { fireEvent, render, screen } from '@testing-library/react';
import ChunkCard from './index';

jest.mock('@/components/image', () => ({
  __esModule: true,
  default: () => null,
}));

jest.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (value: string) => value }),
}));

test('shows parser identity and Office locator without inventing a PDF page', () => {
  render(
    <ChunkCard
      item={
        {
          available_int: 1,
          chunk_id: 'chunk-1',
          content_with_weight: '병합된 시험 결과 영역',
          doc_id: 'doc-1',
          doc_name: 'structured.xlsx',
          image_id: '',
          positions: [],
          parser_platform: {
            parser_name: 'docling',
            parser_version: '2.115.0',
          },
          office_locator: {
            kind: 'xlsx',
            sheet: 'Sheet-B',
            cell_range: 'B3:F3',
          },
        } as any
      }
      checked={false}
      switchChunk={jest.fn()}
      editChunk={jest.fn()}
      handleCheckboxClick={jest.fn()}
      selected={false}
      clickChunkCard={jest.fn()}
      textMode={'full' as any}
    />,
  );

  expect(screen.getByText('docling 2.115.0')).toBeInTheDocument();
  expect(screen.getByText('Sheet-B!B3:F3')).toBeInTheDocument();
  expect(screen.queryByText(/p\. /)).toBeNull();
});

test('renders HWP table display HTML without replacing searchable text', () => {
  render(
    <ChunkCard
      item={
        {
          available_int: 1,
          chunk_id: 'chunk-hwp-table',
          content_with_weight: '검색용 표 텍스트',
          display_html:
            '<table><thead><tr><th>시험</th></tr></thead><tbody><tr><td>적합</td></tr></tbody></table>',
          doc_id: 'doc-1',
          doc_name: 'document.hwpx',
          image_id: '',
          positions: [],
          parser_platform: {
            parser_name: 'rhwp',
            parser_version: '0.8.1',
          },
        } as any
      }
      checked={false}
      switchChunk={jest.fn()}
      editChunk={jest.fn()}
      handleCheckboxClick={jest.fn()}
      selected={false}
      clickChunkCard={jest.fn()}
      textMode={'full' as any}
    />,
  );

  expect(screen.getByRole('table')).toBeInTheDocument();
  expect(screen.getByText('시험')).toBeInTheDocument();
  expect(screen.getByText('적합')).toBeInTheDocument();
  expect(screen.queryByText('검색용 표 텍스트')).toBeNull();
});

test('read-only cards keep selection highlights without edit or mutation controls', () => {
  const editChunk = jest.fn();
  const clickChunkCard = jest.fn();

  render(
    <ChunkCard
      item={
        {
          available_int: 1,
          chunk_id: 'chunk-read-only',
          content_with_weight: '검사 가능한 청크',
          doc_id: 'doc-1',
          doc_name: 'document.pdf',
          image_id: '',
          positions: [],
        } as any
      }
      checked={false}
      switchChunk={jest.fn()}
      editChunk={editChunk}
      handleCheckboxClick={jest.fn()}
      selected={false}
      clickChunkCard={clickChunkCard}
      textMode={'full' as any}
      readOnly
    />,
  );

  const content = screen.getByText('검사 가능한 청크');
  fireEvent.click(content);
  fireEvent.doubleClick(content);

  expect(clickChunkCard).toHaveBeenCalled();
  expect(editChunk).not.toHaveBeenCalled();
  expect(screen.queryByRole('checkbox')).toBeNull();
});
