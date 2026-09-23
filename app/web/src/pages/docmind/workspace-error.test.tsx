import { act, fireEvent, render, screen } from '@testing-library/react';
import { DocMindSharedWorkspaceError } from '@/utils/docmind-shared-workspace';
import DocMindWorkspaceError from './workspace-error';

let mockError: Error;
let mockState = 'idle';
const mockRevalidate = jest.fn();
jest.mock('react-router', () => ({
  useRouteError: () => mockError,
  useRevalidator: () => ({ state: mockState, revalidate: mockRevalidate }),
}));
jest.mock('@/components/fallback-component', () => ({
  __esModule: true,
  default: () => <p>Unexpected error</p>,
}));

beforeEach(() => {
  jest.useFakeTimers();
  mockError = new DocMindSharedWorkspaceError('Unavailable', 503);
  mockState = 'idle';
  mockRevalidate.mockClear();
});
afterEach(() => jest.useRealTimers());

it('revalidates a transient loader error without reloading the browser', () => {
  const { unmount } = render(<DocMindWorkspaceError />);
  expect(screen.getByRole('alert')).toHaveTextContent('자동으로');
  act(() => jest.advanceTimersByTime(5000));
  expect(mockRevalidate).toHaveBeenCalledTimes(1);
  unmount();
  act(() => jest.advanceTimersByTime(10000));
  expect(mockRevalidate).toHaveBeenCalledTimes(1);
});

it('bounds automatic retries and offers an explicit retry', () => {
  render(<DocMindWorkspaceError />);
  for (let i = 0; i < 15; i++) act(() => jest.advanceTimersByTime(5000));
  expect(mockRevalidate).toHaveBeenCalledTimes(12);
  expect(screen.getByRole('alert')).toHaveTextContent('지연');
  fireEvent.click(screen.getByRole('button', { name: '다시 연결' }));
  expect(mockRevalidate).toHaveBeenCalledTimes(13);
});

it('does not retry unrelated rendering errors or denied authentication', () => {
  mockError = new Error('Render failed');
  const { rerender } = render(<DocMindWorkspaceError />);
  expect(screen.getByText('Unexpected error')).toBeInTheDocument();
  act(() => jest.advanceTimersByTime(10000));
  mockError = new DocMindSharedWorkspaceError('Denied', 403);
  rerender(<DocMindWorkspaceError />);
  act(() => jest.advanceTimersByTime(10000));
  expect(mockRevalidate).not.toHaveBeenCalled();
});

it('does not overlap an in-flight loader retry', () => {
  mockState = 'loading';
  render(<DocMindWorkspaceError />);
  act(() => jest.advanceTimersByTime(10000));
  expect(mockRevalidate).not.toHaveBeenCalled();
  expect(screen.getByRole('button')).toBeDisabled();
});
