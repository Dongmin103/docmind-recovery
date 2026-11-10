# Engine handoff helpers

Use [the Korean runbook](../../docs/engine-handoff/RUNBOOK.md). These tools do not
replace the Windows ingestion worker, initialize models, change runtime quotas,
delete indices, or configure networking.

- `engine.py projects`: read project identifiers.
- `engine.py register-source ...`: preview an additive source/folder registration;
  writes only with `--apply`. Existing conflicting/disabled sources are rejected.
- `engine.py jobs --source ID`: read up to 20 recent jobs, without document text.
- `engine.py check-job --job ID [--expected-plaintext-sha256 HASH]`: read DB and ES;
  exit 0 only when completion, active identities, cleanup and chunk counts agree.
  Exit 2 means incomplete/mismatch; exit 1 means execution/configuration error.
- `make_sample.py --output /new/path/sample.docx`: generate a synthetic plaintext
  DOCX using the Python standard library. Never overwrites an existing file.
  Encryption/upload through the source product is a separate prerequisite.

Run `engine.py` inside the configured API container, at `/ragflow` with
`PYTHONPATH=/ragflow`, using `/ragflow/.venv/bin/python`. No dependency download is
needed in that image. It uses the deployed Peewee schema and configured stores.
Do not run it against an arbitrary database. `make_sample.py` needs Python 3 and
the standard library only.

Tests use SQLite in memory and selected model definitions parsed from the actual
application file. They do not import the application bootstrap or touch the live
database. The test runtime must have the repository's Peewee dependency installed.

```bash
DOCMIND_APP_ROOT=/ragflow /ragflow/.venv/bin/python -m unittest discover -s /tmp/docmind-handoff -p test_handoff.py -v
```

On 2026-11-10 all 8 tests passed in the AVDC API Python environment. The production
database was used only for a registration dry run and a read-only check of an
existing completed job; source/folder/job counts were unchanged. A new encrypted
synthetic end-to-end run and a clean-server installation were not executed.
