import { buildRagFlowInspectionPath } from './docmind-inspection-link';

export const DocMindSections = [
  'search',
  'library',
  'catalog',
  'settings',
] as const;
export type DocMindSection = (typeof DocMindSections)[number];
export type DocMindView = DocMindSection | 'document';

export function parseDocMindView(value: string | null): DocMindView {
  return value === 'document' ||
    DocMindSections.includes(value as DocMindSection)
    ? (value as DocMindView)
    : 'search';
}

export function parseDocMindReturnView(value: string | null): DocMindSection {
  return value === 'library' || value === 'catalog' ? value : 'search';
}

export function buildDocMindSectionPath(view: DocMindSection): string {
  return `/docmind?view=${view}`;
}

export function buildDocMindDocumentPath(
  datasetId?: string,
  documentId?: string,
  returnView: DocMindSection = 'search',
): string | undefined {
  if (!buildRagFlowInspectionPath(datasetId, documentId)) return undefined;
  const params = new URLSearchParams({
    view: 'document',
    id: datasetId!,
    doc_id: documentId!,
    source: 'docmind',
    return: parseDocMindReturnView(returnView),
  });
  return `/docmind?${params}`;
}
