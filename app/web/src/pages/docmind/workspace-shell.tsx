import {
  useEffect,
  useId,
  useRef,
  useState,
  type PropsWithChildren,
} from 'react';
import { Link } from 'react-router';
import {
  buildDocMindSectionPath,
  type DocMindSection,
} from '@/utils/docmind-workspace';
import './workspace-shell.less';

const MobileMenuQuery = '(max-width: 767px)';
const Sections: {
  id: DocMindSection;
  label: string;
  icon: string;
  admin?: boolean;
}[] = [
  {
    id: 'search',
    label: '검색',
    icon: 'm21 21-4.3-4.3 M19 10.5a8.5 8.5 0 1 1-17 0 8.5 8.5 0 0 1 17 0',
  },
  {
    id: 'library',
    label: '자료 관리',
    icon: 'M3 7V5a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z',
    admin: true,
  },
];

function ShellIcon({ path }: { path: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={path} />
    </svg>
  );
}

export default function WorkspaceShell({
  activeSection,
  canAdminister,
  sharedWorkspace = false,
  children,
}: PropsWithChildren<{
  activeSection: DocMindSection;
  canAdminister: boolean;
  sharedWorkspace?: boolean;
}>) {
  const menuId = useId();
  const menuToggle = useRef<HTMLButtonElement>(null);
  const [isMobile, setIsMobile] = useState(
    () => window.matchMedia?.(MobileMenuQuery).matches ?? false,
  );
  const [isMenuOpen, setIsMenuOpen] = useState(false);

  useEffect(() => {
    const media = window.matchMedia?.(MobileMenuQuery);
    if (!media) return;
    const handleMediaChange = (event: MediaQueryListEvent) => {
      setIsMobile(event.matches);
      setIsMenuOpen(false);
    };
    media.addEventListener('change', handleMediaChange);
    return () => media.removeEventListener('change', handleMediaChange);
  }, []);

  useEffect(() => {
    if (!isMobile || !isMenuOpen) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setIsMenuOpen(false);
      menuToggle.current?.focus();
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isMobile, isMenuOpen]);

  const handleMenuToggle = () => setIsMenuOpen((open) => !open);
  const handleNavigation = () => {
    if (!isMobile) return;
    setIsMenuOpen(false);
    menuToggle.current?.focus();
  };

  return (
    <div className="docmind-workspace docmind-shell">
      <header className="docmind-shell-header">
        <div className="docmind-shell-brand-group">
          {isMobile && (
            <button
              ref={menuToggle}
              type="button"
              className="docmind-shell-menu-toggle"
              aria-label={isMenuOpen ? '메뉴 닫기' : '메뉴 열기'}
              aria-expanded={isMenuOpen}
              aria-controls={menuId}
              onClick={handleMenuToggle}
            >
              <ShellIcon
                path={
                  isMenuOpen
                    ? 'm6 6 12 12M6 18 18 6'
                    : 'M4 6h16M4 12h16M4 18h16'
                }
              />
            </button>
          )}
          <Link
            to={buildDocMindSectionPath('search')}
            className="docmind-shell-brand"
            aria-label="DocMind 검색 홈"
            onClick={handleNavigation}
          >
            <span aria-hidden="true" className="docmind-shell-mark">
              D
            </span>
            <span className="docmind-shell-name">DocMind</span>
          </Link>
        </div>
        <span className="docmind-shell-workspace-label">
          {sharedWorkspace
            ? '공용 작업 공간'
            : canAdminister
              ? '관리자 작업 공간'
              : '문서 검색 공간'}
        </span>
      </header>
      <div className="docmind-shell-body">
        <aside
          id={menuId}
          className="docmind-shell-sidebar"
          hidden={isMobile && !isMenuOpen}
        >
          <nav aria-label="DocMind 메뉴" className="docmind-shell-nav">
            {Sections.filter((section) => !section.admin || canAdminister).map(
              (section) => (
                <Link
                  key={section.id}
                  to={buildDocMindSectionPath(section.id)}
                  aria-label={section.label}
                  aria-current={
                    activeSection === section.id ? 'page' : undefined
                  }
                  className="docmind-shell-nav-link"
                  onClick={handleNavigation}
                >
                  <ShellIcon path={section.icon} />
                  <span>{section.label}</span>
                </Link>
              ),
            )}
          </nav>
        </aside>
        <div className="docmind-shell-content">{children}</div>
      </div>
    </div>
  );
}
