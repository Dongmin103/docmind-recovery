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

import ast

from common.token_utils import num_tokens_from_string
from rag.flow.chunker.title_chunker.common import (
    PDF_POSITIONS_KEY,
    BaseTitleChunker,
    resolve_target_level,
)

MIN_GROUP_TOKENS = 32
MAX_GROUP_TOKENS = 1024
HANGING_LABEL_X_GAP = 24
HANGING_LABEL_MAX_WIDTH = 80


def _is_empty_literal_line(text):
    stripped = text.strip()
    if not stripped:
        return True
    try:
        value = ast.literal_eval(stripped)
    except (SyntaxError, ValueError):
        return False
    if value in (None, ""):
        return True
    if isinstance(value, (list, tuple)):
        return all(item in (None, "") for item in value)
    return False


def _has_media_payload(record):
    return any(record.get(key) is not None for key in ("image", "table", "img_id"))


def is_placeholder_only_media(record):
    if record.get("doc_type_kwd") not in {"image", "table"}:
        return False
    if _has_media_payload(record):
        return False

    lines = [line.strip() for line in (record.get("text") or "").splitlines() if line.strip()]
    if not lines:
        return True
    if len(lines) == 1:
        return _is_empty_literal_line(lines[0])
    return _is_empty_literal_line(lines[-1])


def _mark_attachment_only(record):
    marked = dict(record)
    marked["available_int"] = 0
    return marked


def _group_token_count(records):
    return sum(num_tokens_from_string(record.get("text") or "") for record in records if record.get("doc_type_kwd") == "text")


def _is_text_group(records):
    return bool(records) and all(record.get("doc_type_kwd") == "text" for record in records)


def _positions(records):
    positions = []
    for record in records:
        for position in record.get(PDF_POSITIONS_KEY) or record.get("position_int") or []:
            if not isinstance(position, (list, tuple)) or len(position) < 5:
                continue
            try:
                positions.append(tuple(map(float, position[:5])))
            except (TypeError, ValueError):
                continue
    return positions


def _position_pages(records):
    return {int(position[0]) for position in _positions(records)}


def _same_single_page(left_records, right_records):
    pages = _position_pages(left_records) | _position_pages(right_records)
    return len(pages) == 1


def _same_nonempty_column(left_records, right_records):
    left_column_id = left_records[0].get("column_id")
    right_column_id = right_records[0].get("column_id")
    if not (left_column_id and left_column_id == right_column_id):
        return False
    if not (_positions(left_records) or _positions(right_records)):
        return True
    return _same_single_page(left_records, right_records)


def _group_bbox(records):
    positions = _positions(records)
    if not positions:
        return None
    pages = {int(page) for page, *_ in positions}
    if len(pages) != 1:
        return None
    return (
        pages.pop(),
        min(x0 for _, x0, _, _, _ in positions),
        max(x1 for _, _, x1, _, _ in positions),
        min(top for _, _, _, top, _ in positions),
        max(bottom for _, _, _, _, bottom in positions),
    )


def _is_narrow_label(records):
    bbox = _group_bbox(records)
    return bbox is not None and bbox[2] - bbox[1] <= HANGING_LABEL_MAX_WIDTH and _group_token_count(records) < MIN_GROUP_TOKENS


def _has_vertical_contact(left_bbox, right_bbox):
    return min(left_bbox[4], right_bbox[4]) >= max(left_bbox[3], right_bbox[3])


def _has_direct_horizontal_edge_gap(left_bbox, right_bbox):
    if left_bbox[2] <= right_bbox[1]:
        gap = right_bbox[1] - left_bbox[2]
        return 0 < gap <= HANGING_LABEL_X_GAP
    if right_bbox[2] <= left_bbox[1]:
        gap = left_bbox[1] - right_bbox[2]
        return 0 < gap <= HANGING_LABEL_X_GAP
    return False


def _is_hanging_label_merge(left_records, right_records):
    # Only a narrow short label may attach to its immediate body. Do not use
    # broad x-overlap as a fallback because that can join distinct columns.
    if not (_is_narrow_label(left_records) or _is_narrow_label(right_records)):
        return False

    left_bbox = _group_bbox(left_records)
    right_bbox = _group_bbox(right_records)
    if left_bbox is None or right_bbox is None or left_bbox[0] != right_bbox[0]:
        return False
    return _has_vertical_contact(left_bbox, right_bbox) and _has_direct_horizontal_edge_gap(left_bbox, right_bbox)


def _can_absorb_text_group(left_records, right_records):
    if _same_nonempty_column(left_records, right_records):
        return True
    if not _same_single_page(left_records, right_records):
        return False
    return _is_hanging_label_merge(left_records, right_records)


def _mark_isolated_label(records):
    return [dict(record, available_int=0) for record in records]


def _apply_group_searchability(records):
    if _group_token_count(records) < MIN_GROUP_TOKENS:
        for record in records:
            record["available_int"] = 0
        return
    for record in records:
        record.pop("available_int", None)



def _find_forward_text_group(record_groups, start_index):
    index = start_index + 1
    if index < len(record_groups) and _is_text_group(record_groups[index]):
        return index
    return None


def _compact_short_text_groups(record_groups, max_group_tokens=MAX_GROUP_TOKENS):
    compacted = [list(records) for records in record_groups if records]
    index = 0
    while index < len(compacted):
        records = compacted[index]
        if not _is_text_group(records):
            index += 1
            continue

        token_count = _group_token_count(records)
        if token_count >= MIN_GROUP_TOKENS:
            index += 1
            continue

        forward_index = _find_forward_text_group(compacted, index)
        if forward_index is not None and _can_absorb_text_group(records, compacted[forward_index]) and token_count + _group_token_count(compacted[forward_index]) <= max_group_tokens:
            records.extend(compacted[forward_index])
            del compacted[forward_index]
            _apply_group_searchability(records)
            index += 1
            continue

        if index > 0 and _is_text_group(compacted[index - 1]) and _can_absorb_text_group(compacted[index - 1], records) and token_count + _group_token_count(compacted[index - 1]) <= max_group_tokens:
            compacted[index - 1].extend(records)
            _apply_group_searchability(compacted[index - 1])
            del compacted[index]
            continue

        compacted[index] = _mark_isolated_label(records)
        index += 1

    return compacted


def _build_section_ids(levels, target_level):
    """Assign a stable section id that increments whenever a title at or above
    ``target_level`` starts a new section."""
    sec_ids = []
    sid = 0
    for i, level in enumerate(levels):
        if target_level is not None and level <= target_level and i > 0:
            sid += 1
        sec_ids.append(sid)
    return sec_ids


def _resolve_group_target_level(levels, hierarchy, most_level):
    """Pick the level used as the grouping target for the group method."""
    if hierarchy and int(hierarchy) > 0:
        return resolve_target_level(levels, hierarchy)
    return most_level


def _should_compact_short_text_groups(hierarchy):
    return not (hierarchy and int(hierarchy) > 0)


class GroupTitleChunker(BaseTitleChunker):
    """Group consecutive records under the same title into one chunk."""

    start_message = "Start to group by title levels."

    def resolve_levels(self, line_records):
        """Resolve title levels via the shared outline/frequency strategy."""
        return self.resolve_title_levels(line_records)

    def build_chunks(self, line_records, resolved):
        """Build chunks by merging records inside the same logical section.

        The merge ceiling uses the configurable ``chunk_token_cap`` (0/None
        means "no ceiling"). The post-build ``_enforce_token_cap`` in
        BaseTitleChunker is the single hard guarantee, so any residual over-cap
        chunk (e.g. a single record bigger than the cap) is still re-split
        there.
        """
        target_level = _resolve_group_target_level(
            resolved["levels"],
            self.param.hierarchy,
            resolved["most_level"],
        )
        sec_ids = _build_section_ids(resolved["levels"], target_level)
        record_groups = []
        tk_cnt = 0
        last_sid = -2

        group_cap = getattr(self.param, "chunk_token_cap", 0) or MAX_GROUP_TOKENS

        # Initial grouping stays within the current section and a strict
        # nonempty column identity. Short-text absorption is handled only by
        # the post-group compaction pass below.
        for record, sec_id in zip(line_records, sec_ids):
            if record["doc_type_kwd"] != "text":
                if _should_compact_short_text_groups(self.param.hierarchy) and is_placeholder_only_media(record):
                    record_groups.append([_mark_attachment_only(record)])
                    continue
                record_groups.append([record])
                tk_cnt = 0
                last_sid = -2
                continue

            text = record["text"]
            if not text.strip():
                continue

            token_count = num_tokens_from_string(text)
            can_fit = tk_cnt + token_count <= group_cap
            should_merge = record_groups and record_groups[-1][0]["doc_type_kwd"] == "text" and _same_nonempty_column(record_groups[-1], [record]) and can_fit and sec_id == last_sid

            if should_merge:
                record_groups[-1].append(record)
                tk_cnt += token_count
            else:
                record_groups.append([record])
                tk_cnt = token_count

            last_sid = sec_id

        if _should_compact_short_text_groups(self.param.hierarchy):
            record_groups = _compact_short_text_groups(record_groups, group_cap)

        return self.build_chunks_from_record_groups(record_groups)
