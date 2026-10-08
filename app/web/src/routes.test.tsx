jest.mock('react-router', () => ({
  ...jest.requireActual('react-router'),
  createBrowserRouter: (routes: unknown) => ({ routes }),
  redirect: (target: string) => ({
    status: 302,
    headers: { get: (name: string) => (name === 'Location' ? target : null) },
  }),
}));

import { matchRoutes } from 'react-router';
import authorizationUtil from './utils/authorization-util';
import { routers } from './routes';

const load = async (path: string, search = '') => {
  const route = routers.routes.find((item) => item.path === path);
  if (typeof route?.loader !== 'function') throw new Error(`Missing loader for ${path}`);
  return route.loader({
    request: { url: `http://localhost${path}${search}` } as Request,
    params: {},
    context: {},
  } as Parameters<typeof route.loader>[0]) as Promise<Response>;
};

it.each(['/', '/home', '/login', '/login-next'])(
  'routes %s to the DocMind workspace',
  async (path) => {
    const result = await load(path);
    expect(result.status).toBe(302);
    expect(result.headers.get('Location')).toBe('/docmind');
  },
);

it('stores an OAuth callback token before redirecting to DocMind', async () => {
  const setAuthorization = jest.spyOn(authorizationUtil, 'setAuthorization');
  const result = await load('/login', '?auth=sample-token&view=library');
  expect(setAuthorization).toHaveBeenCalledWith('sample-token');
  expect(result.headers.get('Location')).toBe('/docmind?view=library');
  setAuthorization.mockRestore();
});

it.each([
  '/user-setting',
  '/user-setting/profile',
  '/user-setting/model',
  '/user-setting/team',
  '/user-setting/api',
  '/user-setting/mcp',
  '/user-setting/data-source',
  '/user-setting/chat-channel',
  '/user-setting/data-source/data-source-detail-page',
  '/user-setting/future-section/nested-page',
])('redirects the matched settings route %s before rendering controls', async (path) => {
  const matches = matchRoutes(routers.routes, path);
  const guardedMatch = matches?.find(
    (match) => typeof match.route.loader === 'function',
  );
  expect(guardedMatch).toBeDefined();
  if (!guardedMatch || typeof guardedMatch.route.loader !== 'function') return;

  const response = await guardedMatch.route.loader({
    request: { url: `http://localhost${path}` } as Request,
    params: guardedMatch.params,
    context: {},
  } as Parameters<typeof guardedMatch.route.loader>[0]) as Response;
  expect(response.headers.get('Location')).toBe('/docmind');
});
