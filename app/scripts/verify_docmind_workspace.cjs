/* UI-only browser verification. All API responses are synthetic; no backend writes. */
const { createRequire } = require('node:module');
const { resolve } = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');

const runtimeRequire = process.env.DOCMIND_BROWSER_NODE_MODULES
  ? createRequire(resolve(process.env.DOCMIND_BROWSER_NODE_MODULES, '../package.json'))
  : require;
const { chromium } = runtimeRequire('playwright');
const base = process.env.DOCMIND_UI_URL || 'http://127.0.0.1:5173';
const output = resolve(process.env.DOCMIND_UI_EVIDENCE_DIR || 'runtime/ui-evidence/docmind-unified-ui');
const datasetId = 'a'.repeat(32);
const documentId = 'b'.repeat(32);
const folders = [
  { id: 'validation', name: 'Validation', document_count: 2, depth: 0 },
  { id: 'risk', name: 'Quality Risk Management', document_count: 1, depth: 0 },
];
const chunks = Array.from({ length: 25 }, (_, i) => ({
  chunk_id: `sample-chunk-${i + 1}`, doc_id: documentId,
  docnm_kwd: '밸리데이션 안내.txt', available_int: 1,
  content_with_weight: i === 1
    ? '<table><thead><tr><th>평가 항목</th><th>확인하는 내용</th></tr></thead><tbody><tr><td>정확성</td><td>참값과 측정값의 일치 정도</td></tr><tr><td>정밀성</td><td>반복 측정 결과 사이의 일치 정도</td></tr></tbody></table>'
    : `시험방법 밸리데이션은 분석법이 의도한 목적에 적합한지 확인하는 과정입니다. 정확성, 정밀성, 특이성과 검량선의 범위를 문서에 기록합니다. 화면 확인용 예시 근거 ${i + 1}.`,
  positions: [], similarity: 0.95 - i / 100,
}));
const doc = { id: documentId, name: '밸리데이션 안내.txt', kb_id: datasetId, type: 'txt', size: 2048, create_date: '2026-09-09 00:00:00', chunk_num: 25, progress: 1, run: '3' };
const registrations = [
  { registration_id: 'reg-1', document_id: documentId, document_name: doc.name, document_exists: true, folder_id: 'validation', state: 'INDEXED', progress: 1, chunk_count: 25, index_ready: true, draft_eligible: true, active_catalog_member: true, retry_allowed: false, is_current: true },
  { registration_id: 'reg-2', document_id: 'c'.repeat(32), document_name: '시험방법 보완자료.hwpx', document_exists: true, folder_id: 'validation', state: 'INDEXING', progress: 0.45, chunk_count: 0, index_ready: false, draft_eligible: false, active_catalog_member: false, retry_allowed: false, is_current: true },
  { registration_id: 'reg-3', document_id: 'd'.repeat(32), document_name: '품질위험관리 안내.pdf', document_exists: true, folder_id: 'risk', state: 'FAILED', progress: 0.2, chunk_count: 0, index_ready: false, draft_eligible: false, active_catalog_member: false, retry_allowed: true, is_current: true, error_message: '예시: 문서 처리 중 연결이 끊겼습니다.' },
];
const draft = {
  draft_id: 'draft-sample', version_label: 'DRAFT-EXAMPLE', parent_version_id: 'active-sample', lifecycle_state: 'READY', health_state: 'VALID',
  snapshot_hash: 'e'.repeat(64), active_parent_is_current: true, change_count: 1, has_effective_changes: true,
  generation_progress: 100, digest_ready_count: 3, membership_count: 3, card_ready_count: 2,
  can_generate: false, readiness_mode: 'ADMIN_SAVED', search_validation_performed: false,
  folders: folders.map((f, ordinal) => ({ ...f, ordinal })),
  changes: [{ operation: 'ADD', document_id: documentId, document_name: doc.name, to_folder_id: 'validation', ordinal: 0 }],
  routing_cards: folders.map((f) => ({ folder_id: f.id, folder_name: f.name, l0: `${f.name}: 시험방법과 품질 문서를 찾는 폴더입니다.`, l1: '정확성·정밀성·특이성의 정의와 시험방법을 확인할 때 선택합니다. 문서별 원문과 청크를 근거로 내용을 확인하세요.' })),
};

async function run() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  const writes = [];
  let searches = 0;
  let owner = true;
  let inspectionUnavailable = false;
  page.on('pageerror', (error) => errors.push(error.message));
  await context.addInitScript(() => {
    localStorage.setItem('lng', 'ko');
    localStorage.setItem('ragflow-ui-theme', 'dark');
  });
  await context.route(/\/(?:api\/v1|v1)\//, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const ok = (data) => route.fulfill({ json: { code: 0, data } });
    if (path === '/api/v1/language') return ok({ language: 'python' });
    if (path === '/api/v1/docmind/folders') return ok({ dataset_id: datasetId, catalog_source: 'database', catalog_version_id: 'active-sample', folders, can_administer: owner });
    if (path === '/api/v1/docmind/search') {
      searches++;
      return ok({ dataset_id: datasetId, chunks: chunks.slice(0, 10), ranked_chunks: chunks, ranked_total: 25, total: 25, candidate_count: 25, selected_folders: [{ id: 'validation' }], catalog_source: 'database', catalog_version_id: 'active-sample', scope_mode: 'automatic', effective_folder_ids: ['validation'], caps: { folders: 5, candidates: 64, per_folder: 32, per_document: 8, results: 10 } });
    }
    if (request.method() !== 'GET') {
      writes.push({ path, method: request.method() });
      return route.fulfill({ status: 403, json: { code: 403, message: 'UI fixture: mutation disabled' } });
    }
    if (path.endsWith('/docmind/admin/registrations')) return ok({ project_id: 'project-sample', dataset_id: datasetId, catalog_version_id: 'active-sample', registrations });
    if (path.endsWith('/docmind/admin/hierarchy')) return ok({ project_id: 'project-sample', dataset_id: datasetId, source_root_file_id: 'root', nodes: [
      { file_id: 'root', type: 'folder', name: '문서', relative_path: '', depth: 0 },
      { file_id: 'folder-validation', parent_file_id: 'root', type: 'folder', name: 'Validation', relative_path: 'Validation', depth: 1, semantic_folder_id: 'validation' },
      { file_id: 'file-sample', parent_file_id: 'folder-validation', type: 'file', name: doc.name, relative_path: `Validation/${doc.name}`, depth: 2, document_id: documentId, document_exists: true, index_state: 'INDEXED' },
    ] });
    if (path.endsWith('/docmind/admin/hierarchy/imports')) return ok({ jobs: [] });
    if (path.endsWith('/docmind/admin/catalog/drafts')) return ok({ project_id: 'project-sample', active_version_id: 'active-sample', drafts: [draft] });
    if (path.endsWith('/docmind/admin/catalog/drafts/draft-sample')) return ok(draft);
    if (path.endsWith('/docmind/admin/catalog/versions')) return ok({ project_id: 'project-sample', active_version_id: 'active-sample', catalog_source_mode: 'database', versions: [{ version_id: 'active-sample', version_label: 'V1', lifecycle_state: 'PUBLISHED', health_state: 'VALID', active: true, membership_count: 2, rollback_allowed: false }, { version_id: draft.draft_id, version_label: draft.version_label, lifecycle_state: 'READY', health_state: 'VALID', active: false, membership_count: 3, publish_allowed: true, parent_version_id: 'active-sample', validation_report_hash: 'f'.repeat(64), readiness_mode: 'ADMIN_SAVED' }] });
    if (path.endsWith(`/documents/${documentId}/chunks`)) return inspectionUnavailable
      ? route.fulfill({ json: { code: 102, message: '예시: 문서에 접근할 수 없습니다.' } })
      : ok({ chunks, total: chunks.length, doc });
    if (path.endsWith(`/documents/${documentId}/preview`)) return route.fulfill({ contentType: 'text/plain; charset=utf-8', body: '화면 검증용 예시 문서\n\n시험방법 밸리데이션\n\n정확성은 참값과 측정값의 일치 정도입니다. 정밀성은 반복 측정 결과 사이의 일치 정도를 확인합니다.\n\n실제 문서 파싱이나 인덱싱을 실행하지 않은 UI 검증입니다.' });
    if (path.includes('user/info') || path.endsWith('/users/me')) return ok({ id: 'sample-user', nickname: '화면 검증', language: 'ko', email: 'preview@example.invalid', time_zone: 'Asia/Seoul' });
    if (path.includes('tenant/list') || path.includes('llm') || path.includes('providers') || path.includes('models')) return ok([]);
    return ok({});
  });
  const screenshot = async (name) => {
    await page.evaluate(() => {
      if (document.getElementById('fixture-label')) return;
      const label = document.createElement('div'); label.id = 'fixture-label';
      label.textContent = 'UI 검증용 예시 데이터 · 실제 인덱싱/Publish 없음';
      Object.assign(label.style, { position: 'fixed', bottom: '8px', right: '12px', zIndex: '9999', padding: '6px 10px', background: '#151b27', color: '#fff', border: '1px solid #64748b', borderRadius: '6px', fontSize: '12px', pointerEvents: 'none' });
      document.body.appendChild(label);
    });
    await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true });
  };
  try {
    await page.goto(`${base}/docmind`, { waitUntil: 'networkidle' });
    const input = page.getByPlaceholder('문서에서 찾고 싶은 내용을 입력하세요');
    await input.fill('시험방법 밸리데이션의 정확성과 정밀성은 어떻게 다른가?');
    await input.press('Enter');
    await page.getByText('검색 결과 25개 중 10개 표시').waitFor();
    await page.getByRole('button', { name: '결과 10개 더 보기' }).click();
    await screenshot('search-desktop');
    await page.getByRole('link', { name: '원문 · 청크 확인' }).first().click();
    await page.getByText('보기 전용').waitFor();
    await page.getByRole('cell', { name: '정확성', exact: true }).waitFor();
    await screenshot('document-desktop');
    assert.equal(await page.getByRole('button', { name: /청크 추가|삭제/ }).count(), 0);
    await page.getByRole('button', { name: '뒤로', exact: true }).click();
    await page.goBack();
    assert.notEqual(new URL(page.url()).searchParams.get('view'), 'document', 'in-app back must not reopen inspection on browser Back');
    await page.getByText('검색 결과 25개 중 20개 표시').waitFor();
    assert.equal(await input.inputValue(), '시험방법 밸리데이션의 정확성과 정밀성은 어떻게 다른가?');
    await page.getByRole('link', { name: '설정', exact: true }).click();
    await page.getByText('preview@example.invalid', { exact: true }).waitFor();
    await page.getByRole('button', { name: '모델 · API 연결', exact: true }).click();
    await page.getByTestId('sidebar-default-models').waitFor();
    await screenshot('settings-desktop');
    await page.getByRole('link', { name: '자료 관리', exact: true }).click();
    await page.getByRole('heading', { name: '현재 작업', exact: true }).waitFor();
    await screenshot('library-desktop');
    await page.getByRole('button', { name: /^등록 기록 \d+건$/ }).click();
    await page.getByRole('region', { name: '문서 처리 현황' }).getByText(doc.name, { exact: true }).waitFor();
    await page.getByRole('button', { name: '등록 기록 닫기', exact: true }).click();
    await page.getByRole('link', { name: 'Catalog', exact: true }).click();
    await page.getByRole('button', { name: 'Publish 검토', exact: true }).click();
    await page.getByText(draft.routing_cards[0].l0, { exact: false }).first().waitFor();
    await screenshot('catalog-desktop');
    await page.getByRole('link', { name: '검색', exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await screenshot('search-mobile');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'mobile horizontal overflow');
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => document.documentElement.classList.remove('dark'));
    await page.waitForFunction(() => {
      const field = document.querySelector('#docmind-search input');
      const surface = document.querySelector('main.docmind');
      return field && surface && getComputedStyle(field).color === getComputedStyle(surface).color;
    });
    await screenshot('search-light');
    assert.equal(searches, 1, 'navigation must not re-run search');
    assert.deepEqual(writes, [], 'navigation must not mutate backend');
    inspectionUnavailable = true;
    await page.goto(`${base}/docmind?view=document&id=${datasetId}&doc_id=${documentId}&source=docmind`, { waitUntil: 'networkidle' });
    await page.getByText('문서를 불러오지 못했습니다.', { exact: true }).waitFor();
    inspectionUnavailable = false;
    await page.getByRole('button', { name: '다시 불러오기', exact: true }).click();
    await page.getByRole('cell', { name: '정확성', exact: true }).waitFor();
    owner = false;
    await page.goto(`${base}/docmind?view=catalog`, { waitUntil: 'networkidle' });
    await page.getByText('이 화면은 관리자만 사용할 수 있습니다.').waitFor();
    assert.equal(await page.getByRole('link', { name: 'Catalog', exact: true }).count(), 0);
    assert.deepEqual(errors, []);
    fs.writeFileSync(resolve(output, 'verification.json'), JSON.stringify({ mode: 'synthetic-browser-fixtures', actual_backend_tested: false, searches, writes, errors, result: 'PASS' }, null, 2));
    console.log(JSON.stringify({ result: 'PASS', output, searches, writes, errors }));
  } catch (error) {
    await screenshot('failure');
    console.error(JSON.stringify({ result: 'FAIL', error: error.message, errors, url: page.url(), output }));
    process.exitCode = 1;
  } finally { await browser.close(); }
}
run().catch((error) => { console.error(error); process.exitCode = 1; });
