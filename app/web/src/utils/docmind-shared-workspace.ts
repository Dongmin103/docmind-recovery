type SharedWorkspaceResponse = {
  code?: number;
  message?: string;
};

let sharedWorkspaceConfirmed = false;
let recovery: Promise<boolean> | undefined;

const createSharedSession = () =>
  fetch('/api/v1/docmind/shared-session', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });

// Recover only a workspace that the server explicitly enabled on this page.
// Do not replay the failed request: it may have been a document mutation.
export const recoverDocMindSharedWorkspace = async (): Promise<boolean> => {
  if (!sharedWorkspaceConfirmed || window.location.pathname !== '/docmind') {
    return false;
  }
  if (!recovery) {
    recovery = (async () => {
      try {
        const response = await createSharedSession();
        if ([401, 403, 404].includes(response.status)) {
          sharedWorkspaceConfirmed = false;
          return false;
        }
        // During startup keep the page and its query. Existing query retries
        // and polling can recover; the failed API response still reaches UI.
        if (!response.ok) return true;
        const payload = (await response.json()) as SharedWorkspaceResponse;
        if (payload.code !== 0) {
          sharedWorkspaceConfirmed = false;
          return false;
        }
        return true;
      } catch {
        return true;
      }
    })().finally(() => {
      recovery = undefined;
    });
  }
  return recovery;
};

export class DocMindSharedWorkspaceError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'DocMindSharedWorkspaceError';
    this.status = status;
  }
}

export const initializeDocMindSharedWorkspace = async () => {
  sharedWorkspaceConfirmed = false;
  const response = await createSharedSession();

  // A normal authenticated RAGFlow deployment can keep using its login flow.
  if (response.status === 404) return null;

  const payload = (await response
    .json()
    .catch(() => ({}))) as SharedWorkspaceResponse;
  if (response.ok && payload.code === 0) {
    sharedWorkspaceConfirmed = true;
    return null;
  }

  throw new DocMindSharedWorkspaceError(
    payload.message || '공용 DocMind 작업공간을 준비하지 못했습니다.',
    response.status || 503,
  );
};
