"""Isolated SQLite tests: no application bootstrap, production DB or model calls."""
import ast
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import peewee as pw
import engine
import make_sample


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.db = pw.SqliteDatabase(':memory:')
        class Base(pw.Model):
            create_time=pw.BigIntegerField(null=True)
            update_time=pw.BigIntegerField(null=True)
            create_date=pw.DateTimeField(null=True)
            update_date=pw.DateTimeField(null=True)
        Base._meta.database=self.db
        root=Path(os.environ.get('DOCMIND_APP_ROOT',Path(__file__).resolve().parents[2]/'app'))
        tree=ast.parse((root/'api/db/db_models.py').read_text(encoding='utf-8'))
        names={'DocmindProject','DocmindFolder','DocmindSource'}
        nodes=[n for n in tree.body if isinstance(n,ast.ClassDef) and n.name in names]
        scope={n:getattr(pw,n) for n in ('CharField','IntegerField','BooleanField')}
        scope['DataBaseModel']=Base
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'isolated-models','exec'),scope)
        self.m=NS(DB=self.db,**{n:scope[n] for n in names})
        self.db.create_tables([scope[n] for n in names])
        self.m.DocmindProject.create(id='p',tenant_id='t',dataset_id='k',catalog_source_mode='database')
    def tearDown(self):self.db.close()
    def register(self,source='handoff',**kw):return engine.register_source(self.m,'p',source,'합성',**kw)
    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.register()['action'],'would_create')
        self.assertEqual(self.m.DocmindFolder.select().count(),0)
        self.assertEqual(self.m.DocmindSource.select().count(),0)
    def test_idempotent_and_distinct_folder_ordinals(self):
        first=self.register(apply=True)
        self.assertEqual(self.register(apply=True)['action'],'unchanged')
        second=self.register('handoff-2',apply=True)
        self.assertEqual((first['ordinal'],second['ordinal']),(0,1))
        self.assertEqual(self.m.DocmindSource.select().count(),2)
    def test_disabled_and_conflicting_source_preserved(self):
        self.register(apply=True)
        self.m.DocmindSource.update(enabled=False).execute()
        with self.assertRaisesRegex(ValueError,'SOURCE_CONFLICT'):self.register(apply=True)
        self.assertFalse(self.m.DocmindSource.get().enabled)
    def test_wrong_project_and_path_id_rejected(self):
        with self.assertRaisesRegex(ValueError,'DATABASE_PROJECT'):engine.register_source(self.m,'missing','x','x',True)
        with self.assertRaisesRegex(ValueError,'INVALID_IDENTIFIER'):self.register('../unsafe',apply=True)
        self.assertEqual(self.m.DocmindSource.select().count(),0)
    def test_conflict_does_not_leave_orphan_folder(self):
        self.m.DocmindSource.create(id='handoff',project_id='other',display_name='old')
        with self.assertRaisesRegex(ValueError,'SOURCE_CONFLICT'):self.register(apply=True)
        self.assertEqual(self.m.DocmindFolder.select().count(),0)
        self.assertEqual(self.m.DocmindSource.get().project_id,'other')


class AcceptanceTests(unittest.TestCase):
    def rows(self):
        job=NS(lifecycle_state='COMPLETE',cleanup_state='COMPLETE',host_cleanup_state='COMPLETE',
               source_document_id='map',document_id='doc',chunk_set_id='set',parser_run_id='run',
               project_id='p',source_id='source',version_id='v')
        version=NS(lifecycle_state='ACTIVE',source_document_id='map',document_id='doc',chunk_set_id='set',parser_run_id='run',search_cleanup_complete=True)
        mapping=NS(deleted_at=None,project_id='p',source_id='source',document_id='doc',active_source_version_id='v')
        doc=NS(status='1',active_chunk_set_id='set')
        run=NS(doc_id='doc',chunk_set_id='set',lifecycle='READY',raw_artifact_ref=None,staged_chunk_count=2)
        return job,version,mapping,doc,run
    def test_complete_requires_cleanup_and_matching_active_identity(self):
        rows=self.rows()
        self.assertTrue(all(engine.completion_checks(*rows).values()))
        rows[0].host_cleanup_state='PENDING'
        self.assertFalse(all(engine.completion_checks(*rows).values()))
        rows=self.rows();rows[3].active_chunk_set_id='old'
        self.assertFalse(all(engine.completion_checks(*rows).values()))
    def test_residual_artifact_or_uncertified_version_fails(self):
        rows=self.rows();rows[4].raw_artifact_ref='artifact://remaining'
        self.assertFalse(all(engine.completion_checks(*rows).values()))
        rows=self.rows();rows[1].search_cleanup_complete=False
        self.assertFalse(all(engine.completion_checks(*rows).values()))
    def test_fixture_valid_xml_and_no_overwrite(self):
        with TemporaryDirectory() as d:
            path=Path(d)/'sample.docx'
            report=make_sample.create(path)
            with ZipFile(path) as z:
                for name in z.namelist():ET.fromstring(z.read(name))
            self.assertFalse(report['encrypted'])
            with self.assertRaises(FileExistsError):make_sample.create(path)


if __name__=='__main__':unittest.main()
