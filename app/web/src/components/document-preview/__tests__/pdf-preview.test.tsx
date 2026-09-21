import { act, render } from '@testing-library/react';
import * as React from 'react';

let mockHighlighterProps: any;
let mockLoaderProps: any;
let mockLayerReady = true;

jest.mock('react-pdf-highlighter', () => {
  const ReactLib = jest.requireActual('react');

  return {
    PdfLoader: ({ children, ...props }: any) => {
      mockLoaderProps = props;
      return ReactLib.createElement(
        'div',
        { 'data-testid': 'pdf-loader' },
        children({
          getPage: () =>
            Promise.resolve({
              getViewport: () => ({ width: 600, height: 800 }),
            }),
        }),
      );
    },
    PdfHighlighter: (props: any) => {
      mockHighlighterProps = props;
      return ReactLib.createElement(
        'div',
        { 'data-testid': 'pdf-highlighter' },
        mockLayerReady
          ? ReactLib.createElement('div', {
              className: 'PdfHighlighter__highlight-layer',
            })
          : null,
      );
    },
    Highlight: () => null,
    AreaHighlight: () => null,
    Popup: ({ children }: any) => children,
  };
});

jest.mock('../hooks', () => ({
  useCatchDocumentError: () => '',
}));

jest.mock('@/utils/authorization-util', () => ({
  getAuthorization: () => 'Bearer test-token',
}));

import { PdfPreview } from '../pdf-preview';

const createHighlight = (pageNumber = 3, x1 = 12) =>
  ({
    id: `generated-${Math.random()}`,
    comment: { text: '', emoji: '' },
    content: { text: 'matching passage' },
    position: {
      pageNumber,
      boundingRect: {
        width: 600,
        height: 800,
        x1,
        x2: x1 + 36,
        y1: 70,
        y2: 112,
      },
      rects: [],
    },
  }) as any;

const preview = (url: string, highlights: any[]) =>
  React.createElement(PdfPreview, { url, highlights });

const createScrollTo = () =>
  jest.fn(() => {
    const layer = document.querySelector('.PdfHighlighter__highlight-layer');
    if (layer) {
      layer.innerHTML =
        '<div class="Highlight--scrolledTo"><div class="Highlight__part"></div></div>';
    }
  });

const flushPendingFrames = async () => {
  await act(async () => {
    await Promise.resolve();
    jest.runOnlyPendingTimers();
    await Promise.resolve();
  });
};

describe('PdfPreview', () => {
  beforeEach(() => {
    mockHighlighterProps = undefined;
    mockLoaderProps = undefined;
    mockLayerReady = true;
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('scrolls after a delayed highlighter scroll ref becomes ready', async () => {
    const scrollTo = createScrollTo();
    render(preview('/api/v1/documents/document-1/preview', [createHighlight()]));

    expect(scrollTo).not.toHaveBeenCalled();

    act(() => {
      jest.advanceTimersByTime(1000);
      mockHighlighterProps.scrollRef(scrollTo);
      jest.runOnlyPendingTimers();
    });
    await flushPendingFrames();

    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect(scrollTo).toHaveBeenCalledWith(
      expect.objectContaining({ position: expect.objectContaining({ pageNumber: 3 }) }),
    );
  });

  it('retries after the highlight layer appears during a cold load', async () => {
    mockLayerReady = false;
    const scrollTo = createScrollTo();
    const highlights = [createHighlight()];
    render(preview('/api/v1/documents/document-1/preview', highlights));

    act(() => {
      mockHighlighterProps.scrollRef(scrollTo);
      jest.runOnlyPendingTimers();
    });
    expect(scrollTo).not.toHaveBeenCalled();

    const layer = document.createElement('div');
    layer.className = 'PdfHighlighter__highlight-layer';
    await act(async () => {
      document.querySelector('[data-testid="pdf-highlighter"]')?.append(layer);
      await Promise.resolve();
    });
    await flushPendingFrames();

    expect(scrollTo).toHaveBeenCalledTimes(1);
  });

  it('cancels a queued scroll when the viewer unmounts', () => {
    mockLayerReady = false;
    const scrollTo = createScrollTo();
    const { unmount } = render(
      preview('/api/v1/documents/document-1/preview', [createHighlight()]),
    );

    act(() => {
      mockHighlighterProps.scrollRef(scrollTo);
      unmount();
      jest.runOnlyPendingTimers();
    });

    expect(scrollTo).not.toHaveBeenCalled();
  });

  it('does not scroll again when regenerated highlights have the same coordinates', async () => {
    const scrollTo = createScrollTo();
    const firstHighlight = createHighlight();
    const { rerender } = render(
      preview('/api/v1/documents/document-1/preview', [firstHighlight]),
    );

    act(() => {
      mockHighlighterProps.scrollRef(scrollTo);
      jest.runOnlyPendingTimers();
    });
    await flushPendingFrames();
    expect(scrollTo).toHaveBeenCalledTimes(1);

    rerender(preview('/api/v1/documents/document-1/preview', [createHighlight()]));

    expect(scrollTo).toHaveBeenCalledTimes(1);
  });

  it('scrolls once for a new URL or first-highlight coordinate signature', async () => {
    const scrollTo = createScrollTo();
    const { rerender } = render(
      preview('/api/v1/documents/document-1/preview', [createHighlight()]),
    );

    act(() => {
      mockHighlighterProps.scrollRef(scrollTo);
      jest.runOnlyPendingTimers();
    });
    await flushPendingFrames();
    expect(scrollTo).toHaveBeenCalledTimes(1);

    rerender(
      preview('/api/v1/documents/document-1/preview', [createHighlight(3, 40)]),
    );
    act(() => {
      jest.runOnlyPendingTimers();
    });
    await flushPendingFrames();
    expect(scrollTo).toHaveBeenCalledTimes(2);

    rerender(
      preview('/api/v1/documents/document-2/preview', [createHighlight(3, 40)]),
    );

    act(() => {
      mockHighlighterProps.scrollRef(scrollTo);
      jest.runOnlyPendingTimers();
    });
    await flushPendingFrames();
    expect(scrollTo).toHaveBeenCalledTimes(3);
  });

  it('preserves the document preview request contract', () => {
    render(preview('/api/v1/documents/document-1/preview', [createHighlight()]));

    expect(mockLoaderProps).toEqual(
      expect.objectContaining({
        url: '/api/v1/documents/document-1/preview',
        httpHeaders: expect.objectContaining({ Authorization: 'Bearer test-token' }),
        workerSrc: '/pdfjs-dist/pdf.worker.min.js',
      }),
    );
  });
});
