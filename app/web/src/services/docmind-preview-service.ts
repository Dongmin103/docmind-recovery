import { Authorization } from '@/constants/authorization';
import type { CreatedPreview, PreviewSession } from '@/interfaces/database/docmind-preview';
import type { CreatePreviewRequest } from '@/interfaces/request/docmind-preview';
import { getAuthorization } from '@/utils/authorization-util';

export class PreviewRequestError extends Error {
  constructor(
    public readonly code: string,
    public readonly status: number,
  ) {
    super(code);
  }
}

const BasePath = '/api/v1/docmind';
export const PreviewTokenHeader = 'X-DocMind-Preview-Token';

async function previewRequest<T>(
  path: string,
  method: string,
  signal: AbortSignal,
  previewToken?: string,
  body?: object,
): Promise<T> {
  const response = await fetch(`${BasePath}${path}`, {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    signal,
    headers: {
      [Authorization]: getAuthorization(),
      ...(previewToken ? { [PreviewTokenHeader]: previewToken } : {}),
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new PreviewRequestError(
      typeof payload.error === 'string' ? payload.error : 'PREVIEW_REQUEST_FAILED',
      response.status,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function createPreview(
  documentId: string,
  sourceVersionId: string,
  chunkSetId: string,
  idempotencyKey: string,
  signal: AbortSignal,
) {
  return previewRequest<CreatedPreview>(
    `/documents/${encodeURIComponent(documentId)}/previews`,
    'POST',
    signal,
    undefined,
    {
      source_version_id: sourceVersionId,
      chunk_set_id: chunkSetId,
      idempotency_key: idempotencyKey,
    } satisfies CreatePreviewRequest,
  );
}

export function getPreview(
  previewId: string,
  token: string,
  signal: AbortSignal,
) {
  return previewRequest<PreviewSession>(
    `/previews/${encodeURIComponent(previewId)}`,
    'GET',
    signal,
    token,
  );
}

export function heartbeatPreview(
  previewId: string,
  token: string,
  signal: AbortSignal,
) {
  return previewRequest<PreviewSession>(
    `/previews/${encodeURIComponent(previewId)}/heartbeat`,
    'POST',
    signal,
    token,
  );
}

export function closePreview(
  previewId: string,
  token: string,
  signal: AbortSignal,
) {
  return previewRequest<void>(
    `/previews/${encodeURIComponent(previewId)}`,
    'DELETE',
    signal,
    token,
  );
}

export async function fetchPreviewBlob(
  previewId: string,
  token: string,
  signal: AbortSignal,
  page?: number,
): Promise<Blob> {
  const suffix = page === undefined
    ? 'content'
    : `pages/${encodeURIComponent(page)}`;
  const response = await fetch(
    `${BasePath}/previews/${encodeURIComponent(previewId)}/${suffix}`,
    {
      credentials: 'same-origin',
      cache: 'no-store',
      signal,
      headers: {
        [Authorization]: getAuthorization(),
        [PreviewTokenHeader]: token,
      },
    },
  );
  if (!response.ok || response.status === 202) {
    const payload = await response.json().catch(() => ({}));
    throw new PreviewRequestError(
      typeof payload.error === 'string' ? payload.error : 'PREVIEW_CONTENT_FAILED',
      response.status,
    );
  }
  return response.blob();
}
