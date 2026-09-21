/*
 *  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */

import React, { memo, useCallback, useEffect, useRef } from 'react';
import {
  AreaHighlight,
  Highlight,
  IHighlight,
  PdfHighlighter,
  PdfLoader,
  Popup,
} from 'react-pdf-highlighter';

import { Spin } from '@/components/ui/spin';
// import FileError from '@/pages/document-viewer/file-error';
import { Authorization } from '@/constants/authorization';
import { cn } from '@/lib/utils';
import FileError from '@/pages/document-viewer/file-error';
import { getAuthorization } from '@/utils/authorization-util';
import { useCatchDocumentError } from './hooks';
type PdfLoaderProps = React.ComponentProps<typeof PdfLoader> & {
  httpHeaders?: Record<string, string>;
  standardFontDataUrl?: string;
};

const Loader = PdfLoader as React.ComponentType<PdfLoaderProps>;
export interface IProps {
  highlights?: IHighlight[];
  setWidthAndHeight?: (width: number, height: number) => void;
  url: string;
  className?: string;
}
const HighlightPopup = ({
  comment,
}: {
  comment: { text: string; emoji: string };
}) =>
  comment.text ? (
    <div className="Highlight__popup">
      {comment.emoji} {comment.text}
    </div>
  ) : null;

const getHighlightSignature = (url: string, highlight: IHighlight) => {
  const rect = highlight.position.boundingRect;
  return [
    url,
    highlight.position.pageNumber,
    rect.x1,
    rect.x2,
    rect.y1,
    rect.y2,
  ].join(':');
};

const PdfPreview = ({
  highlights: state,
  setWidthAndHeight,
  url,
  className,
}: IProps) => {
  const scrollHandleRef = useRef<{
    url: string;
    scrollTo: (highlight: IHighlight) => void;
  } | null>(null);
  const pendingHighlightRef = useRef<{
    url: string;
    highlight: IHighlight;
  } | null>(null);
  const scrolledSignatureRef = useRef<string | null>(null);
  const attemptedSignatureRef = useRef<string | null>(null);
  const previewRootRef = useRef<HTMLDivElement>(null);
  const scrollFrameRef = useRef<number | null>(null);
  const error = useCatchDocumentError(url);

  const resetHash = () => {};

  const scrollToPendingHighlight = useCallback(() => {
    const pending = pendingHighlightRef.current;
    const scrollHandle = scrollHandleRef.current;
    if (
      !pending ||
      !scrollHandle ||
      pending.url !== url ||
      scrollHandle.url !== url ||
      !previewRootRef.current?.querySelector(
        '.PdfHighlighter__highlight-layer',
      )
    )
      return;

    const { highlight } = pending;
    const signature = getHighlightSignature(url, highlight);
    if (scrolledSignatureRef.current === signature) return;
    if (
      attemptedSignatureRef.current === signature &&
      previewRootRef.current.querySelector(
        '.Highlight--scrolledTo .Highlight__part',
      )
    ) {
      scrolledSignatureRef.current = signature;
      return;
    }

    attemptedSignatureRef.current = signature;
    scrollHandle.scrollTo(highlight);
  }, [url]);

  const scheduleScrollToPendingHighlight = useCallback(() => {
    if (scrollFrameRef.current !== null) return;
    scrollFrameRef.current = requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      scrollToPendingHighlight();
    });
  }, [scrollToPendingHighlight]);

  useEffect(() => {
    const highlight = state?.[0];
    if (!highlight) {
      pendingHighlightRef.current = null;
      scrolledSignatureRef.current = null;
      attemptedSignatureRef.current = null;
      return;
    }
    pendingHighlightRef.current = { url, highlight };
    scheduleScrollToPendingHighlight();
  }, [scheduleScrollToPendingHighlight, state, url]);

  useEffect(() => {
    const root = previewRootRef.current;
    if (!root) return;

    const observer = new MutationObserver(() => {
      const pending = pendingHighlightRef.current;
      if (
        pending &&
        scrolledSignatureRef.current !==
          getHighlightSignature(pending.url, pending.highlight)
      ) {
        scheduleScrollToPendingHighlight();
      }
    });
    observer.observe(root, { childList: true, subtree: true });
    scheduleScrollToPendingHighlight();
    return () => {
      observer.disconnect();
      if (scrollFrameRef.current !== null) {
        cancelAnimationFrame(scrollFrameRef.current);
        scrollFrameRef.current = null;
      }
    };
  }, [scheduleScrollToPendingHighlight]);

  const httpHeaders = {
    [Authorization]: getAuthorization(),
  };

  return (
    <div
      ref={previewRootRef}
      className={cn(
        'relative size-full rounded overflow-hidden',
        '[&_.pdfViewer.removePageBorders_.page]:last-of-type:mb-0',
        className,
      )}
    >
      <Loader
        url={url}
        httpHeaders={httpHeaders}
        beforeLoad={
          <div className="absolute inset-0 flex items-center justify-center">
            <Spin />
          </div>
        }
        workerSrc="/pdfjs-dist/pdf.worker.min.js"
        cMapUrl="/pdfjs-dist/cmaps/"
        cMapPacked={true}
        standardFontDataUrl="/pdfjs-dist/standard_fonts/"
        errorMessage={<FileError>{error}</FileError>}
      >
        {(pdfDocument) => {
          pdfDocument.getPage(1).then((page) => {
            const viewport = page.getViewport({ scale: 1 });
            const width = viewport.width;
            const height = viewport.height;
            setWidthAndHeight?.(width, height);
          });

          return (
            <PdfHighlighter
              pdfDocument={pdfDocument}
              enableAreaSelection={(event) => event.altKey}
              onScrollChange={resetHash}
              scrollRef={(scrollTo) => {
                scrollHandleRef.current = { url, scrollTo };
                scheduleScrollToPendingHighlight();
              }}
              onSelectionFinished={() => null}
              highlightTransform={(
                highlight,
                index,
                setTip,
                hideTip,
                _viewportToScaled,
                _screenshot,
                isScrolledTo,
              ) => {
                const isTextHighlight = !(
                  highlight.content && highlight.content.image
                );

                const component = isTextHighlight ? (
                  <Highlight
                    isScrolledTo={isScrolledTo}
                    position={highlight.position}
                    comment={highlight.comment}
                  />
                ) : (
                  <AreaHighlight
                    isScrolledTo={isScrolledTo}
                    highlight={highlight}
                    onChange={() => {}}
                  />
                );

                return (
                  <Popup
                    popupContent={<HighlightPopup {...highlight} />}
                    onMouseOver={(popupContent) =>
                      setTip(highlight, () => popupContent)
                    }
                    onMouseOut={hideTip}
                    key={index}
                  >
                    {component}
                  </Popup>
                );
              }}
              highlights={state || []}
            />
          );
        }}
      </Loader>
    </div>
  );
};

export default memo(PdfPreview);
export { PdfPreview };
