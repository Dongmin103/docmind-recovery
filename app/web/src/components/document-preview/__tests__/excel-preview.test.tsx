import { act, renderHook } from '@testing-library/react';

let mockSize = { width: 225, height: 600 };
let mockViewers: any[] = [];
let mockResolve: () => void;
let mockReject: (error: Error) => void;
const mockData = new ArrayBuffer(8);
let mockResponse: { data: ArrayBuffer; headers?: Record<string, string> } = {
  data: mockData,
};
let mockFetchError: Error | null = null;
const mockGet = jest.fn<Promise<typeof mockResponse>, unknown[]>(async () => {
  if (mockFetchError) throw mockFetchError;
  return mockResponse;
});

jest.mock('ahooks', () => ({
  ...jest.requireActual('ahooks'),
  useSize: () => mockSize,
}));
jest.mock('@/hooks/route-hook', () => ({
  useGetKnowledgeSearchParams: () => ({}),
}));
jest.mock('@/pages/dataflow-result/hooks', () => ({
  useGetPipelineResultSearchParams: () => ({}),
}));
jest.mock('@/utils/authorization-util', () => ({
  getAuthorization: () => 'test',
}));
jest.mock('axios', () => ({ get: (...args: unknown[]) => mockGet(...args) }));
jest.mock('@js-preview/excel', () => ({
  __esModule: true,
  default: {
    init: (_container: HTMLElement, options: unknown) => {
      const viewer: any = {
        options,
        alive: true,
        reloads: 0,
        callbackRan: false,
      };
      viewer.xs = {
        sheet: {
          reload: () => {
            viewer.reloads++;
          },
        },
      };
      viewer.preview = () =>
        new Promise<void>((resolve, reject) => {
          const finish = () => {
            setTimeout(() => {
              if (!viewer.alive)
                throw new Error('library callback accessed destroyed workbook');
              viewer.callbackRan = true;
            }, 0);
          };
          mockResolve = () => {
            finish();
            resolve();
          };
          mockReject = (error: Error) => {
            finish();
            reject(error);
          };
        });
      viewer.destroy = () => {
        viewer.alive = false;
      };
      mockViewers.push(viewer);
      return viewer;
    },
  },
}));
import { useFetchExcel } from '../hooks';

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};
const start = async (fileType = 'xlsx') => {
  const hook = renderHook(({ url, type }) => useFetchExcel(url, type), {
    initialProps: { url: '/preview/opaque-id', type: fileType },
  });
  act(() => hook.result.current.containerRef(document.createElement('div')));
  await flush();
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  return hook;
};

beforeEach(() => {
  jest.useFakeTimers();
  mockViewers = [];
  mockGet.mockClear();
  mockFetchError = null;
  mockResponse = { data: mockData };
  mockSize = { width: 225, height: 600 };
});
afterEach(() => {
  jest.useRealTimers();
});

test('selects XLS parsing from metadata for opaque preview URLs', async () => {
  const hook = await start('xls');
  expect(mockViewers[0].options).toMatchObject({ xls: true });
  await act(async () => {
    mockResolve();
  });
  hook.unmount();
  await act(async () => {
    jest.runOnlyPendingTimers();
  });
});

test('resizing during parsing keeps one live workbook and redraws latest dimensions', async () => {
  const hook = await start();
  const viewer = mockViewers[0];
  mockSize = { width: 279, height: 600 };
  hook.rerender({ url: '/preview/opaque-id', type: 'xlsx' });
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  expect(viewer.alive).toBe(true);
  expect(mockViewers).toHaveLength(1);
  await act(async () => {
    mockResolve();
  });
  mockSize = { width: 141, height: 600 };
  hook.rerender({ url: '/preview/opaque-id', type: 'xlsx' });
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  expect(mockViewers).toHaveLength(1);
  expect(viewer.reloads).toBeGreaterThan(0);
  hook.unmount();
  await act(async () => {
    jest.runOnlyPendingTimers();
  });
});

test('unmount waits for pending parser and library callbacks before destroying', async () => {
  const hook = await start();
  const viewer = mockViewers[0];
  hook.unmount();
  expect(viewer.alive).toBe(true);
  await act(async () => {
    mockResolve();
  });
  await act(async () => {
    jest.runOnlyPendingTimers();
  });
  expect(viewer.callbackRan).toBe(true);
  expect(viewer.alive).toBe(false);
});

test('file switching isolates stale parser failures from the current file', async () => {
  const hook = await start();
  const oldViewer = mockViewers[0];
  const rejectOld = mockReject;
  hook.rerender({ url: '/preview/new-file', type: 'xlsx' });
  await flush();
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  expect(oldViewer.alive).toBe(true);
  expect(mockViewers).toHaveLength(2);
  await act(async () => {
    rejectOld(new Error('old file failed'));
    mockResolve();
  });
  await act(async () => {
    jest.runOnlyPendingTimers();
  });
  expect(hook.result.current.error).toBe('');
  expect(hook.result.current.status).toBe(true);
  expect(oldViewer.alive).toBe(false);
  hook.unmount();
  await act(async () => {
    jest.runOnlyPendingTimers();
  });
});

test('preserves the original parse failure for display', async () => {
  const hook = await start();
  await act(async () => {
    mockReject(new Error('Invalid XLS workbook'));
  });
  expect(hook.result.current.error).toBe('Invalid XLS workbook');
  expect(hook.result.current.status).toBe(false);
  hook.unmount();
  await act(async () => {
    jest.runOnlyPendingTimers();
  });
});

test('reports HTTP 200 JSON backend errors from the single authenticated request', async () => {
  mockResponse = {
    data: new TextEncoder().encode(
      JSON.stringify({ code: 102, message: '문서를 찾을 수 없습니다.' }),
    ).buffer,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  };
  const hook = await start();
  expect(hook.result.current.error).toBe('문서를 찾을 수 없습니다.');
  expect(hook.result.current.status).toBe(false);
  expect(mockViewers).toHaveLength(0);
  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(mockGet).toHaveBeenCalledWith('/preview/opaque-id', {
    headers: { Authorization: 'test' },
    responseType: 'arraybuffer',
  });
  hook.unmount();
});

test('reports authenticated fetch rejection without initializing a parser', async () => {
  mockFetchError = new Error('Request failed with status code 403');
  const hook = await start();
  expect(hook.result.current.error).toBe('Request failed with status code 403');
  expect(hook.result.current.status).toBe(false);
  expect(mockViewers).toHaveLength(0);
  expect(mockGet).toHaveBeenCalledTimes(1);
  hook.unmount();
});
