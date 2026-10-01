import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import TemporaryOriginalPreview from '../temporary-original-preview';

jest.mock('@/components/document-preview', () => ({
  __esModule: true,
  default: ({ url, fileType, highlights }: { url: string; fileType: string; highlights?: unknown[] }) => (
    <div data-testid="native-viewer" data-url={url} data-format={fileType} data-highlights={highlights?.length ?? 0} />
  ),
}));
jest.mock('@/utils/authorization-util', () => ({
  getAuthorization: () => 'Bearer test-user',
}));

const ReadySession = {
  preview_id: 'preview-1',
  preview_token: 'session-secret',
  status: 'READY',
  source_format: 'docx',
  display_format: 'docx',
  viewer_kind: 'word',
  source_version_id: 'source-v1',
  chunk_set_id: 'chunks-v1',
};

function response(status: number, data?: unknown, contentType = 'application/json') {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
    blob: async () => new Blob(['test'], { type: contentType }),
  } as Response;
}

describe('temporary original preview', () => {
  let mockFetch: jest.Mock;
  let revoke: jest.Mock;

  beforeEach(() => {
    mockFetch = jest.fn((url: string, options: RequestInit) => {
      if (options.method === 'POST') return Promise.resolve(response(202, ReadySession));
      if (url.endsWith('/content')) return Promise.resolve(response(200, undefined, 'application/octet-stream'));
      if (options.method === 'DELETE') return Promise.resolve(response(204));
      return Promise.resolve(response(200, ReadySession));
    });
    global.fetch = mockFetch;
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: jest.fn(() => 'blob:temporary-preview'),
    });
    revoke = jest.fn();
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: revoke,
    });
  });

  it.each(['doc', 'ppt'])('does not request unsupported %s previews', (sourceFormat) => {
    render(<TemporaryOriginalPreview documentId="d" sourceVersionId="v" chunkSetId="c" sourceFormat={sourceFormat} />);
    expect(screen.getByText('이 형식은 원문 미리보기를 지원하지 않습니다.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '원문 보기' })).not.toBeInTheDocument();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it.each([
    ['PREVIEW_DISABLED', 503, '원문 미리보기 기능이 비활성화되어 있습니다.'],
    ['PREVIEW_QUEUE_FULL', 429, '미리보기 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요.'],
    ['PREVIEW_QUEUE_TIMEOUT', 410, '미리보기 대기 시간이 120초를 초과했습니다. 다시 시도해 주세요.'],
    ['PREVIEW_PROCESS_TIMEOUT', 504, '페이지 준비 시간이 60초를 초과했습니다.'],
  ])('explains %s without calling it general overload', async (code, status, message) => {
    mockFetch.mockResolvedValue(response(status as number, { error: code }));
    render(<TemporaryOriginalPreview documentId="d" sourceVersionId="v" chunkSetId="c" />);
    fireEvent.click(screen.getByRole('button', { name: '원문 보기' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(message as string));
  });

  it('starts only after a click, supplies both tokens, and closes on unmount', async () => {
    const { unmount } = render(
      <TemporaryOriginalPreview
        documentId="document-1"
        sourceVersionId="source-v1"
        chunkSetId="chunks-v1"
      />,
    );
    expect(mockFetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '원문 보기' }));
    await waitFor(() => expect(screen.getByTestId('native-viewer')).toHaveAttribute('data-format', 'docx'));
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/v1/docmind/documents/document-1/previews',
      expect.objectContaining({
        method: 'POST',
        signal: expect.any(AbortSignal),
        headers: expect.objectContaining({ Authorization: 'Bearer test-user' }),
        body: expect.stringContaining('"source_version_id":"source-v1"'),
      }),
    );
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/v1/docmind/previews/preview-1/content',
      expect.objectContaining({
        signal: expect.any(AbortSignal),
        headers: expect.objectContaining({
          Authorization: 'Bearer test-user',
          'X-DocMind-Preview-Token': 'session-secret',
        }),
      }),
    );
    unmount();
    await waitFor(() => expect(mockFetch).toHaveBeenCalledWith(
      '/api/v1/docmind/previews/preview-1',
      expect.objectContaining({
        method: 'DELETE',
        headers: expect.objectContaining({
          'X-DocMind-Preview-Token': 'session-secret',
        }),
      }),
    ));
    expect(revoke).toHaveBeenCalledWith('blob:temporary-preview');
  });

  it('closes a creation that completes after the viewer was dismissed', async () => {
    let finishCreate!: (value: Response) => void;
    mockFetch.mockImplementation((url: string, options: RequestInit) => {
      if (options.method === 'POST') {
        return new Promise<Response>((resolve) => {
          finishCreate = resolve;
        });
      }
      if (options.method === 'DELETE') return Promise.resolve(response(204));
      throw new Error(`unexpected request: ${url}`);
    });
    const { unmount } = render(
      <TemporaryOriginalPreview
        documentId="document-1"
        sourceVersionId="source-v1"
        chunkSetId="chunks-v1"
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '원문 보기' }));
    unmount();
    finishCreate(response(202, ReadySession));
    await waitFor(() => expect(mockFetch).toHaveBeenCalledWith(
      '/api/v1/docmind/previews/preview-1',
      expect.objectContaining({ method: 'DELETE' }),
    ));
  });

  it('polls a queued preview, keeps it alive, and clears stale highlights when chunks change', async () => {
    jest.useFakeTimers();
    const onChunkSetChanged = jest.fn();
    mockFetch.mockImplementation((url: string, options: RequestInit) => {
      if (options.method === 'POST' && url.endsWith('/previews')) {
        return Promise.resolve(response(202, { ...ReadySession, status: 'QUEUED' }));
      }
      if (url.endsWith('/heartbeat')) {
        return Promise.resolve(response(200, { ...ReadySession, chunk_set_id: 'chunks-v2', chunk_set_changed: true }));
      }
      if (url.endsWith('/content')) return Promise.resolve(response(200));
      if (options.method === 'DELETE') return Promise.resolve(response(204));
      return Promise.resolve(response(200, ReadySession));
    });
    const { unmount } = render(
      <TemporaryOriginalPreview
        documentId="document-1"
        sourceVersionId="source-v1"
        chunkSetId="chunks-v1"
        highlights={[{} as any]}
        onChunkSetChanged={onChunkSetChanged}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '원문 보기' }));
    await act(async () => {
      await jest.advanceTimersByTimeAsync(2000);
    });
    expect(screen.getByTestId('native-viewer')).toHaveAttribute('data-highlights', '1');
    await act(async () => {
      await jest.advanceTimersByTimeAsync(30000);
    });
    expect(onChunkSetChanged).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('native-viewer')).toHaveAttribute('data-highlights', '0');
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/v1/docmind/previews/preview-1/heartbeat',
      expect.objectContaining({
        signal: expect.any(AbortSignal),
        headers: expect.objectContaining({ 'X-DocMind-Preview-Token': 'session-secret' }),
      }),
    );
    unmount();
    jest.useRealTimers();
  });

  it('closes the old session when the source version changes in place', async () => {
    const { rerender, unmount } = render(
      <TemporaryOriginalPreview
        documentId="document-1"
        sourceVersionId="source-v1"
        chunkSetId="chunks-v1"
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '원문 보기' }));
    await waitFor(() => expect(screen.getByTestId('native-viewer')).toBeInTheDocument());
    rerender(
      <TemporaryOriginalPreview
        documentId="document-1"
        sourceVersionId="source-v2"
        chunkSetId="chunks-v2"
      />,
    );
    await waitFor(() => expect(screen.getByRole('button', { name: '원문 보기' })).toBeInTheDocument());
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/v1/docmind/previews/preview-1',
      expect.objectContaining({ method: 'DELETE' }),
    );
    unmount();
  });
});
