"""Aggregate-only accuracy metrics for offline trials."""

from collections import Counter


def score(predicted, expected):
    """Multiset F1: additional copies are false positives, not silently deduplicated."""
    found, truth = Counter(predicted), Counter(expected)
    matched = sum((found & truth).values())
    predicted_count, expected_count = sum(found.values()), sum(truth.values())
    precision = (
        matched / predicted_count if predicted_count else float(not expected_count)
    )
    recall = matched / expected_count if expected_count else float(not predicted_count)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "matched": matched,
        "predicted": predicted_count,
        "expected": expected_count,
    }
