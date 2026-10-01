jest.mock('react-router', () => ({
  ...jest.requireActual('react-router'),
  createBrowserRouter: (routes: unknown) => ({ routes }),
  redirect: (target: string) => ({
    status: 302,
    headers: { get: (name: string) => (name === 'Location' ? target : null) },
  }),
}));

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
