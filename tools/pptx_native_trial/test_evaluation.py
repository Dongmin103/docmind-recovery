from tools.pptx_native_trial.evaluation import score


def test_empty_truth_and_prediction_score_as_complete():
    assert score([], [])["f1"] == 1


def test_missing_and_extra_data_reduce_recall_and_precision():
    assert score([], ["required"])["recall"] == 0
    assert score(["extra"], [])["precision"] == 0
    assert score(["correct", "extra"], ["correct"])["f1"] == 2 / 3


def test_fixture_metrics_detect_value_corruption_and_duplicate_text():
    from dataclasses import replace
    from tools.pptx_native_trial.benchmark import fixture_accuracy
    from tools.pptx_native_trial.extractor import extract
    from tools.pptx_native_trial.fixtures import synthetic_deck

    result = extract(synthetic_deck())
    assert all(m["f1"] == 1 for m in fixture_accuracy(result, 3).values())
    blocks = list(result.blocks)
    chart_index = next(i for i, b in enumerate(blocks) if b.kind == "chart")
    blocks[chart_index] = replace(
        blocks[chart_index], chart_values=(("매출", "Q1", "999"),)
    )
    blocks.append(blocks[0])
    metrics = fixture_accuracy(replace(result, blocks=tuple(blocks)), 3)
    assert metrics["chart_tuple_exact"]["f1"] < 1
    assert metrics["text_block_exact"]["precision"] < 1


def test_missing_whole_slide_cannot_shrink_the_ground_truth():
    from tools.pptx_native_trial.benchmark import fixture_accuracy
    from tools.pptx_native_trial.extractor import extract
    from tools.pptx_native_trial.fixtures import synthetic_deck

    result = extract(synthetic_deck(1))
    assert (
        fixture_accuracy(result, 3, expected_slides=2)["text_block_exact"]["recall"]
        == 0.5
    )
