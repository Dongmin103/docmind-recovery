from __future__ import annotations

from pathlib import Path

from api.db import db_models


def test_parser_run_additive_model_contract() -> None:
    assert db_models.Document.active_chunk_set_id.null is True
    assert db_models.Document.active_chunk_set_id.index is True
    assert db_models.Task.parse_run_id.null is True
    assert db_models.Task.chunk_set_id.null is True

    fields = db_models.ParserRun._meta.fields
    required = {
        "id",
        "doc_id",
        "chunk_set_id",
        "idempotency_key",
        "source_hash",
        "source_format",
        "source_fingerprint",
        "config_fingerprint",
        "parser_fingerprint",
        "parser_name",
        "parser_version",
        "model_version",
        "backend",
        "schema_version",
        "lifecycle",
        "warnings",
        "error_code",
        "raw_artifact_ref",
        "expected_task_count",
        "completed_task_count",
        "failed_task_count",
        "expected_page_count",
        "completed_page_count",
        "reused_page_count",
        "failed_page_count",
        "staged_chunk_count",
        "staged_token_count",
        "activated_at",
        "retained_until",
        "retained_from_lifecycle",
    }
    assert required <= fields.keys()
    assert db_models.ParserRun._meta.table_name == "parser_run"
    assert fields["chunk_set_id"].unique is True
    assert fields["idempotency_key"].unique is True


def test_existing_database_migration_is_additive() -> None:
    source = Path(db_models.__file__).read_text(encoding="utf-8")
    assert 'alter_db_add_column(migrator, "document", "active_chunk_set_id"' in source
    assert 'alter_db_add_column(migrator, "task", "parse_run_id"' in source
    assert 'alter_db_add_column(migrator, "task", "chunk_set_id"' in source
    assert 'alter_db_add_column(migrator, "parser_run", "retained_from_lifecycle"' in source
    assert 'alter_db_rename_column(migrator, "document", "active_chunk_set_id"' not in source
    assert 'drop_column("document", "active_chunk_set_id"' not in source
