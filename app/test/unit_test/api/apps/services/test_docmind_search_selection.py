import importlib.util
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[5] / 'api/apps/services/docmind_search_selection.py'
spec = importlib.util.spec_from_file_location('search_selection', SOURCE)
selection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selection)


def candidate(doc, text, score, name='guide.pdf', chunk_id=None):
    return SimpleNamespace(chunk={'doc_id': doc, 'chunk_id': chunk_id or text,
                                  'docnm_kwd': name, 'content_with_weight': text}, rerank_text=text[:2400]), score


def test_document_grouping_collapses_duplicates_and_preserves_distinct_numbers(monkeypatch):
    monkeypatch.setattr(selection, 'MIN_RELEVANCE', 0.1)
    scored = [candidate('a', 'port 8123', .9), candidate('a', 'port   8123', .89),
              candidate('a', 'port 9456', .88), candidate('b', 'port 8123', .85)]
    groups, _ = selection.select_document_groups(scored, 'port')
    assert [[c.chunk['content_with_weight'] for c, _ in g] for g in groups] == [['port 8123', 'port 9456'], ['port 8123']]


def test_relevance_filter_does_not_fill_limit_with_irrelevant_docs(monkeypatch):
    monkeypatch.setattr(selection, 'MIN_RELEVANCE', .2)
    monkeypatch.setattr(selection, 'LOW_RELEVANCE', .4)
    groups, info = selection.select_document_groups([candidate('a', 'body', .3), candidate('b', 'other', .1)], 'query')
    assert len(groups) == 1
    assert info == {'status': 'low', 'filtered_chunks': 1}
    groups, info = selection.select_document_groups([candidate('b', 'other', .1)], 'query')
    assert groups == [] and info['status'] == 'no_match'


def test_exact_filename_lookup_survives_body_only_low_score(monkeypatch):
    monkeypatch.setattr(selection, 'MIN_RELEVANCE', .2)
    groups, info = selection.select_document_groups([candidate('a', 'generic body', .01, '설치 방법.pdf')], '설치방법')
    assert len(groups) == 1 and info['status'] == 'matched'
    assert not selection.exact_title_match('C++', 'C#.pdf')
    assert not selection.exact_title_match('설치', '설치 방법.pdf')
    assert not selection.exact_title_match('설치 방법 가격', '설치 방법.pdf')


def test_thirty_two_documents_and_five_distinct_evidence_each(monkeypatch):
    monkeypatch.setattr(selection, 'MIN_RELEVANCE', 0)
    scored = [candidate(str(d), f'evidence {i}', 1 - d * .01 - i * .0001)
              for d in range(40) for i in range(12)]
    groups, _ = selection.select_document_groups(scored, 'query')
    assert len(groups) == 32 and all(len(g) == 5 for g in groups)
    assert [g[0][0].chunk['doc_id'] for g in groups] == [str(d) for d in range(32)]


def test_exact_titles_precede_cap_and_keep_ties_and_evidence_in_score_order():
    scored = [candidate(str(i), str(i), .99 - i * .001, f'other {i}.pdf') for i in range(33)]
    scored += [candidate('b', 'best', .20, '설 치.방법.pdf'), candidate('a', 'first', .15, '설치.방법.docx'),
               candidate('b', 'second', .10, '설 치.방법.pdf')]
    groups, relevance = selection.select_document_groups(scored, '설치.방 법.PDF')
    assert [g[0][0].chunk['doc_id'] for g in groups] == ['b', 'a', *map(str, range(30))]
    assert [c.chunk['chunk_id'] for c, _ in groups[0]] == ['best', 'second']
    assert groups[0][0][1] == .20 and relevance['status'] == 'matched'
    for question in ['설치', '설치.방법 알려줘', '자료에 없는 질문']:
        ordinary, _ = selection.select_document_groups(scored, question)
        assert [g[0][0].chunk['doc_id'] for g in ordinary] == list(map(str, range(32)))
    assert selection.exact_title_match(' Q ', 'Q.pdf')
    assert not selection.exact_title_match(' .pdf ', '.pdf')


def test_authoritative_catalog_name_takes_precedence_over_chunk_label():
    scored = [candidate('old', 'old', .9, 'Exact.pdf'), candidate('current', 'new', .1, 'stale.pdf')]
    groups, _ = selection.select_document_groups(scored, 'exact', document_names={'old':'renamed.pdf','current':'Exact.pdf'})
    assert [g[0][0].chunk['doc_id'] for g in groups] == ['current', 'old']


def test_dedup_uses_full_content_not_only_identical_prefix(monkeypatch):
    monkeypatch.setattr(selection, 'MIN_RELEVANCE', 0)
    prefix = 'x' * 2400
    groups, _ = selection.select_document_groups([candidate('a', prefix + '1', .9), candidate('a', prefix + '2', .8)], 'query')
    assert len(groups[0]) == 2


def test_refill_is_scope_bound_and_preserves_answer_budget():
    assert selection.refill_candidates(1)
    assert selection.refill_candidates(5)
    assert not selection.refill_candidates(6)
    assert not selection.refill_candidates(242)
    assert selection.refill_candidates(242, answer_mode=True)
