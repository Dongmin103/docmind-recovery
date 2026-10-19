"""Compare output limits on identical saved Jina scores; emit aggregates only."""

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('inputs', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'app/api/apps/services/docmind_search_selection.py'
    spec = importlib.util.spec_from_file_location('selection', source)
    selection = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selection)
    totals = {limit: {'documents': 0, 'positive_hits': 0, 'gold_documents': 0,
                      'negative_returns': 0} for limit in (5, 32)}
    counts = {'cases': 0, 'positive_cases': 0, 'negative_cases': 0,
              'expanded_cases': 0, 'previous_top_five_changed': 0,
              'relevance_status_changed': 0, 'max_documents': 0}
    transitions = {}
    for path in args.inputs:
        for row in json.loads(path.read_text(encoding='utf8'))['cases']:
            scored = [(SimpleNamespace(chunk=item['chunk'],
                                       rerank_text=item['chunk']['content_with_weight'][:2400]), item['score'])
                      for item in row['candidate']]
            gold = set(row['gold'])
            results = {}
            for limit in (5, 32):
                selection.DOCUMENT_LIMIT = limit
                groups, relevance = selection.select_document_groups(scored, row['question'])
                ids = [group[0][0].chunk['doc_id'] for group in groups]
                results[limit] = (ids, relevance)
                totals[limit]['documents'] += len(ids)
                totals[limit]['positive_hits'] += bool(gold & set(ids))
                totals[limit]['gold_documents'] += len(gold & set(ids))
                totals[limit]['negative_returns'] += not gold and bool(ids)
            before, after = results[5][0], results[32][0]
            counts['cases'] += 1
            counts['positive_cases' if gold else 'negative_cases'] += 1
            counts['expanded_cases'] += len(after) > len(before)
            counts['previous_top_five_changed'] += before != after[:5]
            counts['relevance_status_changed'] += results[5][1]['status'] != results[32][1]['status']
            if results[5][1]['status'] != results[32][1]['status']:
                transition = results[5][1]['status'] + ' -> ' + results[32][1]['status']
                transitions[transition] = transitions.get(transition, 0) + 1
            counts['max_documents'] = max(counts['max_documents'], len(after))
    report = {'comparison': 'Same candidates and raw rerank scores; document limit 5 versus 32',
              'limitations': 'Additional returned documents are not exhaustively relevance-labeled; '
                             'this replay does not measure live latency or full-corpus precision.',
              **counts, 'status_transitions': transitions, 'limits': totals}
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf8', newline='\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
