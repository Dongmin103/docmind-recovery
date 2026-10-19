"""Idempotently register this installation's models, workspace and sources."""
import hashlib
import json
import os
from pathlib import Path


def stable(*parts):
    return hashlib.sha256('\x1f'.join(parts).encode()).hexdigest()[:32]


def main():
    os.environ['RAGFLOW_SECRET_KEY'] = Path('/run/secrets/session-key').read_text().strip()
    from common import settings
    settings.init_settings()
    from api.apps.services.docmind_bootstrap_service import bootstrap_shared_workspace
    from api.db.db_models import (
        DB,
        DocmindFolder,
        DocmindProject,
        DocmindSource,
        Knowledgebase,
        Tenant,
        TenantModel,
        TenantModelInstance,
        TenantModelProvider,
    )
    from api.db.init_data import init_docmind_shared_user
    secrets = Path('/run/secrets')
    user_id = init_docmind_shared_user()
    if not user_id:
        raise RuntimeError('Shared installation identity was not created')
    definitions = [
        ('OpenAI-API-Compatible', 'GTE', 'gte-multilingual-base', 2, 'http://embedding:8080/v1', 'embedding-key', 8192),
        ('Jina', 'jina', 'jina-reranker-v3.5', 16, '', 'jina-key', 8192),
    ]
    with DB.atomic():
        for provider_name, instance_name, model_name, model_type, base_url, key_file, max_tokens in definitions:
            provider, _ = TenantModelProvider.get_or_create(id=stable(user_id, provider_name), defaults={'provider_name': provider_name, 'tenant_id': user_id})
            if provider.tenant_id != user_id or provider.provider_name != provider_name:
                raise ValueError('Provider ownership mismatch')
            instance, _ = TenantModelInstance.get_or_create(id=stable(provider.id, instance_name), defaults={'provider_id': provider.id, 'instance_name': instance_name, 'api_key': (secrets/key_file).read_text().strip(), 'extra': json.dumps({'base_url': base_url})})
            if instance.provider_id != provider.id or instance.instance_name != instance_name:
                raise ValueError('Model instance ownership mismatch')
            TenantModel.get_or_create(id=stable(instance.id, model_name), defaults={'provider_id': provider.id, 'instance_id': instance.id, 'model_name': model_name, 'model_type': model_type, 'extra': json.dumps({'max_tokens': max_tokens})})
        embedding = 'gte-multilingual-base@GTE@OpenAI-API-Compatible'
        Tenant.update(embd_id=embedding, rerank_id='jina-reranker-v3.5@jina@Jina').where(Tenant.id == user_id).execute()
    # Service methods manage their own connection contexts; call them after the
    # registry transaction has closed rather than nesting those contexts in it.
    workspace = bootstrap_shared_workspace(user_id)
    project = DocmindProject.get_by_id(workspace['project_id'])
    Knowledgebase.update(embd_id=embedding).where(Knowledgebase.id == project.dataset_id).execute()
    sources = json.loads(Path('/run/installation/sources.json').read_text(encoding='utf-8-sig'))
    with DB.atomic():
        for source in sources:
            if set(source) != {'source_id', 'display_name'}:
                raise ValueError('Only logical source metadata is accepted')
            folder, _ = DocmindFolder.get_or_create(id=stable(project.id, source['source_id'], 'folder'), defaults={'project_id': project.id, 'slug': source['source_id'], 'display_name': source['display_name'], 'ordinal': 0, 'enabled': True})
            if folder.project_id != project.id:
                raise ValueError('Source folder ownership mismatch')
            row, _ = DocmindSource.get_or_create(id=source['source_id'], defaults={'project_id': project.id, 'display_name': source['display_name'], 'default_folder_id': folder.id, 'enabled': True})
            if row.project_id != project.id or row.default_folder_id != folder.id:
                raise ValueError('Source ownership mismatch; existing registration preserved')
    print(json.dumps({'initialized': True, 'models': len(definitions), 'sources': len(sources), 'embedding_dimensions': 768}))


if __name__ == '__main__':
    main()
