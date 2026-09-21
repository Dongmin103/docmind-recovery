import { render, screen } from '@testing-library/react';

jest.mock('../csv-preview', () => () => null);
jest.mock('../doc-preview', () => ({ DocPreviewer: () => null }));
jest.mock('../image-preview', () => ({ ImagePreviewer: () => null }));
jest.mock('../md', () => ({ Md: () => null }));
jest.mock('../pdf-preview', () => () => null);
jest.mock('../ppt-preview', () => ({ PptPreviewer: () => null }));
jest.mock('../txt-preview', () => ({ TxtPreviewer: () => null }));
jest.mock('../video-preview', () => ({ VideoPreviewer: () => null }));
jest.mock('../excel-preview', () => ({
  ExcelCsvPreviewer: ({ url, fileType }: { url: string; fileType: string }) => {
    const ReactLib = jest.requireActual('react');
    return ReactLib.createElement('div', {
      'data-testid': 'excel-preview',
      'data-type': fileType,
      'data-url': url,
    });
  },
}));
import DocumentPreview from '../index';

test.each(['xls', 'xlsx'])(
  'renders %s source using the Excel viewer and passes metadata',
  (fileType) => {
    render(
      <DocumentPreview
        fileType={fileType}
        url="/api/v1/documents/opaque-id/preview"
      />,
    );
    const viewer = screen.getByTestId('excel-preview');
    expect(viewer).toHaveAttribute('data-type', fileType);
    expect(viewer).toHaveAttribute(
      'data-url',
      '/api/v1/documents/opaque-id/preview',
    );
  },
);
