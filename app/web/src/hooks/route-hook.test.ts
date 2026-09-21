jest.mock('react-router', () => ({
  useLocation: jest.fn(),
  useNavigate: jest.fn(),
  useSearchParams: jest.fn(),
}));

jest.mock('@/routes', () => ({
  Routes: { DataflowResult: '/dataflow-result' },
}));

import { InspectionSource, parseInspectionSource } from './route-hook';

describe('parseInspectionSource', () => {
  it('accepts the bounded docmind source enum', () => {
    expect(parseInspectionSource('docmind')).toBe(InspectionSource.DocMind);
  });

  it.each([
    null,
    '',
    'DocMind',
    'ragflow',
    '/docmind',
    'https://example.com/docmind',
  ])('rejects unsupported source value %p', (source) => {
    expect(parseInspectionSource(source)).toBeUndefined();
  });
});
