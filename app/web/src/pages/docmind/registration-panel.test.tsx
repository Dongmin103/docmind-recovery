import { fireEvent, render, screen } from '@testing-library/react';
import type { DocMindRegistration } from '@/services/docmind-service';
import RegistrationPanel from './registration-panel';

const row = (
  id: string,
  values: Partial<DocMindRegistration> = {},
): DocMindRegistration => ({
  registration_id: id,
  document_id: id,
  document_name: `${id}.pdf`,
  document_exists: true,
  folder_id: 'folder',
  state: 'INDEXED',
  progress: 1,
  chunk_count: 10,
  index_ready: true,
  retry_allowed: false,
  is_current: true,
  ...values,
});
const setup = (
  registrations: DocMindRegistration[],
  documentFolderNames = new Map<string, string>(),
) => {
  const onRetry = jest.fn();
  render(
    <RegistrationPanel
      registrations={registrations}
      folderNames={new Map([['folder', 'Validation']])}
      documentFolderNames={documentFolderNames}
      retryPending={false}
      onRetry={onRetry}
      renderInspection={(r) => <a href={`#${r.document_id}`}>문서 상세</a>}
    />,
  );
  return { onRetry };
};

describe('minimal registration history', () => {
  it('uses the document parent from the source tree when folder IDs have different representations', () => {
    setup(
      [
        row('doc', {
          folder_id: 'node-opaque-slug',
          index_ready: false,
        }),
      ],
      new Map([['doc', 'GMP/추가 자료']]),
    );
    expect(screen.getByText(/GMP\/추가 자료/)).toBeVisible();
    expect(screen.queryByText(/폴더 정보 없음/)).toBeNull();
  });
  it('keeps current work visible and exposes failures/history only on request without mutations', () => {
    const { onRetry } = setup([
      row('work', { index_ready: false }),
      row('published'),
      row('failed', { state: 'FAILED', retry_allowed: true }),
      row('old-failed', {
        state: 'FAILED',
        is_current: false,
        retry_allowed: true,
      }),
    ]);
    expect(screen.getByText('work.pdf')).toBeVisible();
    expect(screen.queryByText('published.pdf')).toBeNull();
    expect(screen.queryByText('failed.pdf')).toBeNull();
    expect(screen.queryByText('old-failed.pdf')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '확인 필요 1' }));
    expect(screen.getByText('failed.pdf')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '등록 기록 4건' }));
    expect(screen.getByText('published.pdf')).toBeVisible();
    expect(screen.getByText('old-failed.pdf')).toBeVisible();
    expect(screen.getAllByRole('button', { name: '재시도' })).toHaveLength(1);
    expect(onRetry).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '등록 기록 닫기' }));
    expect(screen.queryByText('old-failed.pdf')).toBeNull();
    expect(screen.getByText('work.pdf')).toBeVisible();
  });

  it('retains retry actions and never treats history disclosure as an action', () => {
    const { onRetry } = setup([
      row('ready', { index_ready: false }),
      row('failure', { state: 'FAILED', retry_allowed: true }),
    ]);
    fireEvent.click(screen.getByRole('button', { name: '확인 필요 1' }));
    fireEvent.click(screen.getByRole('button', { name: '재시도' }));
    expect(onRetry).toHaveBeenCalledWith('failure');
  });

  it('reveals long histories five at a time without dropping any records', () => {
    setup(Array.from({ length: 12 }, (_, i) => row(`done-${i}`)));
    expect(
      screen.getByText('현재 진행하거나 반영할 작업이 없습니다.'),
    ).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '등록 기록 12건' }));
    expect(screen.getAllByRole('article')).toHaveLength(5);
    fireEvent.click(screen.getByRole('button', { name: '기록 5개 더 보기' }));
    expect(screen.getAllByRole('article')).toHaveLength(10);
    fireEvent.click(screen.getByRole('button', { name: '기록 2개 더 보기' }));
    expect(screen.getAllByRole('article')).toHaveLength(12);
  });
});
