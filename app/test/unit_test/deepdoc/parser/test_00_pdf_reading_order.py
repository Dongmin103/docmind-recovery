#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Regression tests for DeepDoc multi-column reading order."""

from deepdoc.parser.pdf_parser import RAGFlowPdfParser


def _box(text, *, top, x0, x1, col_id=0, page_number=1, bottom=None):
    return {
        "text": text,
        "top": float(top),
        "bottom": float(bottom if bottom is not None else top + 10),
        "x0": float(x0),
        "x1": float(x1),
        "col_id": col_id,
        "page_number": page_number,
        "layout_type": "text",
    }


def _ordered_texts(boxes):
    parser = object.__new__(RAGFlowPdfParser)
    parser.boxes = boxes
    parser._final_reading_order_merge()
    return [box["text"] for box in parser.boxes]


def _assigned_columns(boxes):
    parser = object.__new__(RAGFlowPdfParser)
    assigned = parser._assign_column(boxes)
    return [box["col_id"] for box in assigned]


def test_does_not_treat_single_column_indentation_as_multiple_columns():
    boxes = [
        _box(f"body-{index}", top=index * 20, x0=0, x1=100)
        for index in range(6)
    ] + [
        _box("indent-1", top=130, x0=12, x1=100),
        _box("indent-2", top=150, x0=12, x1=100),
        _box("deep-indent-1", top=170, x0=24, x1=100),
        _box("deep-indent-2", top=190, x0=24, x1=100),
    ]
    for box in boxes:
        box.pop("col_id")

    assert set(_assigned_columns(boxes)) == {0}


def test_assigns_columns_only_when_text_spans_have_a_real_gutter():
    boxes = []
    for index in range(4):
        boxes.append(_box(f"left-{index}", top=index * 20, x0=0, x1=40))
        boxes.append(_box(f"right-{index}", top=index * 20, x0=60, x1=100))
    for box in boxes:
        box.pop("col_id")

    assigned = _assigned_columns(boxes)

    assert {assigned[index] for index in range(0, len(assigned), 2)} == {0}
    assert {assigned[index] for index in range(1, len(assigned), 2)} == {1}


def test_orders_each_column_top_to_bottom_before_the_next_column():
    boxes = [
        _box("left-1", top=10, x0=0, x1=40, col_id=0),
        _box("right-1", top=12, x0=60, x1=100, col_id=1),
        _box("left-2", top=30, x0=0, x1=40, col_id=0),
        _box("right-2", top=32, x0=60, x1=100, col_id=1),
    ]

    assert _ordered_texts(boxes) == ["left-1", "left-2", "right-1", "right-2"]


def test_spanning_blocks_split_column_reading_into_vertical_bands():
    boxes = [
        _box("page-title", top=0, x0=0, x1=100, col_id=0),
        _box("upper-left", top=20, x0=0, x1=40, col_id=0),
        _box("upper-right", top=22, x0=60, x1=100, col_id=1),
        _box("section-title", top=50, x0=0, x1=100, col_id=0),
        _box("lower-left", top=70, x0=0, x1=40, col_id=0),
        _box("lower-right", top=72, x0=60, x1=100, col_id=1),
    ]

    assert _ordered_texts(boxes) == [
        "page-title",
        "upper-left",
        "upper-right",
        "section-title",
        "lower-left",
        "lower-right",
    ]


def test_large_shared_vertical_gap_separates_grid_rows_before_column_order():
    boxes = [
        _box("upper-left-1", top=10, x0=0, x1=40, col_id=0),
        _box("upper-right-1", top=12, x0=60, x1=100, col_id=1),
        _box("upper-left-2", top=30, x0=0, x1=40, col_id=0),
        _box("upper-right-2", top=32, x0=60, x1=100, col_id=1),
        _box("lower-left-1", top=100, x0=0, x1=40, col_id=0),
        _box("lower-right-1", top=102, x0=60, x1=100, col_id=1),
        _box("lower-left-2", top=120, x0=0, x1=40, col_id=0),
        _box("lower-right-2", top=122, x0=60, x1=100, col_id=1),
    ]

    assert _ordered_texts(boxes) == [
        "upper-left-1",
        "upper-left-2",
        "upper-right-1",
        "upper-right-2",
        "lower-left-1",
        "lower-left-2",
        "lower-right-1",
        "lower-right-2",
    ]


def test_preserves_single_column_page_order_and_page_boundaries():
    boxes = [
        _box("page-1-line-2", top=30, x0=0, x1=100, page_number=1),
        _box("page-2-line-1", top=10, x0=0, x1=100, page_number=2),
        _box("page-1-line-1", top=10, x0=0, x1=100, page_number=1),
    ]

    assert _ordered_texts(boxes) == ["page-1-line-1", "page-1-line-2", "page-2-line-1"]
