import { getSourceLocations } from '../source-location';

it('shows every distinct Office contribution after chunk merging', () => {
  expect(getSourceLocations({
    positions: [],
    provenance: [
      { kind: 'xlsx', sheet: '요약', cell_range: 'A1:B3' },
      { kind: 'xlsx', sheet: '세부', cell_range: 'C2:D5' },
      { kind: 'xlsx', sheet: '요약', cell_range: 'A1:B3' },
    ],
  })).toBe('요약!A1:B3 · 세부!C2:D5');
});

it('keeps HWP structural location without inventing page coordinates', () => {
  expect(getSourceLocations({
    positions: [],
    provenance: [{
      kind: 'hwp',
      section_index: 1,
      paragraph_index: 3,
      block_locator: 'section:1/paragraph:3',
    }],
  })).toBe('섹션 2 > 문단 4');
});
