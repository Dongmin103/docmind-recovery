import {
  initializeDocMindSharedWorkspace,
  recoverDocMindSharedWorkspace,
} from './docmind-shared-workspace';

const response = (status: number, body: object) =>
  ({
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  }) as Response;

afterEach(() => {
  jest.restoreAllMocks();
  window.history.replaceState({}, '', '/');
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

it('coalesces expired shared-session recovery without replaying an API request', async () => {
  window.history.replaceState({}, '', '/docmind?view=library');
  const fetch = jest
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(response(200, { code: 0 }));
  await initializeDocMindSharedWorkspace();
  fetch.mockClear();
  expect(
    await Promise.all([
      recoverDocMindSharedWorkspace(),
      recoverDocMindSharedWorkspace(),
    ]),
  ).toEqual([true, true]);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toBe('/api/v1/docmind/shared-session');
});

it('does not recover normal authenticated routes or disabled shared workspaces', async () => {
  const fetch = jest
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(response(200, { code: 0 }));
  await initializeDocMindSharedWorkspace();
  fetch.mockClear();
  expect(await recoverDocMindSharedWorkspace()).toBe(false);
  expect(fetch).not.toHaveBeenCalled();
  window.history.replaceState({}, '', '/docmind');
  fetch.mockResolvedValue(response(404, {}));
  await initializeDocMindSharedWorkspace();
  fetch.mockClear();
  expect(await recoverDocMindSharedWorkspace()).toBe(false);
  expect(fetch).not.toHaveBeenCalled();
});

it.each([401, 403, 404])(
  'restores normal authentication when recovery is denied with %s',
  async (status) => {
    window.history.replaceState({}, '', '/docmind');
    const fetch = jest
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(response(200, { code: 0 }));
    await initializeDocMindSharedWorkspace();
    fetch.mockResolvedValue(response(status, {}));
    expect(await recoverDocMindSharedWorkspace()).toBe(false);
    fetch.mockClear();
    expect(await recoverDocMindSharedWorkspace()).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  },
);

it('keeps a confirmed workspace mounted during an outage and permits later recovery', async () => {
  window.history.replaceState({}, '', '/docmind');
  const fetch = jest
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(response(200, { code: 0 }));
  await initializeDocMindSharedWorkspace();
  fetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
  expect(await recoverDocMindSharedWorkspace()).toBe(true);
  fetch.mockResolvedValueOnce(response(503, {}));
  expect(await recoverDocMindSharedWorkspace()).toBe(true);
  fetch.mockResolvedValueOnce(response(200, { code: 0 }));
  expect(await recoverDocMindSharedWorkspace()).toBe(true);
});
