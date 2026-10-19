"""Bind fresh installation secrets and login keys before starting DOCMIND."""
import os
import re
from pathlib import Path

import yaml
from Cryptodome.PublicKey import RSA


def start():
    secrets = Path('/run/secrets')
    os.environ['RAGFLOW_SECRET_KEY'] = (secrets/'session-key').read_text().strip()
    if len(os.environ['RAGFLOW_SECRET_KEY']) < 32:
        raise ValueError('Invalid installation session key')
    conf = Path('/ragflow/conf')
    conf.mkdir(exist_ok=True)
    private, public = conf/'private.pem', conf/'public.pem'
    if private.exists() != public.exists():
        raise ValueError('Incomplete login key pair; no keys replaced')
    if not private.exists():
        key = RSA.generate(2048)
        private.write_bytes(key.export_key())
        private.chmod(0o600)
        public.write_bytes(key.publickey().export_key())
    key = RSA.import_key(private.read_bytes())
    if key.publickey().export_key() != RSA.import_key(public.read_bytes()).export_key():
        raise ValueError('Login key mismatch')
    # Bind this installation's public key in the prebuilt bundle; no npm build
    # and no private key transfer is needed on the destination Windows server.
    pattern = r'-----BEGIN PUBLIC KEY-----[A-Za-z0-9+/=\r\n]+-----END PUBLIC KEY-----'
    inline = public.read_text().replace('\n', '').replace('\r', '')
    for asset in Path('/ragflow/web/dist').rglob('*.js'):
        text = asset.read_text(encoding='utf-8')
        updated = re.sub(pattern, lambda _: inline, text)
        if updated != text:
            asset.write_text(updated, encoding='utf-8')
    config = {
        'ragflow': {'host': '0.0.0.0', 'http_port': 9380},
        'mysql': {'name': 'docmind', 'user': 'root', 'password': os.environ['MYSQL_PASSWORD'], 'host': 'mysql', 'port': 3306, 'max_connections': 100, 'stale_timeout': 300, 'max_allowed_packet': 1073741824},
        'es': {'hosts': 'http://es:9200', 'username': 'elastic', 'password': os.environ['ELASTIC_PASSWORD']},
        'redis': {'host': 'redis:6379', 'db': 1, 'password': os.environ['REDIS_PASSWORD']},
        'minio': {'host': 'minio:9000', 'user': 'docmind', 'password': os.environ['MINIO_PASSWORD']},
        'user_default_llm': {'default_models': {'embedding_model': {'name': 'gte-multilingual-base@GTE@OpenAI-API-Compatible'}}},
    }
    (conf/'service_conf.yaml.template').write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    os.execv('/ragflow/entrypoint.sh', ['/ragflow/entrypoint.sh'])


if __name__ == '__main__':
    start()
