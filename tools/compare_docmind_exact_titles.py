"""Replay identical saved candidates against a baseline selector; emit no document data."""

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def load(path):
    spec = importlib.util.spec_from_file_location("selection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args()
    old = load(args.baseline)
    new = load(Path(__file__).resolve().parents[1] / "app/api/apps/services/docmind_search_selection.py")
    report = {"ordinary": 0, "ordinary_identical": 0, "exact": 0, "exact_first": 0,
              "exact_old_first": 0, "live_titles": 0, "live_gold_first_before": 0, "live_gold_first_after": 0}
    for path in args.inputs:
        for row in json.loads(path.read_text(encoding="utf8"))["cases"]:
            scored = [(SimpleNamespace(chunk=x["chunk"], rerank_text=x["chunk"]["content_with_weight"][:2400]), x["score"])
                      for x in row["candidate"]]
            query = row["question"]
            exact = {c.chunk["doc_id"] for c, _ in scored
                     if new.exact_title_match(query, c.chunk.get("docnm_kwd") or c.chunk.get("doc_name") or "")}
            before, after = (m.select_document_groups(scored, query) for m in (old, new))
            before_ids, after_ids = ([g[0][0].chunk["doc_id"] for g in result[0]] for result in (before, after))
            if not exact:
                report["ordinary"] += 1
                report["ordinary_identical"] += before == after
                assert before == after, "Non-title result, evidence, score or relevance changed"
            else:
                report["exact"] += 1
                report["exact_old_first"] += bool(before_ids and before_ids[0] in exact)
                report["exact_first"] += bool(after_ids and after_ids[0] in exact)
                assert after_ids and after_ids[0] in exact
                other_before = [doc for doc in before_ids if doc not in exact]
                other_after = [doc for doc in after_ids if doc not in exact]
                assert other_after == other_before[:len(other_after)]
            if row["category"] == "live_title":
                report["live_titles"] += 1
                for ids, key in [(before_ids, "live_gold_first_before"), (after_ids, "live_gold_first_after")]:
                    report[key] += bool(ids and ids[0] in row["gold"])
            if row["id"] == "live-title-11":
                report["requested_case_ranks"] = [next(i for i, doc in enumerate(ids, 1) if doc in row["gold"])
                                                  for ids in (before_ids, after_ids)]
    assert report["requested_case_ranks"] == [10, 1]
    print(json.dumps(report))


if __name__ == "__main__":
    main()
