import { initializeDocMindSharedWorkspace } from './docmind-shared-workspace';

const response = (status: number, body: object) =>
  ({
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  }) as Response;

afterEach(() => {
  jest.restoreAllMocks();
});

it('creates the shared session before DocMind mounts', async () => {
  const fetch = jest
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(response(200, { code: 0, data: { ready: true } }));

  await expect(initializeDocMindSharedWorkspace()).resolves.toBeNull();
  expect(fetch).toHaveBeenCalledWith('/api/v1/docmind/shared-session', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
});

it('keeps the normal login flow when shared mode is disabled', async () => {
  jest
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(response(404, { message: 'disabled' }));

  await expect(initializeDocMindSharedWorkspace()).resolves.toBeNull();
});

it('blocks the workspace when the configured shared user is unavailable', async () => {
  jest
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(
      response(503, { message: 'DOCMIND_SHARED_USER_NOT_FOUND' }),
    );

  await expect(initializeDocMindSharedWorkspace()).rejects.toMatchObject({
    status: 503,
  });
});
