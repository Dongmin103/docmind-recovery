import { render, screen } from '@testing-library/react';
import SearchResultContent from './search-result-content';

jest.mock('@/components/highlight-markdown', () => ({
  __esModule: true,
  default: ({ children }: { children: string }) => <p>{children}</p>,
}));

it('preserves source HTML table rows and merged cells without active markup', () => {
  const { container } = render(<SearchResultContent content={'<table><tr><th colspan="2">지원사업</th></tr><tr><td rowspan="2">일반형</td><td>구축</td></tr><tr><td>고도화</td></tr></table><script>alert(1)</script>'} />);
  expect(screen.getByRole('table')).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: '지원사업' })).toHaveAttribute('colspan', '2');
  expect(screen.getByRole('cell', { name: '일반형' })).toHaveAttribute('rowspan', '2');
  expect(screen.getAllByRole('row')).toHaveLength(3);
  expect(screen.getByRole('region', { name: '문서 관련 표' })).toHaveAttribute('tabindex', '0');
  expect(container.querySelector('script')).toBeNull();
});
