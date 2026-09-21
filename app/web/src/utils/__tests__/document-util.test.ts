import { buildChunkHighlights } from '../document-util';

const pageSize = { width: 600, height: 800 };

const createChunk = (positions: unknown) =>
  ({
    content_with_weight: 'matching passage',
    positions,
  }) as any;

describe('buildChunkHighlights', () => {
  it('creates one highlight rectangle for every valid position', () => {
    const highlights = buildChunkHighlights(
      createChunk([
        [3, 12, 48, 70, 112],
        [4, 22, 58, 80, 122],
      ]),
      pageSize,
    );

    expect(highlights).toHaveLength(2);
    expect(highlights.map((highlight) => highlight.position)).toEqual([
      {
        pageNumber: 3,
        boundingRect: {
          width: 600,
          height: 800,
          x1: 12,
          x2: 48,
          y1: 70,
          y2: 112,
        },
        rects: [
          {
            width: 600,
            height: 800,
            x1: 12,
            x2: 48,
            y1: 70,
            y2: 112,
          },
        ],
      },
      {
        pageNumber: 4,
        boundingRect: {
          width: 600,
          height: 800,
          x1: 22,
          x2: 58,
          y1: 80,
          y2: 122,
        },
        rects: [
          {
            width: 600,
            height: 800,
            x1: 22,
            x2: 58,
            y1: 80,
            y2: 122,
          },
        ],
      },
    ]);
  });

  it.each([undefined, [], [[3, 12, 48, 70]], [[3, 12, Infinity, 70, 112]]])(
    'returns no highlights for missing, empty, or malformed positions: %p',
    (positions) => {
      expect(buildChunkHighlights(createChunk(positions), pageSize)).toEqual([]);
    },
  );

  it('keeps valid positions when the same chunk contains malformed tuples', () => {
    const highlights = buildChunkHighlights(
      createChunk([
        [3, 12, 48, 70, 112],
        ['three', 12, 48, 70, 112],
        [4, 22, 58, 80],
        [5, 32, 68, 90, 132],
      ]),
      pageSize,
    );

    expect(highlights).toHaveLength(2);
    expect(highlights.map((highlight) => highlight.position.pageNumber)).toEqual([
      3,
      5,
    ]);
  });
});
