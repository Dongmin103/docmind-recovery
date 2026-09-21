import type { PropsWithChildren } from 'react';
import { Link } from 'react-router';
import {
  buildDocMindSectionPath,
  type DocMindSection,
} from '@/utils/docmind-workspace';

const Sections: {
  id: DocMindSection;
  label: string;
  description: string;
  admin?: boolean;
}[] = [
  { id: 'search', label: '검색', description: '질문에서 문서 근거까지' },
  {
    id: 'library',
    label: '자료 관리',
    description: '폴더 · 문서 · 인덱싱',
    admin: true,
  },
  {
    id: 'catalog',
    label: 'Catalog',
    description: 'L0/L1 · Publish · 버전',
    admin: true,
  },
  { id: 'settings', label: '설정', description: '계정 · 모델' },
];

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
  return (
    <div className="flex h-dvh min-h-0 flex-col bg-bg-base text-text-primary">
      <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-border-button px-5">
        <Link
          to={buildDocMindSectionPath('search')}
          className="flex items-center gap-3 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
          aria-label="DocMind 검색 홈"
        >
          <span
            aria-hidden="true"
            className="grid size-9 place-items-center rounded-xl bg-accent-primary text-primary-foreground font-semibold"
          >
            D
          </span>
          <span className="text-xl font-semibold tracking-tight">DocMind</span>
          <span className="hidden border-l border-border-button pl-3 text-sm text-text-secondary sm:inline">
            문서 검색 · 관리
          </span>
        </Link>
        <span className="text-xs text-text-secondary">
          {sharedWorkspace
            ? '공용 작업 공간'
            : canAdminister
              ? '관리자 작업 공간'
              : '문서 검색 공간'}
        </span>
      </header>
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <aside className="shrink-0 border-b border-border-button bg-bg-card md:w-52 md:border-b-0 md:border-r">
          <nav
            aria-label="DocMind 메뉴"
            className="flex gap-1 overflow-x-auto p-2 md:flex-col md:p-3"
          >
            {Sections.filter((section) => !section.admin || canAdminister).map(
              (section) => (
                <Link
                  key={section.id}
                  to={buildDocMindSectionPath(section.id)}
                  aria-label={section.label}
                  aria-current={
                    activeSection === section.id ? 'page' : undefined
                  }
                  className={`min-w-fit rounded-lg px-4 py-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary ${activeSection === section.id ? 'bg-bg-base font-semibold text-accent-primary shadow-sm' : 'text-text-secondary hover:bg-bg-base hover:text-text-primary'}`}
                >
                  <span className="block">{section.label}</span>
                  <span className="mt-1 hidden text-xs font-normal text-text-secondary md:block">
                    {section.description}
                  </span>
                </Link>
              ),
            )}
          </nav>
          <p className="hidden px-7 py-4 text-xs leading-relaxed text-text-secondary md:block">
            원문 확인부터 검색 반영까지
            <br />
            한곳에서 이어서 작업하세요.
          </p>
        </aside>
        <div className="min-h-0 min-w-0 flex-1">{children}</div>
      </div>
    </div>
  );
}
