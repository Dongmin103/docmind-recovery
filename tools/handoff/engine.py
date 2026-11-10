"""Handoff CLI. Run in the configured API container; no index writes or deletes.

Only register-source --apply creates a folder/source. All other commands read.
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone


def identifier(value, maximum=64):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,%d}' % (maximum - 1), value):
        raise ValueError('INVALID_IDENTIFIER')
    return value


def register_source(m, project_id, source_id, display_name, apply=False):
    identifier(project_id, 32)
    identifier(source_id, 64)
    if not display_name.strip() or len(display_name) > 128:
        raise ValueError('INVALID_DISPLAY_NAME')
    P, F, S = m.DocmindProject, m.DocmindFolder, m.DocmindSource
    with m.DB.atomic():
        p = P.get_or_none(P.id == project_id)
        if p is None or p.catalog_source_mode != 'database':
            raise ValueError('DATABASE_PROJECT_REQUIRED')
        if apply:
            # Serialize ordinal allocation on the project, without changing its value.
            P.update(lock_version=P.lock_version).where(P.id == project_id).execute()
        folder_id = hashlib.sha256((project_id+'\x1f'+source_id+'\x1ffolder').encode()).hexdigest()[:32]
        existing = S.get_or_none(S.id == source_id)
        if existing:
            f = F.get_or_none(F.id == existing.default_folder_id)
            if (existing.project_id != project_id or not existing.enabled or
                existing.display_name != display_name or f is None or
                f.project_id != project_id or not f.enabled):
                raise ValueError('SOURCE_CONFLICT_EXISTING_PRESERVED')
            return {'action': 'unchanged', 'source_id': source_id, 'folder_id': f.id, 'project_id': project_id}
        if F.get_or_none((F.id == folder_id) | ((F.project_id == project_id) & (F.slug == source_id))):
            raise ValueError('FOLDER_CONFLICT_EXISTING_PRESERVED')
        tail = F.select().where(F.project_id == project_id).order_by(F.ordinal.desc()).first()
        ordinal = tail.ordinal + 1 if tail else 0
        if apply:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            stamp = int(now.replace(tzinfo=timezone.utc).timestamp()*1000)
            dates = dict(create_time=stamp, update_time=stamp, create_date=now, update_date=now)
            F.create(id=folder_id, project_id=project_id, slug=source_id,
                     display_name=display_name, ordinal=ordinal, enabled=True, **dates)
            S.create(id=source_id, project_id=project_id, default_folder_id=folder_id,
                     display_name=display_name, enabled=True, **dates)
        return {'action': 'created' if apply else 'would_create', 'source_id': source_id,
                'project_id': project_id, 'folder_id': folder_id, 'ordinal': ordinal}


def completion_checks(job, version, mapping, document, run):
    """Check the same identities required for certification; never repair them."""
    return {
        'job_complete': job.lifecycle_state == 'COMPLETE',
        'server_cleanup': job.cleanup_state == 'COMPLETE',
        'host_cleanup': job.host_cleanup_state == 'COMPLETE',
        'active_version': bool(version and version.lifecycle_state == 'ACTIVE' and
            version.source_document_id == job.source_document_id and
            version.document_id == job.document_id and version.chunk_set_id == job.chunk_set_id and
            version.parser_run_id == job.parser_run_id and version.search_cleanup_complete),
        'active_mapping': bool(mapping and mapping.deleted_at is None and
            mapping.project_id == job.project_id and mapping.source_id == job.source_id and
            mapping.document_id == job.document_id and mapping.active_source_version_id == job.version_id),
        'active_document': bool(document and str(document.status) == '1' and
            document.active_chunk_set_id and document.active_chunk_set_id == job.chunk_set_id),
        'parser_ready_clean': bool(run and run.doc_id == job.document_id and
            run.chunk_set_id == job.chunk_set_id and run.lifecycle in {'READY','READY_WITH_WARNING'} and
            run.raw_artifact_ref is None and run.staged_chunk_count > 0),
    }


def inspect_job(m, settings, job_id, expected_plaintext_sha256=None):
    identifier(job_id, 32)
    j = m.DocmindIngestionJob.get_by_id(job_id)
    v = m.DocmindSourceVersion.get_or_none(m.DocmindSourceVersion.id == j.version_id)
    mapping = m.DocmindSourceDocument.get_or_none(m.DocmindSourceDocument.id == j.source_document_id)
    doc = m.Document.get_or_none(m.Document.id == j.document_id)
    run = m.ParserRun.get_or_none(m.ParserRun.id == j.parser_run_id)
    checks = completion_checks(j, v, mapping, doc, run)
    count = None
    if all(checks.values()):
        from common.doc_store.doc_store_base import OrderByExpr
        from api.apps.services.docmind_ingestion_runtime import _index_name
        project = m.DocmindProject.get_by_id(j.project_id)
        result = settings.docStoreConn.search(['id'], [], {
            'doc_id': j.document_id, 'parse_run_id': j.parser_run_id, 'chunk_set_id': j.chunk_set_id,
        }, [], OrderByExpr(), 0, 1, _index_name(project.tenant_id), [doc.kb_id])
        count = int(settings.docStoreConn.get_total(result))
    checks['es_count_matches'] = bool(count is not None and count > 0 and run and count == run.staged_chunk_count)
    if expected_plaintext_sha256 is not None:
        if not re.fullmatch('[0-9a-fA-F]{64}', expected_plaintext_sha256):
            raise ValueError('INVALID_PLAINTEXT_SHA256')
        checks['plaintext_matches_fixture'] = j.plaintext_sha256 == expected_plaintext_sha256.lower()
    return {'job_id': j.id, 'document_id': j.document_id, 'state': j.lifecycle_state,
            'error_code': j.error_code, 'indexed_chunks': count, 'checks': checks,
            'passed': all(checks.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('projects', help='Read project IDs, dataset IDs and source counts')
    reg = sub.add_parser('register-source', help='Add source/folder only; dry run unless --apply')
    reg.add_argument('--project', required=True)
    reg.add_argument('--source', required=True)
    reg.add_argument('--display-name', required=True)
    reg.add_argument('--apply', action='store_true')
    jobs = sub.add_parser('jobs', help='Read at most 20 recent jobs in one source')
    jobs.add_argument('--source', required=True)
    check = sub.add_parser('check-job', help='Read DB/ES and fail unless fully complete and clean')
    check.add_argument('--job', required=True)
    check.add_argument('--expected-plaintext-sha256')
    args = parser.parse_args()
    from common import settings
    settings.init_settings()
    from api.db import db_models as m
    if args.command == 'projects':
        result = [{'project_id':p.id,'dataset_id':p.dataset_id,'mode':p.catalog_source_mode,
                   'source_count':m.DocmindSource.select().where(m.DocmindSource.project_id==p.id).count()}
                  for p in m.DocmindProject.select()]
    elif args.command == 'register-source':
        result = register_source(m,args.project,args.source,args.display_name,args.apply)
    elif args.command == 'jobs':
        identifier(args.source)
        result = [{'job_id':j.id,'state':j.lifecycle_state,'server_cleanup':j.cleanup_state,
                   'host_cleanup':j.host_cleanup_state,'error_code':j.error_code}
                  for j in m.DocmindIngestionJob.select().where(m.DocmindIngestionJob.source_id==args.source)
                  .order_by(m.DocmindIngestionJob.create_time.desc()).limit(20)]
    else:
        result = inspect_job(m,settings,args.job,args.expected_plaintext_sha256)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if args.command != 'check-job' or result['passed'] else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # Never print DB connection strings, config dumps or arbitrary server errors.
        known = {'INVALID_IDENTIFIER','INVALID_DISPLAY_NAME','DATABASE_PROJECT_REQUIRED',
                 'SOURCE_CONFLICT_EXISTING_PRESERVED','FOLDER_CONFLICT_EXISTING_PRESERVED',
                 'INVALID_PLAINTEXT_SHA256'}
        print(json.dumps({'error':str(exc) if str(exc) in known else type(exc).__name__}),file=sys.stderr)
        sys.exit(1)
