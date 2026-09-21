import {
  buildDocMindDocumentPath,
  buildDocMindSectionPath,
  parseDocMindReturnView,
  parseDocMindView,
} from './docmind-workspace';

describe('DocMind workspace navigation', () => {
  it('keeps document identity and an allowlisted return section in a same-origin path', () => {
    const path = buildDocMindDocumentPath(
      'a'.repeat(32),
      'b'.repeat(32),
      'library',
    )!;
    const url = new URL(path, 'https://docmind.test');
    expect(url.pathname).toBe('/docmind');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      view: 'document',
      id: 'a'.repeat(32),
      doc_id: 'b'.repeat(32),
      source: 'docmind',
      return: 'library',
    });
    expect(buildDocMindSectionPath('catalog')).toBe('/docmind?view=catalog');
  });

  it.each([
    undefined,
    '',
    'https://external.test/doc',
    '../other',
    'B'.repeat(32),
  ])('rejects invalid document IDs %p', (id) => {
    expect(buildDocMindDocumentPath('a'.repeat(32), id)).toBeUndefined();
  });

  it('does not trust arbitrary view or return URL parameters', () => {
    expect(parseDocMindView('document')).toBe('document');
    expect(parseDocMindView('https://external.test')).toBe('search');
    expect(parseDocMindReturnView('https://external.test')).toBe('search');
    expect(parseDocMindReturnView('document')).toBe('search');
  });
});
