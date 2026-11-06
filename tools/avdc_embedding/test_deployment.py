"""CPU-only regression checks; no model download, document or running service."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = load('avdc_service', ROOT / 'service.py')
pool = load('avdc_pool', ROOT / 'pool.py')


class DeploymentTests(unittest.TestCase):
    def test_sources_match_verified_running_artifacts(self):
        record = json.loads((ROOT.parents[1] / 'docs/measurements/avdc-openvino-deployment-2026-11-06.json').read_text())
        for name, expected in record['source_sha256'].items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), expected, name)

    def test_affinity_uses_embedding_budget_without_support_cpus(self):
        config = json.loads((ROOT / 'config.example.json').read_text())
        available = set(range(48)) | set(range(56, 96))
        groups = pool.validate_groups(config['cpu_groups'], available)
        self.assertEqual(len(groups), 22)
        self.assertEqual({cpu for group in groups for cpu in group}, available)
        self.assertNotIn('api_key', config)

    def test_overlapping_or_out_of_container_groups_rejected(self):
        for groups in [[[0, 1, 2, 3], [3, 4, 5, 6]], [[0, 1, 2, 99]], [[0, 1]], [[True, 1, 2, 3]]]:
            with self.assertRaises(ValueError):
                pool.validate_groups(groups, set(range(8)))

    def make_engine(self):
        engine = service.GTEEngine.__new__(service.GTEEngine)
        engine.device = 'cpu'
        engine.cpu_short_batch = 16
        engine.tokenizer = lambda texts, **kwargs: {'input_ids': [range(int(t)) for t in texts]}
        calls = []
        def batch(texts):
            calls.append(texts)
            return [[int(t)] for t in texts]
        engine._retry = batch
        return engine, calls

    def test_short_batches_long_singletons_and_original_order(self):
        engine, calls = self.make_engine()
        texts = ['10', '129'] + ['128'] * 17 + ['8192']
        vectors, tokens = engine.encode(texts)
        self.assertEqual(vectors, [[int(t)] for t in texts])
        self.assertEqual(tokens, sum(map(int, texts)))
        self.assertEqual([len(c) for c in calls], [1, 1, 16, 1, 1])

    def test_overlength_fails_before_any_inference(self):
        engine, calls = self.make_engine()
        with self.assertRaises(service.RequestError):
            engine.encode(['10', '8193'])
        self.assertEqual(calls, [])

    def test_auth_and_lock_recovery(self):
        engine = SimpleNamespace(encode=lambda texts: (_ for _ in ()).throw(RuntimeError('failed')))
        endpoint = service.EmbeddingService(engine, 'synthetic-key')
        with self.assertRaises(service.RequestError) as caught:
            endpoint.embed({'input': 'text'}, 'Bearer wrong')
        self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(RuntimeError):
            endpoint.embed({'input': 'text'}, 'Bearer synthetic-key')
        engine.encode = lambda texts: ([[0.0] * 768], 1)
        self.assertEqual(endpoint.embed({'input': 'text'}, 'Bearer synthetic-key')['usage']['total_tokens'], 1)

    def test_openvino_fp32_four_threads_single_stream_and_cpu_only(self):
        settings = {}
        def compile_model(path, device, config):
            settings.update(config)
            self.assertEqual(device, 'CPU')
            self.assertTrue(path.endswith('/model.xml'))
            return SimpleNamespace(get_property=lambda key: config[key])
        ov = SimpleNamespace(Core=lambda: SimpleNamespace(compile_model=compile_model), Type=SimpleNamespace(f32='f32'))
        torch = SimpleNamespace(set_num_threads=lambda value: None)
        tokenizer = SimpleNamespace(from_pretrained=lambda *a, **k: None)
        transformers = SimpleNamespace(AutoModel=object(), AutoTokenizer=tokenizer)
        config = {'device': 'cpu', 'threads': 4, 'cpu_short_batch': 16, 'model_path': 'synthetic'}
        with patch.dict('sys.modules', {'torch': torch, 'transformers': transformers, 'openvino': ov}):
            service.GTEEngine(config)
            self.assertEqual(settings['NUM_STREAMS'], 1)
            self.assertEqual(settings['INFERENCE_NUM_THREADS'], 4)
            self.assertEqual(settings['INFERENCE_PRECISION_HINT'], 'f32')
            self.assertFalse(settings['ENABLE_CPU_PINNING'])
            with self.assertRaises(RuntimeError):
                service.GTEEngine(dict(config, threads=2))
            with self.assertRaises(ValueError):
                service.GTEEngine(dict(config, cpu_short_batch=8))

    def test_invalid_backend_vectors_are_rejected(self):
        engine = service.GTEEngine.__new__(service.GTEEngine)
        engine.np = np
        engine.torch = None
        engine.tokenizer = lambda *a, **k: {'input_ids': np.zeros((1, 2))}
        for output in [np.zeros((1, 767)), np.full((1, 768), np.nan), np.full((1, 768), np.inf)]:
            engine.model = lambda data: [output]
            with self.assertRaises(RuntimeError):
                engine._batch(['synthetic'])
        engine.model = lambda data: [np.zeros((1, 768), dtype=np.float32)]
        self.assertEqual(len(engine._batch(['synthetic'])[0]), 768)


if __name__ == '__main__':
    unittest.main()
