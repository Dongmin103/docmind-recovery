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

"""PaddleOCR text-line recognizer used by the optional DeepDoc OCR path."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from common.file_utils import get_project_base_directory

PADDLEOCR_KOREAN_V5_MODEL = "korean_PP-OCRv5_mobile_rec"
PADDLEOCR_KOREAN_V5_MODEL_DIR_ENV = "PADDLEOCR_KOREAN_V5_MODEL_DIR"


def default_paddleocr_korean_v5_model_dir() -> str:
    return os.path.join(get_project_base_directory(), "rag/res/paddleocr", f"{PADDLEOCR_KOREAN_V5_MODEL}_infer")


class PaddleOCRKoreanV5Recognizer:
    """Recognize DeepDoc-detected text crops with PaddleOCR's Korean PP-OCRv5 model."""

    def __init__(self, device_id: int | None = None, model_dir: str | None = None):
        model_dir = model_dir or os.environ.get(PADDLEOCR_KOREAN_V5_MODEL_DIR_ENV, default_paddleocr_korean_v5_model_dir())
        if not Path(model_dir).is_dir():
            raise RuntimeError(f"{PADDLEOCR_KOREAN_V5_MODEL} model assets are missing at {model_dir}. Rebuild the RAGFlow image so the preloaded PaddleOCR model is available.")

        try:
            from paddleocr import TextRecognition
        except ImportError as exc:
            raise RuntimeError("PaddleOCR is not installed. Rebuild the RAGFlow image with the PaddleOCR dependencies.") from exc

        device = f"gpu:{device_id}" if device_id is not None else "cpu"
        self.rec_batch_num = int(os.environ.get("PADDLEOCR_RECOGNITION_BATCH_SIZE", "16"))
        self.predictor = TextRecognition(
            model_name=PADDLEOCR_KOREAN_V5_MODEL,
            model_dir=model_dir,
            device=device,
            cpu_threads=int(os.environ.get("OCR_INTRA_OP_NUM_THREADS", "2")),
        )

    @staticmethod
    def _result_data(result: Any) -> Mapping[str, Any]:
        if isinstance(result, Mapping):
            data: Any = result
        elif hasattr(result, "json"):
            data = result.json
            if isinstance(data, str):
                data = json.loads(data)
        elif hasattr(result, "res"):
            data = result.res
        else:
            raise TypeError(f"Unexpected PaddleOCR recognition result: {type(result)!r}")

        if not isinstance(data, Mapping):
            raise TypeError(f"Unexpected PaddleOCR recognition payload: {type(data)!r}")
        payload = data.get("res", data)
        if not isinstance(payload, Mapping):
            raise TypeError(f"Unexpected PaddleOCR recognition payload: {type(payload)!r}")
        return payload

    def __call__(self, img_list: Sequence[Any]) -> tuple[list[list[float | str]], float]:
        if not img_list:
            return [], 0.0

        started = time.time()
        results = self.predictor.predict(list(img_list), batch_size=self.rec_batch_num)
        recognized: list[list[float | str]] = []
        for result in results:
            payload = self._result_data(result)
            text = payload.get("rec_text", "")
            score = payload.get("rec_score", 0.0)
            recognized.append([str(text), float(score)])

        if len(recognized) != len(img_list):
            raise RuntimeError(f"PaddleOCR returned {len(recognized)} results for {len(img_list)} text crops")
        return recognized, time.time() - started
