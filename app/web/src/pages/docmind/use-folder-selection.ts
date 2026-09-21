import { useEffect, useRef, type Dispatch, type SetStateAction } from 'react';

/** Choose an initial folder once; a removed selection requires a new choice. */
export function useFolderSelection(
  ids: readonly string[],
  selected: string,
  setSelected: Dispatch<SetStateAction<string>>,
  initial?: string,
) {
  const initialized = useRef(false);
  useEffect(() => {
    if (!initialized.current && ids.length) {
      initialized.current = true;
      if (!selected) {
        setSelected(initial && ids.includes(initial) ? initial : ids[0]);
        return;
      }
    }
    if (selected && !ids.includes(selected)) setSelected('');
  }, [ids, initial, selected, setSelected]);
}
