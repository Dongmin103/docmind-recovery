import { buildRagFlowInspectionPath } from './docmind-inspection-link';

describe('buildRagFlowInspectionPath', () => {
  it('builds the fixed same-origin RAGFlow inspection path', () => {
    expect(
      buildRagFlowInspectionPath(
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      ),
    ).toBe(
      '/chunk/parsed/chunks?id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&doc_id=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&source=docmind',
    );
  });

  it.each([
    [undefined, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
    ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', undefined],
    ['', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
    [' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
    ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb '],
    ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
    ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb?'],
    ['https://example.com/dataset-id', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
    ['AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
    ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
    ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
  ])(
    'fails closed for a missing or invalid identifier',
    (datasetId, documentId) => {
      expect(buildRagFlowInspectionPath(datasetId, documentId)).toBeUndefined();
    },
  );
});
