import { useState, type ReactNode } from 'react';
import type { DocMindRegistration } from '@/services/docmind-service';

const StateLabels = {
  UPLOADING: '업로드 중',
  UPLOADED: '업로드 완료',
  INDEX_QUEUED: '인덱싱 대기',
  INDEXING: '인덱싱 중',
  INDEXED: '인덱싱 완료',
  FAILED: '실패',
  CANCELLED: '중단됨',
};

type Props = {
  registrations: DocMindRegistration[];
  folderNames: Map<string, string>;
  documentFolderNames?: Map<string, string>;
  hasDraft: boolean;
  draftedDocumentIds: Set<string>;
  retryPending: boolean;
  addPending: boolean;
  onRetry: (id: string) => void;
  onAdd: (registration: DocMindRegistration) => void;
  renderInspection: (registration: DocMindRegistration) => ReactNode;
};

export default function RegistrationPanel(props: Props) {
  const [filter, setFilter] = useState<'work' | 'attention' | 'history'>(
    'work',
  );
  const [limit, setLimit] = useState(5);
  const current = props.registrations.filter((r) => r.is_current);
  const attention = current.filter(
    (r) => r.state === 'FAILED' || r.state === 'CANCELLED',
  );
  const work = current.filter(
    (r) =>
      !attention.includes(r) &&
      !(r.state === 'INDEXED' && r.active_catalog_member),
  );
  const rows =
    filter === 'history'
      ? props.registrations
      : filter === 'attention'
        ? attention
        : work;
  const selectWork = () => {
    setFilter('work');
    setLimit(5);
  };
  const selectAttention = () => {
    setFilter('attention');
    setLimit(5);
  };
  const toggleHistory = () => {
    setFilter(filter === 'history' ? 'work' : 'history');
    setLimit(5);
  };
  const showMore = () => setLimit((value) => value + 5);

  return (
    <section
      className="mt-6 border-t border-border-button pt-5"
      aria-label="문서 처리 현황"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="font-semibold">현재 작업</h3>
        {props.registrations.length > 0 && (
          <button
            type="button"
            onClick={toggleHistory}
            aria-expanded={filter === 'history'}
            className="rounded px-2 py-1 text-xs text-text-secondary hover:text-text-primary focus-visible:ring-2 focus-visible:ring-accent-primary"
          >
            {filter === 'history'
              ? '등록 기록 닫기'
              : `등록 기록 ${props.registrations.length}건`}
          </button>
        )}
      </div>
      <div
        className="mt-3 flex gap-2 text-sm"
        role="group"
        aria-label="작업 보기"
      >
        <button
          type="button"
          onClick={selectWork}
          aria-pressed={filter === 'work'}
          className="rounded-md px-3 py-1.5 text-text-secondary aria-pressed:bg-bg-card aria-pressed:text-text-primary"
        >
          진행·반영 대기 {work.length}
        </button>
        {attention.length > 0 && (
          <button
            type="button"
            onClick={selectAttention}
            aria-pressed={filter === 'attention'}
            className="rounded-md px-3 py-1.5 text-state-warning aria-pressed:bg-state-warning/5"
          >
            확인 필요 {attention.length}
          </button>
        )}
      </div>
      {filter === 'history' && (
        <p className="mt-2 text-xs text-text-secondary">
          완료·실패·이전 시도를 포함한 최근 등록 100건입니다.
        </p>
      )}
      {rows.length === 0 && (
        <p className="py-5 text-sm text-text-secondary">
          {filter === 'work'
            ? '현재 진행하거나 반영할 작업이 없습니다.'
            : '표시할 기록이 없습니다.'}
        </p>
      )}
      <div className="mt-2 divide-y divide-border-button">
        {rows.slice(0, limit).map((registration) => (
          <RegistrationRow
            key={registration.registration_id}
            registration={registration}
            {...props}
          />
        ))}
      </div>
      {rows.length > limit && (
        <button
          type="button"
          onClick={showMore}
          className="mt-3 rounded-lg border border-border-button px-3 py-2 text-xs"
        >
          기록 {Math.min(5, rows.length - limit)}개 더 보기
        </button>
      )}
    </section>
  );
}

function RegistrationRow({
  registration: r,
  ...props
}: Props & { registration: DocMindRegistration }) {
  const retry = () => props.onRetry(r.registration_id);
  const add = () => props.onAdd(r);
  const canAdd =
    r.document_exists &&
    r.is_current &&
    r.draft_eligible &&
    props.hasDraft &&
    !r.active_catalog_member &&
    !props.draftedDocumentIds.has(r.document_id);
  const message = r.parser_run?.error_message || r.error_message;
  return (
    <article className="py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">
            {r.document_name || '이름 없는 문서'}
          </p>
          <p className="mt-1 text-xs text-text-secondary">
            {props.documentFolderNames?.get(r.document_id) ||
              props.folderNames.get(r.folder_id || '') ||
              '폴더 정보 없음'}{' '}
            · {r.chunk_count}개 청크 ·{' '}
            {r.active_catalog_member ? '검색 반영됨' : '검색 미반영'}
          </p>
        </div>
        <span
          className={`text-xs ${r.state === 'FAILED' ? 'text-state-warning' : 'text-text-secondary'}`}
        >
          {StateLabels[r.state]}
          {r.state === 'INDEXING' ? ` ${Math.round(r.progress * 100)}%` : ''}
        </span>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {r.document_exists && props.renderInspection(r)}
          {canAdd && (
            <button
              type="button"
              onClick={add}
              disabled={props.addPending}
              className="rounded border border-border-button px-2 py-1 disabled:opacity-40"
            >
              초안에 추가
            </button>
          )}
          {r.document_exists && r.is_current && r.retry_allowed && (
            <button
              type="button"
              onClick={retry}
              disabled={props.retryPending}
              className="rounded border border-border-button px-2 py-1 disabled:opacity-40"
            >
              재시도
            </button>
          )}
        </div>
      </div>
      {message && <p className="mt-2 text-xs text-state-warning">{message}</p>}
      <details className="mt-2 text-xs text-text-secondary">
        <summary className="w-fit cursor-pointer">
          처리 정보{!r.is_current ? ' · 이전 시도' : ''}
        </summary>
        <div className="mt-2 grid gap-1 break-words">
          <p>
            진행률 {Math.round(r.progress * 100)}% ·{' '}
            {r.parser_run?.parser_name || '문서 처리'}
          </p>
          {r.parser_run && (
            <p>
              {r.parser_run.parser_version} · {r.parser_run.phase}
            </p>
          )}
          {r.blocker_code && <p>{r.blocker_code}</p>}
          {r.error_code && r.error_code !== r.blocker_code && (
            <p>{r.error_code}</p>
          )}
          {r.parser_run?.error_code && <p>{r.parser_run.error_code}</p>}
          <p>{r.registration_id}</p>
        </div>
      </details>
    </article>
  );
}
