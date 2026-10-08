import { fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import type { DocMindFolder } from '@/services/docmind-service';
import ScopeFolderPicker from './scope-folder-picker';

const Folders: DocMindFolder[] = [
  { id: 'quality', name: '품질', relative_path: '품질', document_count: 2 },
  {
    id: 'reports',
    name: '보고서',
    parent_id: 'quality',
    relative_path: '품질/보고서',
    document_count: 3,
  },
  {
    id: 'audits',
    name: '감사',
    parent_id: 'reports',
    relative_path: '품질/보고서/감사',
    document_count: 1,
  },
  { id: 'sales', name: '영업', relative_path: '영업', document_count: 4 },
];

function PickerHarness({
  folders = Folders,
  selected = [],
}: {
  folders?: DocMindFolder[];
  selected?: string[];
}) {
  const [ids, setIds] = useState(selected);
  const toggle = (id: string) =>
    setIds((current) =>
      current.includes(id)
        ? current.filter((value) => value !== id)
        : [...current, id],
    );
  return (
    <>
      <ScopeFolderPicker
        folders={folders}
        selectedIds={ids}
        onToggle={toggle}
      />
      <output aria-label="선택 ID">{ids.join(',')}</output>
    </>
  );
}

it('changes only the clicked ID and distinguishes included descendants from explicit selections', () => {
  render(<PickerHarness />);
  fireEvent.click(screen.getByRole('checkbox', { name: '품질' }));
  expect(screen.getByLabelText('선택 ID')).toHaveTextContent(/^quality$/);
  expect(screen.getByRole('checkbox', { name: '보고서' })).not.toBeChecked();
  expect(
    screen.getByRole('checkbox', { name: '보고서' }),
  ).toHaveAccessibleDescription('상위 폴더에 포함');
  fireEvent.click(screen.getByRole('checkbox', { name: '보고서' }));
  fireEvent.click(screen.getByRole('checkbox', { name: '품질' }));
  expect(screen.getByLabelText('선택 ID')).toHaveTextContent(/^reports$/);
  expect(screen.getByRole('checkbox', { name: '보고서' })).toBeChecked();
  expect(
    within(
      screen.getByRole('checkbox', { name: '품질' }).closest('li')!,
    ).getByText('하위 1개 선택'),
  ).toBeInTheDocument();
});

it('preserves selected descendants when their branch is collapsed and reopened', () => {
  render(<PickerHarness selected={['audits']} />);
  fireEvent.click(screen.getByRole('button', { name: '품질 하위 폴더 접기' }));
  expect(
    screen.queryByRole('checkbox', { name: '감사' }),
  ).not.toBeInTheDocument();
  expect(screen.getByText('폴더 1개 선택')).toBeInTheDocument();
  expect(screen.getByLabelText('선택 ID')).toHaveTextContent('audits');
  const expand = screen.getByRole('button', { name: '품질 하위 폴더 펼치기' });
  expect(expand).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(expand);
  expect(screen.getByRole('checkbox', { name: '감사' })).toBeChecked();
});

it('reveals a matching descendant and its ancestors during filtering without changing selection or collapse state', () => {
  render(<PickerHarness selected={['sales']} />);
  fireEvent.click(screen.getByRole('button', { name: '품질 하위 폴더 접기' }));
  fireEvent.change(screen.getByRole('searchbox', { name: '폴더 찾기' }), {
    target: { value: '감사' },
  });
  expect(screen.getByRole('checkbox', { name: '품질' })).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: '보고서' })).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: '감사' })).toBeInTheDocument();
  expect(
    screen.queryByRole('checkbox', { name: '영업' }),
  ).not.toBeInTheDocument();
  expect(screen.getByLabelText('선택 ID')).toHaveTextContent(/^sales$/);
  fireEvent.change(screen.getByRole('searchbox', { name: '폴더 찾기' }), {
    target: { value: '' },
  });
  expect(
    screen.queryByRole('checkbox', { name: '감사' }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: '영업' })).toBeChecked();
});

it('renders every folder once despite cycles, self parents, or missing parents', () => {
  const folders = [
    { id: 'a', name: 'A', parent_id: 'b', document_count: 0 },
    { id: 'b', name: 'B', parent_id: 'a', document_count: 0 },
    { id: 'self', name: '자기', parent_id: 'self', document_count: 0 },
    { id: 'orphan', name: '미연결', parent_id: 'missing', document_count: 0 },
  ];
  render(<PickerHarness folders={folders} />);
  expect(screen.getAllByRole('checkbox')).toHaveLength(4);
  for (const name of ['A', 'B', '자기', '미연결'])
    expect(screen.getAllByRole('checkbox', { name })).toHaveLength(1);
  fireEvent.click(screen.getByRole('checkbox', { name: 'B' }));
  expect(screen.getByLabelText('선택 ID')).toHaveTextContent(/^b$/);
});

it('uses an exact unique parent path only when no parent ID is supplied', () => {
  const folders = [
    { id: 'root', name: '문서함', relative_path: '문서함', document_count: 0 },
    {
      id: 'child',
      name: '하위',
      relative_path: '문서함/하위',
      document_count: 1,
    },
    {
      id: 'prefix',
      name: '비슷한 경로',
      relative_path: '문서함2/하위',
      document_count: 1,
    },
    {
      id: 'explicit',
      name: '명시 부모 우선',
      parent_id: 'unknown',
      relative_path: '문서함/다른폴더',
      document_count: 1,
    },
    { id: 'dup1', name: '중복1', relative_path: '중복', document_count: 0 },
    { id: 'dup2', name: '중복2', relative_path: '중복', document_count: 0 },
    {
      id: 'ambiguous',
      name: '모호한 자식',
      relative_path: '중복/하위',
      document_count: 1,
    },
  ];
  render(<PickerHarness folders={folders} />);
  fireEvent.click(
    screen.getByRole('button', { name: '문서함 하위 폴더 접기' }),
  );
  expect(
    screen.queryByRole('checkbox', { name: '하위' }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole('checkbox', { name: '비슷한 경로' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('checkbox', { name: '명시 부모 우선' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('checkbox', { name: '모호한 자식' }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: '중복1 하위 폴더 접기' }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: '중복2 하위 폴더 접기' }),
  ).not.toBeInTheDocument();
});

it('keeps supplied folder counts independent instead of summing descendants', () => {
  render(<PickerHarness />);
  const row = screen.getByRole('checkbox', { name: '품질' }).closest('li')!;
  expect(within(row).getByText('문서 2개')).toBeInTheDocument();
  expect(within(row).queryByText('문서 6개')).not.toBeInTheDocument();
  expect(
    screen.getByText('상위 폴더를 선택하면 하위 폴더도 검색합니다.'),
  ).toBeInTheDocument();
});

const DeepFolders: DocMindFolder[] = [
  { id: 'root', name: '자료', document_count: 0 },
  { id: 'division', name: '부서', parent_id: 'root', document_count: 0 },
  { id: 'project', name: '프로젝트', parent_id: 'division', document_count: 0 },
  { id: 'year', name: '연도별', parent_id: 'project', document_count: 0 },
  {
    id: 'archive',
    name: '2026년 문서 검토와 승인 결과를 보관하는 폴더',
    parent_id: 'year',
    document_count: 2,
  },
];

it('shows the first two expanded levels as nested lists and requires disclosure for deeper branches', () => {
  render(<PickerHarness folders={DeepFolders} />);
  expect(
    screen.getByRole('button', { name: '자료 하위 폴더 접기' }),
  ).toHaveAttribute('aria-expanded', 'true');
  expect(
    screen.getByRole('button', { name: '부서 하위 폴더 접기' }),
  ).toHaveAttribute('aria-expanded', 'true');
  const project = screen.getByRole('button', {
    name: '프로젝트 하위 폴더 펼치기',
  });
  expect(project).toHaveAttribute('aria-expanded', 'false');
  expect(
    screen.queryByRole('checkbox', { name: '연도별' }),
  ).not.toBeInTheDocument();
  const root = screen.getByRole('checkbox', { name: '자료' }).closest('li')!;
  expect(
    within(root).getByRole('checkbox', { name: '부서' }),
  ).toBeInTheDocument();
  fireEvent.click(project);
  expect(screen.getByRole('checkbox', { name: '연도별' })).toBeInTheDocument();
  expect(
    screen.queryByRole('checkbox', { name: DeepFolders[4].name }),
  ).not.toBeInTheDocument();
  expect(screen.getByLabelText('선택 ID')).toBeEmptyDOMElement();
});

it('reveals an initially selected deep path but respects an explicit collapse without losing its ID', () => {
  render(<PickerHarness folders={DeepFolders} selected={['archive']} />);
  expect(
    screen.getByRole('checkbox', { name: DeepFolders[4].name }),
  ).toBeChecked();
  fireEvent.click(
    screen.getByRole('button', { name: '프로젝트 하위 폴더 접기' }),
  );
  expect(
    screen.queryByRole('checkbox', { name: DeepFolders[4].name }),
  ).not.toBeInTheDocument();
  expect(screen.getByLabelText('선택 ID')).toHaveTextContent(/^archive$/);
  fireEvent.change(screen.getByRole('searchbox', { name: '폴더 찾기' }), {
    target: { value: '승인 결과' },
  });
  expect(
    screen.getByRole('checkbox', { name: DeepFolders[4].name }),
  ).toBeChecked();
  fireEvent.change(screen.getByRole('searchbox', { name: '폴더 찾기' }), {
    target: { value: '' },
  });
  expect(
    screen.queryByRole('checkbox', { name: DeepFolders[4].name }),
  ).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole('button', { name: '프로젝트 하위 폴더 펼치기' }),
  );
  expect(
    screen.getByRole('checkbox', { name: DeepFolders[4].name }),
  ).toBeChecked();
});

it('shows a parent as containing subfolders instead of reporting its zero direct count as empty', () => {
  render(<PickerHarness folders={DeepFolders} />);
  const root = screen.getByRole('checkbox', { name: '자료' }).closest('label')!;
  expect(within(root).queryByText('문서 0개')).not.toBeInTheDocument();
  expect(within(root).getByText('하위 폴더 1개')).toBeInTheDocument();
});
