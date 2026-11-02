#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
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
import logging
import math
import time
from abc import ABC
from typing import Tuple, List

import numpy as np
import requests

from common.token_utils import truncate, total_token_count_from_response


class Base(ABC):
    def __init__(self, key, model_name, **kwargs):
        pass

    def similarity(self, query: str, texts: List, *, normalize: bool = True) -> Tuple[np.ndarray, int]:
        """Score ``texts`` against ``query`` and return ``(rank, token_count)``.

        This is the single public entry point shared by every reranker. It
        short-circuits empty input and, by default, maps returned scores to
        ``[0, 1]`` regardless of what the backend emits
        (relevance scores, cosine similarities or raw logits). Downstream
        hybrid scoring blends the reranker output with token similarity on a
        fixed ``[0, 1]`` scale, so an un-normalized provider (e.g. NVIDIA's
        unbounded logits) would otherwise corrupt the final ordering.

        Subclasses implement provider-specific scoring in :meth:`_compute_rank`
        and must not normalize themselves.
        ``normalize=False`` preserves provider scores for calibrated search
        relevance gates; those scores are not probabilities.
        """
        if not query or not texts:
            return np.zeros(len(texts) if texts else 0, dtype=float), 0
        rank, token_count = self._compute_rank(query, texts)
        rank = np.asarray(rank, dtype=float)
        if rank.size:
            logging.debug(
                "Rerank %s scores before normalization: count=%d min=%.4f max=%.4f",
                self.__class__.__name__,
                rank.size,
                float(np.min(rank)),
                float(np.max(rank)),
            )
        # Search relevance gates need provider magnitudes, not a best-in-batch 1.
        return (self._normalize_rank(rank) if normalize else rank), token_count

    def _compute_rank(self, query: str, texts: List) -> Tuple[np.ndarray, int]:
        """Provider-specific scoring. ``query`` and ``texts`` are non-empty."""
        raise NotImplementedError("Please implement _compute_rank method!")

    @staticmethod
    def _normalize_rank(rank: np.ndarray) -> np.ndarray:
        """Guarantee scores land in ``[0, 1]`` for the hybrid blend.

        Providers that already emit calibrated relevance scores in ``[0, 1]``
        (Cohere, Jina, Voyage, ...) are returned unchanged, so their absolute
        magnitudes, ``similarity_threshold`` semantics and reported
        ``vector_similarity`` are preserved. Only out-of-range output (e.g.
        NVIDIA's unbounded, often negative logits) is rescaled: a batch with a
        usable spread is min-max mapped onto ``[0, 1]`` (which stops a negative
        logit from dragging a relevant chunk below pure keyword matches once
        weighted by ``vtweight``), while a spreadless batch (including a single
        candidate) has no relative signal and is clamped instead, so a lone
        high score is not silently zeroed.
        """
        if rank.size == 0:
            return rank
        min_rank = float(np.min(rank))
        max_rank = float(np.max(rank))

        if min_rank >= 0.0 and max_rank <= 1.0:
            return rank
        span = max_rank - min_rank
        if span < 1e-3:
            return np.clip(rank, 0.0, 1.0)
        return (rank - min_rank) / span


class JinaRerank(Base):
    _FACTORY_NAME = "Jina"

    def __init__(self, key, model_name="jina-reranker-v2-base-multilingual", base_url="https://api.jina.ai/v1/rerank"):
        self.base_url = base_url or "https://api.jina.ai/v1/rerank"
        self.headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        self.model_name = model_name

    def _compute_rank(self, query: str, texts: List) -> Tuple[np.ndarray, int]:
        texts = [truncate(t, 8196) for t in texts]
        data = {"model": self.model_name, "query": query, "documents": texts, "top_n": len(texts)}
        response = requests.post(self.base_url, headers=self.headers, json=data, timeout=30)
        response.raise_for_status()
        res = response.json()
        results = res.get("results") if isinstance(res, dict) else None
        if not isinstance(results, list) or len(results) != len(texts):
            count = len(results) if isinstance(results, list) else 0
            raise ValueError(f"Jina returned {count} rerank results for {len(texts)} documents")
        rank = np.zeros(len(texts), dtype=float)
        seen: set[int] = set()
        for result in results:
            index = result.get("index") if isinstance(result, dict) else None
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(texts)
                or index in seen
            ):
                raise ValueError(f"unexpected Jina rerank index: {index}")
            relevance_score = result.get("relevance_score")
            if (
                isinstance(relevance_score, bool)
                or not isinstance(relevance_score, (int, float))
                or not math.isfinite(relevance_score)
            ):
                raise ValueError(
                    f"unexpected Jina rerank relevance_score at index {index}: {relevance_score!r}"
                )
            seen.add(index)
            rank[index] = relevance_score
        return rank, total_token_count_from_response(res)


class KURERerank(Base):
    """Score all candidates, preserving absolute scores across API batches."""

    MODEL = "kure-reranker-nano-20261016"
    URL = "https://kure.kullm.ai/api/v1/rerank"
    BATCH_SIZE = 64
    DEADLINE_SECONDS = 45

    def __init__(self, key, model_name=MODEL, base_url=None, **kwargs):
        if model_name != self.MODEL or (base_url and base_url.rstrip("/") != self.URL):
            raise ValueError("KURE requires its pinned snapshot and official rerank endpoint")
        if not key:
            raise ValueError("Missing KURE API credential")
        self.headers = {"Authorization": f"Bearer {key}", "User-Agent": "DocMind-KURE/1.0", "Accept": "application/json"}

    def _compute_rank(self, query, texts):
        deadline = time.monotonic() + self.DEADLINE_SECONDS
        rank = np.zeros(len(texts), dtype=float)
        tokens = 0
        for offset in range(0, len(texts), self.BATCH_SIZE):
            batch = texts[offset:offset + self.BATCH_SIZE]
            data = {"model": self.MODEL, "query": query, "documents": batch, "top_n": len(batch), "return_documents": False}
            for attempt in range(2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("KURE reranking deadline exceeded")
                resp = requests.post(self.URL, headers=self.headers, json=data, timeout=min(30, remaining), allow_redirects=False)
                if resp.status_code == 200:
                    break
                delay = resp.headers.get("Retry-After", "")
                if attempt == 0 and resp.status_code in (429, 503) and delay.isdigit() and int(delay) + 1 < deadline - time.monotonic():
                    time.sleep(max(1, int(delay)))
                    continue
                raise RuntimeError(f"KURE rerank HTTP {resp.status_code}")
            payload = resp.json()
            results = payload.get("results")
            if payload.get("model") != self.MODEL or not isinstance(results, list) or len(results) != len(batch):
                raise ValueError("Unexpected KURE model or result count")
            seen = set()
            for item in results:
                index, score = item["index"], item["relevance_score"]
                if type(index) is not int or not 0 <= index < len(batch) or index in seen:
                    raise ValueError("Invalid KURE result index")
                if type(score) not in (int, float) or not 0 <= score <= 1:
                    raise ValueError("Invalid KURE relevance score")
                rank[offset + index] = score
                seen.add(index)
            usage = payload.get("usage", {}).get("total_tokens")
            if type(usage) is not int or usage < 0:
                raise ValueError("Invalid KURE token count")
            tokens += usage
        return rank, tokens
