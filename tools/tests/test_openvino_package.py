import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('ov_install',ROOT/'deploy/openvino/install.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class PackageTests(unittest.TestCase):
    def test_integrity_and_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'data').write_bytes(b'fixture')
            record={'files':[{'path':'data','bytes':7,'sha256':hashlib.sha256(b'fixture').hexdigest()}]}
            (root/'manifest.json').write_text(json.dumps(record))
            with patch.object(installer,'ROOT',root):
                installer.verify()
                (root/'data').write_bytes(b'changed')
                with self.assertRaises(ValueError):installer.verify()
                record['files'][0]['path']='../outside'
                (root/'manifest.json').write_text(json.dumps(record))
                with self.assertRaises(ValueError):installer.verify()

    def test_template_is_independent_and_no_ocr(self):
        c=json.loads((ROOT/'deploy/openvino/compose.template.json').read_text())
        self.assertEqual(len(c['services']),8)
        self.assertNotIn('ocr',c['services'])
        self.assertNotIn('include',c)
        self.assertNotIn('${',json.dumps(c))
        for s in c['services'].values():
            self.assertNotIn('container_name',s)
            self.assertNotIn('gpus',s)
        e=c['services']['docmind']['environment']
        self.assertEqual(e['PARSER_PLATFORM_KORDOC_CHUNKER_REVISION'],'docmind-kordoc-chunker-v3')
        self.assertEqual(e['DOCMIND_ANSWER_ENABLED'],'0')

    def test_source_zip_includes_cpu_and_handoff(self):
        s=json.loads((ROOT/'deploy/package.json').read_text())
        for tree in ('tools/avdc_embedding','tools/handoff','deploy/openvino'):
            self.assertIn(tree,s['trees'])


if __name__=='__main__':unittest.main()
