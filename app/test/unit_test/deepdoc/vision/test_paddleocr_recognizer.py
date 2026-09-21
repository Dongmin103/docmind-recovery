import sys
import types

import numpy as np

from deepdoc.vision import ocr as deepdoc_ocr
from deepdoc.vision.paddleocr_recognizer import (
    PADDLEOCR_KOREAN_V5_MODEL,
    PADDLEOCR_KOREAN_V5_MODEL_DIR_ENV,
    PaddleOCRKoreanV5Recognizer,
)


class _Result:
    def __init__(self, text, score):
        self.json = {"res": {"rec_text": text, "rec_score": score}}


def test_paddleocr_korean_v5_recognizes_korean_english_and_numbers(monkeypatch, tmp_path):
    calls = {}

    class FakeTextRecognition:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def predict(self, images, batch_size):
            calls["images"] = images
            calls["batch_size"] = batch_size
            return [_Result("품질 Quality 2026", 0.98), _Result("GMP 123", 0.91)]

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(TextRecognition=FakeTextRecognition))
    monkeypatch.setenv(PADDLEOCR_KOREAN_V5_MODEL_DIR_ENV, str(tmp_path))

    recognizer = PaddleOCRKoreanV5Recognizer()
    results, elapsed = recognizer([np.zeros((12, 60, 3), dtype=np.uint8), np.zeros((12, 40, 3), dtype=np.uint8)])

    assert calls["init"]["model_name"] == PADDLEOCR_KOREAN_V5_MODEL
    assert calls["init"]["model_dir"] == str(tmp_path)
    assert calls["init"]["device"] == "cpu"
    assert calls["batch_size"] == 16
    assert results == [["품질 Quality 2026", 0.98], ["GMP 123", 0.91]]
    assert elapsed >= 0


def test_ocr_selects_paddleocr_only_when_explicitly_enabled(monkeypatch, tmp_path):
    created = []

    class FakeDetector:
        def __init__(self, model_dir, device_id=None):
            created.append(("detector", model_dir, device_id))

    class FakeDeepDocRecognizer:
        def __init__(self, model_dir, device_id=None):
            created.append(("deepdoc", model_dir, device_id))

    class FakePaddleOCRRecognizer:
        def __init__(self, device_id=None):
            created.append(("paddle", device_id))

    monkeypatch.setattr(deepdoc_ocr, "TextDetector", FakeDetector)
    monkeypatch.setattr(deepdoc_ocr, "TextRecognizer", FakeDeepDocRecognizer)
    monkeypatch.setattr(deepdoc_ocr, "PaddleOCRKoreanV5Recognizer", FakePaddleOCRRecognizer)
    monkeypatch.setattr(deepdoc_ocr.settings, "PARALLEL_DEVICES", 0)

    monkeypatch.delenv("DEEPDOC_TEXT_RECOGNIZER", raising=False)
    deepdoc_ocr.OCR(str(tmp_path))
    assert any(item[0] == "deepdoc" for item in created)
    assert not any(item[0] == "paddle" for item in created)

    created.clear()
    monkeypatch.setenv("DEEPDOC_TEXT_RECOGNIZER", "paddleocr-korean-v5")
    deepdoc_ocr.OCR(str(tmp_path))
    assert any(item[0] == "paddle" for item in created)
    assert not any(item[0] == "deepdoc" for item in created)
