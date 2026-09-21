import { act, renderHook } from '@testing-library/react';
import { useState } from 'react';
import { useFolderSelection } from './use-folder-selection';

it('initializes once and clears a removed folder instead of selecting a different target', () => {
  const { result, rerender } = renderHook(
    ({ ids }) => {
      const [selected, setSelected] = useState('');
      useFolderSelection(ids, selected, setSelected, 'root');
      return { selected, setSelected };
    },
    { initialProps: { ids: ['root', 'a', 'b'] } },
  );
  expect(result.current.selected).toBe('root');
  act(() => result.current.setSelected('a'));
  rerender({ ids: ['root', 'a', 'b', 'c'] });
  expect(result.current.selected).toBe('a');
  rerender({ ids: ['root', 'b', 'c'] });
  expect(result.current.selected).toBe('');
  rerender({ ids: ['root', 'b', 'c', 'd'] });
  expect(result.current.selected).toBe('');
  act(() => result.current.setSelected('b'));
  expect(result.current.selected).toBe('b');
});
