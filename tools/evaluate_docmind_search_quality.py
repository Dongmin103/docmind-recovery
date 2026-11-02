"""Paired runtime retrieval and reranking; private outputs, aggregate stdout.

Synthetic chunks use an isolated index. Live filename cases are read-only.
Calibration and held-out assignments are fixed before model calls.
"""
import asyncio
import faulthandler
import hashlib
import importlib.util
import json
import logging
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

logging.disable(logging.CRITICAL)
sys.path.insert(0, '/candidate/tools')
from evaluate_docmind_lexical import fixtures

faulthandler.cancel_dump_traceback_later()


def audit(**values):
    print('AUDIT ' + json.dumps(values), flush=True)


def experiment():
    docs, cases = fixtures()
    for doc in docs:
        doc['chunk_id'] = doc['id']
    for i, case in enumerate(cases):
        case['split'] = 'calibration' if i % 6 < 3 else 'heldout'
        case['scope'] = 'all'
    negatives = [
        '달 탐사선의 착륙 좌표', '고양이 예방접종 주기', '해양 조류의 염도 측정',
        '바이올린 활의 송진 교체', '화성 대기의 이산화탄소 비율', '초콜릿 케이크 굽는 온도',
        '제주도 해수욕장 폐장 날짜', '삼각함수 덧셈 정리 증명', '목성 위성 공전 주기',
        '양자 얽힘 실험 장비', '봄철 벚나무 가지치기', '수영 배영 출발 규칙',
        '직원 자녀 학자금 지원 금액', '출장 항공권 좌석 등급 기준', '교육 수료증 재발급 수수료',
        '출입 카드 분실 벌금', '장비 구매 입찰 참가 자격', '법인카드 해외 환전 수수료',
        '재고 자산 감가상각 회계 기준', '퇴직 직원의 퇴직금 산정식', '생산직 야간 수당 계산법',
        '배송 차량 보험 가입 방법', '회의실 방음 공사 견적', '도서관 연체료 감면 제도',
    ]
    for i, question in enumerate(negatives):
        cases.append({'id': f'absent-{i:02}', 'question': question, 'gold': [],
                      'category': 'unrelated' if i < 12 else 'adjacent_absent',
                      'split': 'calibration' if i % 2 == 0 else 'heldout', 'scope': 'all'})
    # Multi-chunk documents: repeated passages plus distinct sections and sources.
    for topic, word in enumerate(['냉각장치', '압축장치', '환기장치', '가열장치']):
        doc_id = f'manual-{topic}'
        for section in range(24):
            text = (f'{word} 운전 점검 {section + 1}항. 정기 점검 시 담당자는 상태 기록을 확인합니다.'
                    if section != 20 else f'{word}의 비상 정지 후 재가동은 차단 밸브를 잠그고 잔압을 제거한 뒤 안전 관리자의 승인을 받아야 합니다.')
            docs.append({'id': doc_id, 'chunk_id': f'{doc_id}-{section:02}', 'name': f'{word} 운전매뉴얼.pdf', 'content': text})
        for copy in range(8):
            docs.append({'id': doc_id, 'chunk_id': f'{doc_id}-copy-{copy}', 'name': f'{word} 운전매뉴얼.pdf',
                         'content': f'{word} 유지보수 작업자는 전원을 차단하고 잠금 표지를 부착해야 합니다.'})
        for source in range(2):
            other = f'{doc_id}-support-{source}'
            docs.append({'id': other, 'chunk_id': other, 'name': f'{word} 안전수칙 {source}.pdf',
                         'content': f'{word} 유지보수 시 전원 차단과 잠금 표지가 필수입니다. 작업 전 안전 담당자가 차단 상태를 확인합니다.'})
        cases.extend([
            {'id': f'single-{topic}', 'question': f'{word} 비상 정지 후 재가동 절차', 'gold': [doc_id],
             'gold_chunks': [f'{doc_id}-20'], 'category': 'single_document', 'scope': doc_id,
             'split': 'calibration' if topic < 2 else 'heldout'},
            {'id': f'diverse-{topic}', 'question': f'{word} 유지보수 안전 수칙',
             'gold': [doc_id, f'{doc_id}-support-0', f'{doc_id}-support-1'],
             'category': 'multi_document', 'scope': 'all', 'split': 'calibration' if topic < 2 else 'heldout'},
        ])
    return docs, cases


def confirmation_experiment():
    """Frozen after broad-refill regression, before any confirmation model calls."""
    docs, previous = experiment()
    replacements = dict(zip(['냉각장치', '압축장치', '환기장치', '가열장치'],
                            ['집진설비', '건조설비', '세척설비', '분쇄설비'], strict=True))
    def replace(value):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    for doc in docs:
        doc['content'], doc['name'] = replace(doc['content']), replace(doc['name'])
    cases = [dict(row, id='confirm-' + row['id'], question=replace(row['question']), split='heldout')
             for row in previous if row['category'] in ['single_document', 'multi_document']]
    for i, word in enumerate(replacements.values()):
        cases.append({'id': f'confirm-title-{i}', 'question': word + '운전매뉴얼', 'gold': [f'manual-{i}'],
                      'category': 'title', 'scope': 'all', 'split': 'heldout'})
    negatives = ['목성의 대적점 크기', '반려 토끼의 사료 배합', '장미 접목에 적합한 계절',
                 '첼로 현의 조율 순서', '해저 화산 분출의 원리', '크루아상 반죽 발효 시간',
                 '직원 주택 대출 이자 지원 한도', '공장 구내식당 식권 환불 절차', '출장 여권 발급 비용 지원',
                 '퇴직자 건강검진 대상 연령', '집진설비 부품 보증 수리 기간', '분쇄설비 임대료와 계약금']
    for i, question in enumerate(negatives):
        cases.append({'id': f'confirm-absent-{i:02}', 'question': question, 'gold': [],
                      'category': 'unrelated' if i < 6 else 'adjacent_absent', 'scope': 'all', 'split': 'heldout'})
    return docs, cases


async def main():
    audit(phase='imports')
    import numpy as np
    from common import settings
    settings.init_settings()
    from api.apps.services import docmind_api_service as service
    from api.db.db_models import DocmindProject
    from api.db.joint_services.tenant_model_service import resolve_model_config
    from api.db.services.knowledgebase_service import KnowledgebaseService
    from api.db.services.llm_service import LLMBundle
    from common.constants import LLMType
    from common.doc_store.doc_store_base import MatchDenseExpr, OrderByExpr
    from rag.llm.embedding_model import OpenAIEmbed
    from rag.nlp import query, rag_tokenizer
    from rag.nlp.docmind_lexical import supplement_expression, supplemental_fields
    original_init = OpenAIEmbed.__init__
    def bounded_client(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.client = self.client.with_options(timeout=30, max_retries=0)
    OpenAIEmbed.__init__ = bounded_client
    project = next(iter(DocmindProject.select()))
    catalog = service._load_serving_catalog(project.tenant_id)
    _, kb = KnowledgebaseService.get_by_id(catalog.dataset_id)
    model = LLMBundle(kb.tenant_id, resolve_model_config(kb.tenant_id, LLMType.EMBEDDING, kb.embd_id))
    reranker = service._rerank_model(catalog)
    es = settings.docStoreConn.es
    builder = query.FulltextQueryer()
    audit(phase='models_ready')
    root = Path('/evaluation')
    replay = '--replay' in sys.argv
    confirmation = '--confirm' in sys.argv
    output = root / ('paired-confirmation.private.json' if confirmation else
                     'paired-replay-narrow.private.json' if replay else 'paired-narrow.private.json')
    existing = json.loads(output.read_text()) if output.exists() else {}
    version = service._catalog_version_id(catalog)
    assert not existing or existing['catalog_version'] == version
    rows = existing.get('cases', [])
    completed = {r['id'] for r in rows}
    cache_path = root / ('confirmation-vectors.private.json' if confirmation else 'vectors.private.json')
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    docs, cases = confirmation_experiment() if confirmation else experiment()
    spec = importlib.util.spec_from_file_location('candidate_selection', '/candidate/app/api/apps/services/docmind_search_selection.py')
    selection = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selection)
    manifest = hashlib.sha256(json.dumps([docs, cases], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert not cache or cache['manifest'] == manifest
    cache.setdefault('manifest', manifest)
    cache.setdefault('queries', {})

    def save():
        output.write_text(json.dumps({'catalog_version': version, 'manifest': manifest, 'cases': rows}, ensure_ascii=False), encoding='utf8')
        cache_path.write_text(json.dumps(cache), encoding='utf8')

    async def score(question, pool):
        ordered = sorted(pool, key=lambda c: c.chunk['chunk_id'])
        if not ordered:
            return [], 0, 0
        for attempt in range(3):
            started = time.monotonic()
            try:
                scores, tokens = await asyncio.wait_for(asyncio.to_thread(reranker.mdl._compute_rank, question, [c.rerank_text for c in ordered]), 45)
                break
            except Exception as error:  # noqa: BLE001 - private provider errors must not expose credentials
                status = getattr(getattr(error, 'response', None), 'status_code', None)
                audit(phase='rerank_retry', error_type=type(error).__name__, http_status=status, attempt=attempt + 1)
                if attempt == 2 or (status and 400 <= status < 500 and status not in (408, 429)):
                    raise RuntimeError('RERANK_EVALUATION_FAILED') from None
                await asyncio.sleep(10 * (attempt + 1))
        elapsed = time.monotonic() - started
        normalized = reranker.mdl._normalize_rank(np.asarray(scores, dtype=float))
        ranked = sorted(zip(ordered, map(float, scores), map(float, normalized)), key=lambda x: (-x[1], -x[0].rrf_score, x[0].chunk['chunk_id']))
        return [{'chunk': c.chunk, 'score': s, 'normalized_score': n, 'rrf_score': c.rrf_score} for c, s, n in ranked], elapsed, tokens

    async def compare(case, bm, dn, recall_seconds):
        before = service._rrf_candidates(bm, dn)
        document_count = (1 if case['scope'] != 'all' else
                          len(catalog.document_names) if case['category'] == 'live_title' else len({d['id'] for d in docs}))
        after = service._rrf_candidates(bm, dn, refill=selection.refill_candidates(document_count))
        baseline, baseline_s, baseline_tokens = await score(case['question'], before)
        # Exactly equal pools share a model response, isolating selection changes.
        same = [c.chunk['chunk_id'] for c in before] == [c.chunk['chunk_id'] for c in after]
        candidate, candidate_s, candidate_tokens = (baseline, baseline_s, baseline_tokens) if same else await score(case['question'], after)
        baseline = sorted(baseline, key=lambda x: (-x['normalized_score'], -x['rrf_score'], x['chunk']['chunk_id']))
        rows.append({**case, 'baseline': baseline, 'candidate': candidate,
                     'baseline_candidates': len(before), 'candidate_candidates': len(after),
                     'shared_rerank': same, 'recall_seconds': recall_seconds,
                     'baseline_rerank_seconds': baseline_s, 'candidate_rerank_seconds': candidate_s,
                     'baseline_tokens': baseline_tokens, 'candidate_tokens': candidate_tokens})
        completed.add(case['id']); save()
        audit(progress=len(rows), case=case['id'], before=len(before), after=len(after))

    index = 'docmind_quality_eval_' + uuid4().hex
    try:
        if replay:
            for case in cases:
                if case['id'] in completed:
                    continue
                pool = [service.Candidate('fixture', {'chunk_id': d['chunk_id'], 'doc_id': d['id'],
                        'docnm_kwd': d['name'], 'content_with_weight': d['content']}, service._bounded_text(d['content']))
                        for d in docs if case['scope'] == 'all' or d['id'] == case['scope']]
                # Two fixed candidate lanes isolate candidate selection from recall.
                await compare(case, pool[:128], pool[128:] + pool[:128], 0)
        elif any(c['id'] not in completed for c in cases):
            mapping = json.loads(Path('/ragflow/conf/mapping.json').read_text())
            mapping['mappings']['properties']['q_768_vec'] = {'type': 'dense_vector', 'dims': 768, 'index': True, 'similarity': 'cosine'}
            es.indices.create(index=index, **mapping)
            if 'vectors' not in cache:
                audit(phase='embedding')
                titles, _ = await asyncio.to_thread(model.encode, [d['name'] for d in docs])
                bodies, _ = await asyncio.to_thread(model.encode, [d['content'] for d in docs])
                cache['vectors'] = (0.1 * np.asarray(titles) + 0.9 * np.asarray(bodies)).tolist()
                save()
            ops = []
            for doc, vector in zip(docs, cache['vectors'], strict=True):
                body_tks = rag_tokenizer.tokenize(doc['content'])
                title_tks = rag_tokenizer.tokenize(re.sub(r'\.[^.]+$', '', doc['name']))
                fields = {'kb_id': 'fixture', 'doc_id': doc['id'], 'docnm_kwd': doc['name'], 'content_with_weight': doc['content'],
                          'content_ltks': body_tks, 'content_sm_ltks': rag_tokenizer.fine_grained_tokenize(body_tks),
                          'title_tks': title_tks, 'title_sm_tks': rag_tokenizer.fine_grained_tokenize(title_tks),
                          'q_768_vec': vector, **supplemental_fields(doc['name'], doc['content'])}
                ops.extend([{'index': {'_index': index, '_id': doc['chunk_id']}}, fields])
            assert not es.bulk(operations=ops, refresh='wait_for')['errors']
            for case in cases:
                if case['id'] in completed:
                    continue
                started = time.monotonic()
                if case['id'] not in cache['queries']:
                    vector, _ = await asyncio.to_thread(model.encode_queries, case['question'])
                    cache['queries'][case['id']] = list(map(float, vector))
                expression, _ = builder.question(case['question'], min_match=0.3)
                expression = supplement_expression(expression, case['question'])
                condition = {} if case['scope'] == 'all' else {'doc_id': [case['scope']]}
                fields = ['doc_id', 'docnm_kwd', 'content_with_weight', 'kb_id']
                def lane(expressions, fields=fields, condition=condition):
                    response = settings.docStoreConn.search(fields, [], condition, expressions, OrderByExpr(), 0, 128, index, ['fixture'])
                    return [service.Candidate('fixture', {**h['_source'], 'chunk_id': h['_id']}, service._bounded_text(h['_source']['content_with_weight'])) for h in response['hits']['hits']]
                bm = lane([expression] if expression else [])
                dn = lane([MatchDenseExpr('q_768_vec', cache['queries'][case['id']], 'float', 'cosine', 2048, {'similarity': 0.0, 'num_candidates': 4096})])
                await compare(case, bm, dn, time.monotonic() - started)
    finally:
        assert re.fullmatch(r'docmind_quality_eval_[0-9a-f]{32}', index)
        es.indices.delete(index=index, ignore_unavailable=True)
    if replay or confirmation:
        audit(phase='complete', cases=len(rows), index_cleaned=True)
        return
    stems = {d: re.sub(r'\.[^.]+$', '', name) for d, name in (catalog.document_names or {}).items()}
    unique = sorted(set(stems.values()), key=lambda s: hashlib.sha256(s.encode()).hexdigest())[:12]
    for i, stem in enumerate(unique):
        case_id = f'live-title-{i:02}'
        if case_id in completed:
            continue
        question = re.sub(r'(?<=[가-힣])\s+(?=[가-힣])', '', stem) if i % 2 else stem
        case = {'id': case_id, 'category': 'live_title', 'question': question,
                'gold': [d for d, s in stems.items() if s == stem], 'scope': 'all', 'split': 'heldout'}
        scope = service._resolve_scope(catalog, {'mode': 'all'})
        started = time.monotonic()
        bm, dn = await asyncio.gather(*(service._lane_candidates(project.tenant_id, question, catalog, scope, candidate_mode=mode) for mode in ['bm25', 'dense']))
        await compare(case, bm, dn, time.monotonic() - started)
    assert service._catalog_version_id(service._load_serving_catalog(project.tenant_id)) == version
    audit(phase='complete', cases=len(rows), index_cleaned=True)


if __name__ == '__main__':
    asyncio.run(main())
    if '--and-confirm' in sys.argv:
        sys.argv.append('--confirm')
        asyncio.run(main())
