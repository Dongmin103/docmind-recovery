import type { IChunk } from '@/interfaces/database/dataset';

type LocationChunk = Pick<IChunk, 'positions' | 'provenance' | 'office_locator' | 'hwp_locator' | 'parser_platform'>;
type Locator = Record<string, unknown>;

function label(locator: Locator): string | undefined {
  if (locator.kind === 'docx') {
    const path = Array.isArray(locator.heading_path)
      ? locator.heading_path.filter((part): part is string => typeof part === 'string').join(' > ')
      : '';
    return path || (typeof locator.item_locator === 'string' ? locator.item_locator : 'DOCX 구조 위치');
  }
  if (locator.kind === 'xlsx' && typeof locator.sheet === 'string') {
    const place = locator.cell_range || locator.region_locator;
    return `${locator.sheet}${typeof place === 'string' ? `!${place}` : ''}`;
  }
  if (locator.kind === 'pptx' && typeof locator.slide === 'number') {
    return `슬라이드 ${locator.slide}`;
  }
  if (locator.kind === 'hwp' || locator.kind === 'hwpx') {
    if (typeof locator.section_index !== 'number') return undefined;
    const section = `섹션 ${locator.section_index + 1}`;
    const table = locator.table as { row?: number; column?: number } | undefined;
    if (typeof table?.row === 'number' && typeof table?.column === 'number') {
      return `${section} > 표 ${table.row + 1}행 ${table.column + 1}열`;
    }
    if (typeof locator.paragraph_index === 'number') {
      return `${section} > 문단 ${locator.paragraph_index + 1}`;
    }
    return typeof locator.block_locator === 'string'
      ? `${section} > ${locator.block_locator}`
      : section;
  }
  if (locator.kind === 'pdf' && typeof locator.page === 'number') {
    return `p. ${locator.page}`;
  }
  return undefined;
}

export function getSourceLocations(chunk: LocationChunk): string {
  const provenance = Array.isArray(chunk.provenance) ? chunk.provenance : [];
  const officeLocators = Array.isArray(chunk.parser_platform?.office_locators)
    ? chunk.parser_platform.office_locators
    : [];
  const locators: Locator[] = (provenance.length ? provenance : officeLocators)
    .filter((value): value is Locator => !!value && typeof value === 'object');
  if (!locators.length) {
    if (chunk.hwp_locator) locators.push(chunk.hwp_locator);
    if (chunk.office_locator) locators.push(chunk.office_locator);
  }
  const labels = [...new Set(locators.map(label).filter((value): value is string => !!value))];
  if (labels.length) return labels.join(' · ');
  const page = chunk.positions?.find((value) => Array.isArray(value) && Number.isFinite(value[0]))?.[0];
  return page ? `p. ${page}` : '문서 위치';
}
