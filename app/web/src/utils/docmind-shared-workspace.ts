type SharedWorkspaceResponse = {
  code?: number;
  message?: string;
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
  const response = await fetch('/api/v1/docmind/shared-session', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });

  // A normal authenticated RAGFlow deployment can keep using its login flow.
  if (response.status === 404) return null;

  const payload = (await response
    .json()
    .catch(() => ({}))) as SharedWorkspaceResponse;
  if (response.ok && payload.code === 0) return null;

  throw new DocMindSharedWorkspaceError(
    payload.message || '공용 DocMind 작업공간을 준비하지 못했습니다.',
    response.status || 503,
  );
};
