import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import WorkspaceShell from './workspace-shell';

function LocationStatus() {
  const location = useLocation();
  return (
    <output aria-label="현재 경로">
      {location.pathname + location.search}
    </output>
  );
}

function renderShell(canAdminister = true) {
  return render(
    <MemoryRouter initialEntries={['/docmind?view=search']}>
      <WorkspaceShell activeSection="search" canAdminister={canAdminister}>
        <LocationStatus />
      </WorkspaceShell>
    </MemoryRouter>,
  );
}

describe('WorkspaceShell mobile navigation', () => {
  const OriginalMatchMedia = window.matchMedia;
  let mediaChange: ((event: MediaQueryListEvent) => void) | undefined;
  let mobile: boolean;

  beforeEach(() => {
    mobile = true;
    mediaChange = undefined;
    window.matchMedia = jest.fn().mockImplementation(() => ({
      get matches() {
        return mobile;
      },
      media: '(max-width: 767px)',
      onchange: null,
      addListener: jest.fn(),
      removeListener: jest.fn(),
      addEventListener: (_type: string, listener: typeof mediaChange) => {
        mediaChange = listener;
      },
      removeEventListener: jest.fn(),
      dispatchEvent: jest.fn(),
    }));
  });

  afterEach(() => {
    window.matchMedia = OriginalMatchMedia;
  });

  it('keeps collapsed links out of navigation and exposes the controlled menu when opened', () => {
    renderShell();
    const toggle = screen.getByRole('button', { name: '메뉴 열기' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(
      screen.queryByRole('navigation', { name: 'DocMind 메뉴' }),
    ).not.toBeInTheDocument();

    fireEvent.click(toggle);
    const menu = screen.getByRole('navigation', { name: 'DocMind 메뉴' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(
      document.getElementById(toggle.getAttribute('aria-controls')!),
    ).toContainElement(menu);
    expect(within(menu).getByRole('link', { name: '검색' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(
      within(menu).queryByRole('link', { name: '설정' }),
    ).not.toBeInTheDocument();
  });

  it('closes on Escape and returns focus from a menu link to its toggle', () => {
    renderShell();
    const toggle = screen.getByRole('button', { name: '메뉴 열기' });
    fireEvent.click(toggle);
    screen.getByRole('link', { name: '자료 관리' }).focus();

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });

    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveFocus();
    expect(
      screen.queryByRole('navigation', { name: 'DocMind 메뉴' }),
    ).not.toBeInTheDocument();
  });

  it('closes after section navigation while preserving the destination URL', () => {
    renderShell();
    const toggle = screen.getByRole('button', { name: '메뉴 열기' });
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole('link', { name: '자료 관리' }));

    expect(screen.getByLabelText('현재 경로')).toHaveTextContent(
      '/docmind?view=library',
    );
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(
      screen.queryByRole('navigation', { name: 'DocMind 메뉴' }),
    ).not.toBeInTheDocument();
  });

  it('shows navigation on desktop and restores a collapsed menu when returning to mobile', () => {
    renderShell();
    act(() => {
      mobile = false;
      mediaChange?.({ matches: false } as MediaQueryListEvent);
    });
    expect(
      screen.getByRole('navigation', { name: 'DocMind 메뉴' }),
    ).toBeVisible();
    expect(
      screen.queryByRole('button', { name: /메뉴/ }),
    ).not.toBeInTheDocument();

    act(() => {
      mobile = true;
      mediaChange?.({ matches: true } as MediaQueryListEvent);
    });
    expect(screen.getByRole('button', { name: '메뉴 열기' })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
    expect(
      screen.queryByRole('navigation', { name: 'DocMind 메뉴' }),
    ).not.toBeInTheDocument();
  });

  it('does not expose administrative navigation to a search-only user', () => {
    renderShell(false);
    fireEvent.click(screen.getByRole('button', { name: '메뉴 열기' }));
    const menu = screen.getByRole('navigation', { name: 'DocMind 메뉴' });
    expect(
      within(menu).queryByRole('link', { name: '자료 관리' }),
    ).not.toBeInTheDocument();
    expect(within(menu).getByRole('link', { name: '검색' })).toHaveAttribute(
      'href',
      '/docmind?view=search',
    );
    expect(
      within(menu).queryByRole('link', { name: '설정' }),
    ).not.toBeInTheDocument();
  });
});
