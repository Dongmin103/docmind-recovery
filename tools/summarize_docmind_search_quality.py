"""Reduce private paired results to aggregates; never emit queries or document text."""

import argparse
import hashlib
import importlib.util
import json
import statistics
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    module_path = Path(__file__).resolve().parents[1] / "app/api/apps/services/docmind_search_selection.py"
    spec = importlib.util.spec_from_file_location("selection", module_path)
    selection = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selection)
    data = json.loads(args.input.read_text(encoding="utf8"))
    cases = []
    for row in data["cases"]:
        scored = [(SimpleNamespace(chunk=item["chunk"], rerank_text=item["chunk"]["content_with_weight"][:2400]), item["score"])
                  for item in row["candidate"]]
        groups, relevance = selection.select_document_groups(scored, row["question"])
        baseline = [item["chunk"] for item in row["baseline"][:5]]
        candidate = [group[0][0].chunk for group in groups]
        evidence = [item[0].chunk for group in groups for item in group]
        gold = set(row["gold"])
        metrics = {}
        for mode, chunks in [("baseline", baseline), ("candidate", candidate)]:
            ids = [chunk["doc_id"] for chunk in chunks]
            metrics[mode] = {
                "returned": bool(chunks), "documents": len(set(ids)),
                "duplicate_document_slots": len(ids) - len(set(ids)),
                "hit": bool(gold.intersection(ids)) if gold else None,
                "recall": len(gold.intersection(ids)) / len(gold) if gold else None,
                "mrr": next((1 / rank for rank, doc in enumerate(ids, 1) if doc in gold), 0) if gold else None,
                "gold_chunk_hit": bool(set(row["gold_chunks"]) & {c["chunk_id"] for c in (baseline if mode == "baseline" else evidence)}) if "gold_chunks" in row else None,
            }
        cases.append({"id": row["id"], "split": row["split"], "category": row["category"],
                      "relevance": relevance["status"], **metrics,
                      **{key: row[key] for key in ["baseline_candidates", "candidate_candidates", "baseline_rerank_seconds", "candidate_rerank_seconds", "baseline_tokens", "candidate_tokens"]}})
    aggregates = []
    for split in ["calibration", "heldout"]:
        for category in ["all", *sorted({r["category"] for r in cases})]:
            selected = [r for r in cases if r["split"] == split and (category == "all" or r["category"] == category)]
            if not selected:
                continue
            modes = {}
            for mode in ["baseline", "candidate"]:
                metrics = {}
                for key in ["returned", "documents", "duplicate_document_slots", "hit", "recall", "mrr", "gold_chunk_hit"]:
                    values = [r[mode][key] for r in selected if r[mode][key] is not None]
                    metrics[key] = {"n": len(values), "mean": statistics.mean(values)} if values else None
                for key in ["candidates", "rerank_seconds", "tokens"]:
                    values = [r[f"{mode}_{key}"] for r in selected]
                    metrics[key] = {"median": statistics.median(values), "max": max(values)}
                modes[mode] = metrics
            aggregates.append({"split": split, "category": category, "n": len(selected), **modes})
    result = {"minimum": selection.MIN_RELEVANCE, "low": selection.LOW_RELEVANCE,
              "fixture_manifest": data["manifest"],
              "selection_sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
              "cases": cases, "aggregates": aggregates}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8", newline="\n")
    print(json.dumps({"cases": len(cases), "regressions": [r["id"] for r in cases if r["baseline"]["hit"] and not r["candidate"]["hit"]],
                      "negative_returns": {mode: sum(r[mode]["returned"] for r in cases if r[mode]["hit"] is None) for mode in ["baseline", "candidate"]},
                      "heldout_positive_hits": {mode: sum(r[mode]["hit"] for r in cases if r["split"] == "heldout" and r[mode]["hit"] is not None) for mode in ["baseline", "candidate"]}}))


if __name__ == "__main__":
    main()
