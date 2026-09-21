const ragFlowIdPattern = /^[0-9a-f]{32}$/;

export function buildRagFlowInspectionPath(
  datasetId?: string,
  documentId?: string,
): string | undefined {
  if (!datasetId || !documentId) return undefined;
  if (!ragFlowIdPattern.test(datasetId) || !ragFlowIdPattern.test(documentId)) {
    return undefined;
  }

  const params = new URLSearchParams({
    id: datasetId,
    doc_id: documentId,
    source: 'docmind',
  });
  return `/chunk/parsed/chunks?${params.toString()}`;
}
