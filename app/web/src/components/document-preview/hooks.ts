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

import { Authorization } from '@/constants/authorization';
import { useGetKnowledgeSearchParams } from '@/hooks/route-hook';
import { useGetPipelineResultSearchParams } from '@/pages/dataflow-result/hooks';
import api, { restAPIv1 } from '@/utils/api';
import { getAuthorization } from '@/utils/authorization-util';
import jsPreviewExcel from '@js-preview/excel';
import { useSize } from 'ahooks';
import axios from 'axios';
import JSZip from 'jszip';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as XLSX from 'xlsx';

// ZIP file header bytes "PK"
const ZIP_HEADER_0 = 0x50;
const ZIP_HEADER_1 = 0x4b;

export const isZipLikeBlob = async (blob: Blob): Promise<boolean> => {
  try {
    const headerSlice = blob.slice(0, 4);
    const buf = await headerSlice.arrayBuffer();
    const bytes = new Uint8Array(buf);
    return (
      bytes.length >= 2 &&
      bytes[0] === ZIP_HEADER_0 &&
      bytes[1] === ZIP_HEADER_1
    );
  } catch (e) {
    console.error('Failed to inspect blob header', e);
    return false;
  }
};

export const useDocumentResizeObserver = () => {
  const [containerWidth, setContainerWidth] = useState<number>();
  const [containerRef, setContainerRef] = useState<HTMLElement | null>(null);
  const size = useSize(containerRef);

  const onResize = useCallback((width?: number) => {
    if (width) {
      setContainerWidth(width);
    }
  }, []);

  useEffect(() => {
    onResize(size?.width);
  }, [size?.width, onResize]);

  return { containerWidth, setContainerRef };
};

function highlightPattern(text: string, pattern: string, pageNumber: number) {
  if (pageNumber === 2) {
    return `<mark>${text}</mark>`;
  }
  if (text.trim() !== '' && pattern.match(text)) {
    // return pattern.replace(text, (value) => `<mark>${value}</mark>`);
    return `<mark>${text}</mark>`;
  }
  return text.replace(pattern, (value) => `<mark>${value}</mark>`);
}

export const useHighlightText = (searchText: string = '') => {
  const textRenderer = useCallback(
    (textItem: any) => {
      return highlightPattern(textItem.str, searchText, textItem.pageNumber);
    },
    [searchText],
  );

  return textRenderer;
};

export const useGetDocumentUrl = (isAgent: boolean) => {
  const { documentId } = useGetKnowledgeSearchParams();
  const { createdBy, documentId: id } = useGetPipelineResultSearchParams();

  const url = useMemo(() => {
    if (isAgent) {
      return api.downloadFile + `?id=${id}&created_by=${createdBy}`;
    }
    return `${restAPIv1}/documents/${documentId}/preview`;
  }, [createdBy, documentId, id, isAgent]);

  return url;
};

export const useCatchError = (api: string) => {
  const [error, setError] = useState('');
  const fetchDocument = useCallback(async () => {
    const ret = await axios.get(api);
    const { data } = ret;
    if (!(data instanceof ArrayBuffer) && data.code !== 0) {
      setError(data.message);
    }
    return ret;
  }, [api]);

  useEffect(() => {
    fetchDocument();
  }, [fetchDocument]);

  return { fetchDocument, error };
};

export const useFetchDocument = () => {
  const fetchDocument = useCallback(async (api: string) => {
    const ret = await axios.get(api, {
      headers: {
        [Authorization]: getAuthorization(),
      },
      responseType: 'arraybuffer',
    });
    return ret;
  }, []);

  return { fetchDocument };
};

/**
 * WPS spreadsheets embed images in cells via the proprietary DISPIMG formula
 * (stored in xl/cellimages.xml). @js-preview/excel cannot evaluate this
 * formula and crashes with "Cannot read properties of undefined (reading
 * 'render')". This function strips DISPIMG formulas from worksheet XML,
 * replacing them with a text placeholder so the rest of the sheet renders.
 */
async function stripWpsDispImg(data: ArrayBuffer): Promise<ArrayBuffer> {
  const zip = await JSZip.loadAsync(data);

  const worksheetPaths = Object.keys(zip.files).filter((path) =>
    /^xl\/worksheets\/sheet\d+\.xml$/.test(path),
  );

  let modified = false;
  for (const path of worksheetPaths) {
    const file = zip.file(path);
    if (!file) continue;
    const xml = await file.async('string');
    if (!xml.includes('DISPIMG')) continue;

    modified = true;
    let cleaned = xml;
    // Remove <f> formula tags that reference DISPIMG
    cleaned = cleaned.replace(/<f\b[^>]*>[\s\S]*?DISPIMG[\s\S]*?<\/f>/g, '');
    // Replace cached <v> values that reference DISPIMG with a placeholder
    cleaned = cleaned.replace(/<v>[^<]*DISPIMG[^<]*<\/v>/g, '<v>[图片]</v>');
    zip.file(path, cleaned);
  }

  if (!modified) return data;
  return zip.generateAsync({
    type: 'arraybuffer',
    compression: 'DEFLATE',
  });
}

/**
 * ExcelJS (used internally by @js-preview/excel) fails to parse workbooks
 * whose root <workbook> element carries an XML namespace prefix (e.g.
 * <x:workbook> instead of <workbook>). This is common in files exported by
 * WPS Office or older Excel versions. The workbook-xform parser only sets
 * its model when the closing tag name is exactly "workbook", so a prefixed
 * root element leaves the model undefined and crashes with
 * "Cannot read properties of undefined (reading 'sheets')".
 *
 * When such a prefix is detected, re-serialize the file with SheetJS (which
 * always emits a standard, prefix-free xlsx) so ExcelJS can parse it.
 */
async function normalizeXlsxForExcelJS(
  data: ArrayBuffer,
): Promise<ArrayBuffer> {
  try {
    const zip = await JSZip.loadAsync(data);
    const workbookFile = zip.file('xl/workbook.xml');
    if (!workbookFile) return data;

    const xml = await workbookFile.async('string');
    // Detect a namespace prefix on the root <workbook> element, e.g. <x:workbook>
    if (!/<\w+:workbook[\s>]/.test(xml)) return data;

    const workbook = XLSX.read(data, { type: 'array' });
    return XLSX.write(workbook, {
      bookType: 'xlsx',
      type: 'array',
    }) as ArrayBuffer;
  } catch {
    // Not a valid ZIP, SheetJS can't parse, etc. - let the previewer
    // handle the original data and surface its own error.
    return data;
  }
}

type ExcelPreviewInstance = ReturnType<typeof jsPreviewExcel.init> & {
  xs: {
    sheet: {
      reload: () => void;
      editor?: { clear: () => void };
    };
  };
};

export const useFetchExcel = (filePath: string, fileType = 'xlsx') => {
  const [status, setStatus] = useState(true);
  const [error, setError] = useState('');
  const { fetchDocument } = useFetchDocument();
  const [containerEl, setContainerEl] = useState<HTMLDivElement | null>(null);
  const size = useSize(containerEl);
  const resizeRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (!filePath || !containerEl) return;
    let disposed = false;
    let ready = false;
    let previewer: ExcelPreviewInstance | null = null;
    let pending: Promise<unknown> | null = null;
    let retired = false;
    const host = document.createElement('div');
    host.style.height = '100%';
    host.style.width = '100%';
    containerEl.appendChild(host);
    setStatus(true);
    setError('');

    // 1.7.14 reads wrapper dimensions on every sheet.reload(). Keep the
    // workbook, active sheet and scroll instead of reparsing on each resize.
    const resize = () => {
      if (disposed || !ready || !previewer) return;
      previewer.xs.sheet.reload();
      // reload clears the canvas; the library's clear hook redraws images.
      previewer.xs.sheet.editor?.clear();
    };
    resizeRef.current = resize;

    const retire = () => {
      if (retired) return;
      retired = true;
      host.remove();
      if (!previewer || !pending) return;
      const instance = previewer;
      // preview() has no cancellation API. Its XHR/parser must finish before
      // destroy nulls xs/workbookDataSource. loadData's clear/sheet-swap hooks
      // queue non-nested zero-delay callbacks before preview settles; drain
      // them before destruction (also on parse failure).
      void pending.then(
        () => setTimeout(() => instance.destroy(), 0),
        () => setTimeout(() => instance.destroy(), 0),
      );
    };

    void (async () => {
      try {
        const response = await fetchDocument(filePath);
        if (disposed) return;
        // Both preview backends return HTTP 200 JSON envelopes on failure.
        // Inspect the same authenticated response before it reaches ExcelJS.
        if (String(response.headers?.['content-type'] ?? '').includes('json')) {
          const envelope = JSON.parse(new TextDecoder().decode(response.data));
          if (
            envelope &&
            typeof envelope === 'object' &&
            'code' in envelope &&
            envelope.code !== 0
          ) {
            throw new Error(envelope.message || 'Failed to load document');
          }
        }
        let data: ArrayBuffer = response.data;
        const isXls = fileType.toLowerCase() === 'xls';
        // XLS is BIFF, not a ZIP. Only XLSX uses the XML repair pipeline.
        if (!isXls) {
          try {
            data = await normalizeXlsxForExcelJS(data);
            data = await stripWpsDispImg(data);
          } catch {
            // Let the parser report malformed input instead of a ZIP helper.
            data = response.data;
          }
        }
        if (disposed) return;
        const options = { xls: isXls, minColLength: 20 };
        previewer = jsPreviewExcel.init(host, options) as ExcelPreviewInstance;
        pending = previewer.preview(data);
        await pending;
        if (disposed) return;
        ready = true;
        resize();
        setStatus(true);
      } catch (cause) {
        if (disposed) return;
        setError(cause instanceof Error ? cause.message : String(cause));
        setStatus(false);
        retire();
      }
    })();

    return () => {
      disposed = true;
      if (resizeRef.current === resize) resizeRef.current = null;
      retire();
    };
  }, [filePath, fileType, containerEl, fetchDocument]);

  useEffect(() => {
    resizeRef.current?.();
  }, [size?.width, size?.height]);

  return { status, containerRef: setContainerEl, error };
};

export const useCatchDocumentError = (url: string) => {
  const httpHeaders = useMemo(() => {
    return {
      [Authorization]: getAuthorization(),
    };
  }, []);
  const [error, setError] = useState<string>('');

  const fetchDocument = useCallback(async () => {
    try {
      const { data } = await axios.get(url, { headers: httpHeaders });
      // Only treat as error if response is JSON with an error code
      // Binary data (like PDF) won't have a code property
      if (
        data &&
        typeof data === 'object' &&
        'code' in data &&
        data.code !== 0
      ) {
        setError(data?.message || 'Failed to load document');
      }
    } catch (e) {
      // Network errors or non-2xx responses
      const errMsg = e instanceof Error ? e.message : 'Failed to load document';
      if (errMsg) {
        setError(errMsg);
      }
    }
  }, [url, httpHeaders]);
  useEffect(() => {
    fetchDocument();
  }, [fetchDocument]);

  return error;
};

const ZOOM_STEPS = [25, 50, 75, 100, 125, 150, 175, 200] as const;

const clampZoom = (scale: number, direction: 1 | -1): number => {
  const exactIdx = ZOOM_STEPS.indexOf(scale as (typeof ZOOM_STEPS)[number]);
  let idx: number;

  if (exactIdx >= 0) {
    // Already on a predefined step: move one step in the zoom direction.
    idx = exactIdx + direction;
  } else if (direction > 0) {
    // Between steps and zooming in: snap up to the next higher step. This
    // index is already the target, so it must not be advanced again.
    const next = ZOOM_STEPS.findIndex((v) => v > scale);
    idx = next < 0 ? ZOOM_STEPS.length - 1 : next;
  } else {
    // Between steps and zooming out: snap down to the next lower step.
    let prev = 0;
    for (let i = ZOOM_STEPS.length - 1; i >= 0; i--) {
      if (ZOOM_STEPS[i] < scale) {
        prev = i;
        break;
      }
    }
    idx = prev;
  }

  idx = Math.max(0, Math.min(ZOOM_STEPS.length - 1, idx));
  return ZOOM_STEPS[idx] ?? scale;
};

interface UseDocxPreviewZoomOptions {
  url: string;
  totalPages: number;
  pageWidthPx?: number;
  containerWidth?: number;
  paddingPx?: number;
  enabled?: boolean;
}

interface UseDocxPreviewZoomResult {
  zoomScale: number;
  minZoom: number;
  maxZoom: number;
  handleZoomIn: () => void;
  handleZoomOut: () => void;
  resetZoom: () => void;
}

export const useDocxPreviewZoom = ({
  url,
  totalPages,
  pageWidthPx,
  containerWidth,
  paddingPx = 32,
  enabled = true,
}: UseDocxPreviewZoomOptions): UseDocxPreviewZoomResult => {
  const [zoomScale, setZoomScale] = useState(100);
  const [hasUserZoomed, setHasUserZoomed] = useState(false);
  const [isInitialFitPending, setIsInitialFitPending] = useState(true);

  const resetZoom = useCallback(() => {
    setZoomScale(100);
    setHasUserZoomed(false);
    setIsInitialFitPending(true);
  }, []);

  useEffect(() => {
    resetZoom();
  }, [url, resetZoom]);

  const handleZoomIn = useCallback(() => {
    setHasUserZoomed(true);
    setZoomScale((s) => clampZoom(s, 1));
  }, []);

  const handleZoomOut = useCallback(() => {
    setHasUserZoomed(true);
    setZoomScale((s) => clampZoom(s, -1));
  }, []);

  // Fit the page width to the container on first paint and on resize,
  // unless the user has manually changed the zoom level.
  useEffect(() => {
    if (!enabled || totalPages <= 0 || !containerWidth || !pageWidthPx) {
      return;
    }

    const availableWidth = Math.max(0, containerWidth - paddingPx);
    if (availableWidth <= 0) {
      return;
    }

    const fitScale = Math.floor((availableWidth / pageWidthPx) * 100);
    const clampedFitScale = Math.min(100, fitScale);

    if (isInitialFitPending) {
      setZoomScale(clampedFitScale);
      setIsInitialFitPending(false);
    } else if (!hasUserZoomed) {
      setZoomScale(clampedFitScale);
    }
  }, [
    enabled,
    totalPages,
    containerWidth,
    pageWidthPx,
    paddingPx,
    isInitialFitPending,
    hasUserZoomed,
  ]);

  return {
    zoomScale,
    minZoom: ZOOM_STEPS[0],
    maxZoom: ZOOM_STEPS[ZOOM_STEPS.length - 1],
    handleZoomIn,
    handleZoomOut,
    resetZoom,
  };
};
