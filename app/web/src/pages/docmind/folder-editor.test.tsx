import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  createDocMindHierarchyFolder,
  updateDocMindHierarchyFolder,
  deleteDocMindHierarchyFolder,
} from '@/services/docmind-service';
import FolderEditor from './folder-editor';

type DocMindHierarchyNode =
  import('@/services/docmind-service').DocMindHierarchyNode;

jest.mock('@/services/docmind-service', () => ({
  createDocMindHierarchyFolder: jest.fn(),
  updateDocMindHierarchyFolder: jest.fn(),
  deleteDocMindHierarchyFolder: jest.fn(),
}));

const folder = (
  id: string,
  name: string,
  parent = 'root',
  extra: Partial<DocMindHierarchyNode> = {},
): DocMindHierarchyNode => ({
  file_id: id,
  name,
  parent_file_id: parent,
  relative_path: name,
  depth: id === 'root' ? 0 : 1,
  type: 'folder',
  document_exists: false,
  mutation_capabilities: {
    can_create_child: true,
    can_edit: true,
    can_delete: true,
    edit_blocker_code: null,
    delete_blocker_code: null,
  },
  ...extra,
});
const Root = folder('root', 'GMP', 'root', {
  mutation_capabilities: {
    can_create_child: true,
    can_edit: false,
    can_delete: false,
    edit_blocker_code: 'DOCMIND_SOURCE_ROOT_IMMUTABLE',
    delete_blocker_code: 'DOCMIND_SOURCE_ROOT_IMMUTABLE',
  },
});
const Free = folder('free', '일반 폴더');
const Protected = folder('protected', '보호 자료', 'root', {
  mutation_capabilities: {
    can_create_child: true,
    can_edit: false,
    can_delete: false,
    edit_blocker_code: 'DOCMIND_FOLDER_PHYSICALLY_PROTECTED',
    delete_blocker_code: 'DOCMIND_FOLDER_PHYSICALLY_PROTECTED',
  },
});
const Nonempty = folder('nonempty', '문서가 있는 폴더', 'root', {
  mutation_capabilities: {
    can_create_child: true,
    can_edit: true,
    can_delete: false,
    edit_blocker_code: null,
    delete_blocker_code: 'DOCMIND_FOLDER_DELETE_NONEMPTY',
  },
});
const Nodes = [
  Root,
  Free,
  Protected,
  Nonempty,
  folder('child', '하위 폴더', 'nonempty'),
];

function setup(nodes = Nodes, changed = jest.fn(async () => {})) {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  const view = (items: DocMindHierarchyNode[]) => (
    <QueryClientProvider client={client}>
      <FolderEditor nodes={items} rootId="root" onChanged={changed} />
    </QueryClientProvider>
  );
  const rendered = render(view(nodes));
  return {
    changed,
    rerender: (items: DocMindHierarchyNode[]) => rendered.rerender(view(items)),
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

it('shows precise protection reasons and never sends forbidden or unchanged mutations', () => {
  setup();
  fireEvent.click(
    screen.getByRole('button', { name: '작업 선택: 이름 바꾸기' }),
  );
  fireEvent.change(screen.getByLabelText('이름을 바꿀 폴더'), {
    target: { value: 'protected' },
  });
  expect(screen.getByRole('note')).toHaveTextContent('Catalog 초안·검색 버전');
  expect(screen.getByRole('button', { name: '이름 바꾸기' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('이름을 바꿀 폴더'), {
    target: { value: 'free' },
  });
  expect(screen.getByRole('button', { name: '이름 바꾸기' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '작업 선택: 삭제' }));
  fireEvent.change(screen.getByLabelText('삭제할 폴더'), {
    target: { value: 'nonempty' },
  });
  expect(screen.getByRole('note')).toHaveTextContent('비어 있는 폴더만 삭제');
  expect(screen.getByRole('button', { name: '빈 폴더 삭제' })).toBeDisabled();
  expect(updateDocMindHierarchyFolder).not.toHaveBeenCalled();
  expect(deleteDocMindHierarchyFolder).not.toHaveBeenCalled();
});

it('selects a newly created folder and announces success even before the refreshed tree arrives', async () => {
  (createDocMindHierarchyFolder as jest.Mock).mockResolvedValue({
    data: {
      code: 0,
      data: { folder_id: 'new', name: '신규 자료', parent_file_id: 'root' },
    },
  });
  const { rerender } = setup();
  fireEvent.change(screen.getByLabelText('추가할 폴더 이름'), {
    target: { value: '신규 자료' },
  });
  fireEvent.click(screen.getByRole('button', { name: '폴더 추가 실행' }));
  expect(await screen.findByRole('status')).toHaveTextContent(
    '추가하고 이름 바꾸기 대상으로 선택',
  );
  expect(createDocMindHierarchyFolder).toHaveBeenCalledWith(
    'root',
    '신규 자료',
  );
  expect(screen.getByLabelText('이름을 바꿀 폴더')).toHaveValue('new');
  rerender([...Nodes, folder('new', '신규 자료')]);
  await waitFor(() =>
    expect(screen.getByLabelText('폴더 새 이름')).toHaveValue('신규 자료'),
  );
  expect(screen.getByRole('button', { name: '이름 바꾸기' })).toBeDisabled();
});

it('submits an actual name change without showing a location field', async () => {
  (updateDocMindHierarchyFolder as jest.Mock).mockResolvedValue({
    data: {
      code: 0,
      data: {
        folder_id: 'nonempty',
        name: '바뀐 이름',
        parent_file_id: 'root',
      },
    },
  });
  setup();
  fireEvent.click(
    screen.getByRole('button', { name: '작업 선택: 이름 바꾸기' }),
  );
  fireEvent.change(screen.getByLabelText('이름을 바꿀 폴더'), {
    target: { value: 'nonempty' },
  });
  expect(screen.queryByLabelText('새 위치의 부모 폴더')).toBeNull();
  fireEvent.change(screen.getByLabelText('폴더 새 이름'), {
    target: { value: '바뀐 이름' },
  });
  fireEvent.click(screen.getByRole('button', { name: '이름 바꾸기' }));
  await waitFor(() =>
    expect(updateDocMindHierarchyFolder).toHaveBeenCalledWith('nonempty', {
      name: '바뀐 이름',
      parentFileId: undefined,
    }),
  );
  expect(await screen.findByRole('status')).toHaveTextContent('이름을');
});

it('moves a folder without showing a rename field and excludes its descendants', async () => {
  (updateDocMindHierarchyFolder as jest.Mock).mockResolvedValue({
    data: {
      code: 0,
      data: {
        folder_id: 'nonempty',
        name: '문서가 있는 폴더',
        parent_file_id: 'free',
      },
    },
  });
  setup();
  fireEvent.click(
    screen.getByRole('button', { name: '작업 선택: 위치 옮기기' }),
  );
  fireEvent.change(screen.getByLabelText('위치를 옮길 폴더'), {
    target: { value: 'nonempty' },
  });
  expect(screen.queryByLabelText('폴더 새 이름')).toBeNull();
  const destination = screen.getByLabelText('새 위치의 부모 폴더');
  expect(destination.querySelector('option[value="nonempty"]')).toBeNull();
  expect(destination.querySelector('option[value="child"]')).toBeNull();
  fireEvent.change(destination, { target: { value: 'free' } });
  fireEvent.click(screen.getByRole('button', { name: '위치 옮기기' }));
  await waitFor(() =>
    expect(updateDocMindHierarchyFolder).toHaveBeenCalledWith('nonempty', {
      name: undefined,
      parentFileId: 'free',
    }),
  );
  expect(await screen.findByRole('status')).toHaveTextContent(
    '위치를 옮겼습니다',
  );
});

it('keeps a successful operation distinct from a failed list refresh', async () => {
  (createDocMindHierarchyFolder as jest.Mock).mockResolvedValue({
    data: {
      code: 0,
      data: { folder_id: 'new', name: '신규 자료', parent_file_id: 'root' },
    },
  });
  setup(
    Nodes,
    jest.fn(async () => {
      throw new Error('offline');
    }),
  );
  fireEvent.change(screen.getByLabelText('추가할 폴더 이름'), {
    target: { value: '신규 자료' },
  });
  fireEvent.click(screen.getByRole('button', { name: '폴더 추가 실행' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '성공한 작업을 다시 실행하지 말고',
  );
  expect(screen.getByRole('status')).toHaveTextContent(
    '추가하고 이름 바꾸기 대상으로 선택',
  );
  expect(screen.getByRole('button', { name: '이름 바꾸기' })).toBeDisabled();
  expect(createDocMindHierarchyFolder).toHaveBeenCalledTimes(1);
});

it('requires confirmation for an allowed deletion and clears the selected target after success', async () => {
  const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
  (deleteDocMindHierarchyFolder as jest.Mock).mockResolvedValue({
    data: { code: 0, data: { folder_id: 'free', deleted: true } },
  });
  try {
    setup();
    fireEvent.click(screen.getByRole('button', { name: '작업 선택: 삭제' }));
    fireEvent.change(screen.getByLabelText('삭제할 폴더'), {
      target: { value: 'free' },
    });
    fireEvent.click(screen.getByRole('button', { name: '빈 폴더 삭제' }));
    expect(deleteDocMindHierarchyFolder).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: '빈 폴더 삭제' }));
    expect(await screen.findByRole('status')).toHaveTextContent(
      '빈 폴더를 삭제',
    );
    expect(deleteDocMindHierarchyFolder).toHaveBeenCalledWith('free');
    expect(screen.getByLabelText('삭제할 폴더')).toHaveValue('');
  } finally {
    confirm.mockRestore();
  }
});

it('does not enable mutations when capability metadata is unavailable', () => {
  setup([folder('root', 'GMP', 'root', { mutation_capabilities: undefined })]);
  fireEvent.change(screen.getByLabelText('추가할 폴더 이름'), {
    target: { value: '자료' },
  });
  expect(screen.getByRole('button', { name: '폴더 추가 실행' })).toBeDisabled();
  expect(screen.getByText(/권한을 확인하려면/)).toBeVisible();
});
