#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
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
"""Convert title-aware chunks into retrieval children with parent context."""

from __future__ import annotations

from typing import Any

from common.token_utils import num_tokens_from_string
from rag.flow.base import ProcessBase, ProcessParamBase
from rag.flow.chunker.schema import TokenChunkerFromUpstream
from rag.nlp import naive_merge


class ParentChildChunkerParam(ProcessParamBase):
    def __init__(self):
        super().__init__()
        self.parent_token_size = 1024
        self.child_token_size = 300
        self.min_child_token_size = 120
        self.child_overlap_percent = 0

    def check(self):
        self.check_positive_integer(self.parent_token_size, "Parent chunk token size.")
        self.check_positive_integer(self.child_token_size, "Child chunk token size.")
        self.check_positive_integer(self.min_child_token_size, "Minimum child chunk token size.")
        self.check_decimal_float(self.child_overlap_percent / 100, "Child overlap percentage: [0, 30].")
        if self.child_overlap_percent > 30:
            raise ValueError("Child overlap percentage: [0, 30].")
        if self.min_child_token_size > self.child_token_size:
            raise ValueError("Minimum child chunk token size must not exceed child chunk token size.")

    def get_input_form(self) -> dict[str, dict]:
        return {}


def _merge_to_token_size(text: str, token_size: int, overlap_percent: int = 0) -> list[str]:
    return [chunk.strip() for chunk in naive_merge(text, token_size, "\n\n。；！？", overlap_percent) if chunk and chunk.strip()]


def _merge_short_children(children: list[str], min_tokens: int, target_tokens: int) -> list[str]:
    """Attach undersized trailing/leading fragments without crossing a title parent."""
    merged: list[str] = []
    pending = ""

    for child in children:
        candidate = f"{pending}\n{child}".strip() if pending else child
        if num_tokens_from_string(candidate) < min_tokens:
            pending = candidate
            continue
        merged.append(candidate)
        pending = ""

    if pending:
        if merged and num_tokens_from_string(f"{merged[-1]}\n{pending}") <= target_tokens + min_tokens:
            merged[-1] = f"{merged[-1]}\n{pending}".strip()
        else:
            merged.append(pending)

    return merged


def build_parent_child_chunks(
    chunks: list[dict[str, Any]],
    parent_token_size: int,
    child_token_size: int,
    min_child_token_size: int,
    child_overlap_percent: int,
) -> list[dict[str, Any]]:
    """Keep TitleChunker boundaries as parents and index only their children."""
    output: list[dict[str, Any]] = []

    for source_index, source in enumerate(chunks):
        if not isinstance(source, dict):
            continue

        text = str(source.get("text") or "").strip()
        if source.get("doc_type_kwd", "text") != "text" or not text:
            output.append(dict(source))
            continue

        parents = _merge_to_token_size(text, parent_token_size)
        for parent_index, parent_text in enumerate(parents):
            children = _merge_to_token_size(parent_text, child_token_size, child_overlap_percent)
            children = _merge_short_children(children, min_child_token_size, child_token_size)

            parent_key = f"{source_index}:{parent_index}"
            parent_tokens = num_tokens_from_string(parent_text)
            for child_index, child_text in enumerate(children):
                child = dict(source)
                child["text"] = child_text
                child["mom"] = parent_text
                child["parent_id"] = parent_key
                child["parent_token_count"] = parent_tokens
                child["child_token_count"] = num_tokens_from_string(child_text)
                child["child_index"] = child_index
                output.append(child)

    return output


class ParentChildChunker(ProcessBase):
    component_name = "ParentChildChunker"

    async def _invoke(self, **kwargs):
        try:
            from_upstream = TokenChunkerFromUpstream.model_validate(kwargs)
        except Exception as exc:
            self.set_output("_ERROR", f"Input error: {exc}")
            return

        if from_upstream.output_format != "chunks":
            self.set_output("_ERROR", "ParentChildChunker requires TitleChunker chunks input.")
            return

        self.set_output("output_format", "chunks")
        self.callback(0.05, "Build parent-child chunks.")
        chunks = build_parent_child_chunks(
            from_upstream.chunks or [],
            self._param.parent_token_size,
            self._param.child_token_size,
            self._param.min_child_token_size,
            self._param.child_overlap_percent,
        )
        self.set_output("chunks", chunks)
        self.callback(1, f"Built {len(chunks)} retrieval children.")
