from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.db.db_models import DocmindProject, File, File2Document
from api.db.services import docmind_document_path_service as service


@pytest.fixture
def source_tree(monkeypatch):
    models = [DocmindProject, File, File2Document]
    database = SqliteDatabase(":memory:")
    with database.bind_ctx(models):
        database.create_tables(models)
        monkeypatch.setattr(service.KnowledgebaseService, "get_by_id", lambda _: (True, SimpleNamespace(tenant_id="owner")))
        DocmindProject.create(id="project", tenant_id="owner", dataset_id="dataset", source_root_file_id="root")

        def node(file_id, name, parent="root", tenant="owner", kind="file", doc_id=None):
            File.create(id=file_id, name=name, parent_id=parent, tenant_id=tenant, created_by=tenant, type=kind)
            if doc_id:
                File2Document.create(id=f"link-{file_id}", file_id=file_id, document_id=doc_id)

        node("root", "ai-ref", parent="root", kind="folder")
        yield node
    database.close()


def test_resolves_nested_and_root_documents_with_source_root_name(source_tree):
    source_tree("gmp", "GMP", kind="folder")
    source_tree("quality", "품질관리", parent="gmp", kind="folder")
    source_tree("sop", "SOP.pdf", parent="quality", doc_id="nested")
    source_tree("readme", "README.pdf", doc_id="direct")
    assert service.document_relative_paths("dataset", {"nested", "direct", "missing"}) == {
        "nested": "ai-ref/GMP/품질관리/SOP.pdf", "direct": "ai-ref/README.pdf",
    }


@pytest.mark.parametrize("case", ["outside", "foreign", "cycle", "missing-parent", "absolute", "traversal", "folder-leaf", "file-parent"])
def test_omits_invalid_or_out_of_scope_links(source_tree, case):
    if case == "outside":
        source_tree("parent", "Outside", parent="parent", kind="folder")
        source_tree("leaf", "secret.pdf", parent="parent", doc_id="doc")
    elif case == "foreign":
        source_tree("parent", "Foreign", tenant="other", kind="folder")
        source_tree("leaf", "secret.pdf", parent="parent", doc_id="doc")
    elif case == "cycle":
        source_tree("parent", "Cycle", parent="leaf", kind="folder")
        source_tree("leaf", "secret.pdf", parent="parent", doc_id="doc")
    elif case == "missing-parent":
        source_tree("leaf", "secret.pdf", parent="missing", doc_id="doc")
    elif case == "file-parent":
        source_tree("parent", "not-a-folder.pdf")
        source_tree("leaf", "secret.pdf", parent="parent", doc_id="doc")
    else:
        source_tree("leaf", "/srv/secret.pdf" if case == "absolute" else "../secret.pdf" if case == "traversal" else "Folder", kind="folder" if case == "folder-leaf" else "file", doc_id="doc")
    assert service.document_relative_paths("dataset", {"doc"}) == {}


def test_missing_project_or_source_root_is_optional(source_tree):
    DocmindProject.update(source_root_file_id=None).execute()
    assert service.document_relative_paths("dataset", {"doc"}) == {}
    assert service.document_relative_paths("unknown", {"doc"}) == {}


def test_filters_to_requested_documents_and_skips_invalid_alternate_links(source_tree):
    source_tree("foreign", "foreign.pdf", tenant="other", doc_id="doc")
    source_tree("valid", "valid.pdf", doc_id="doc")
    source_tree("unrequested", "hidden.pdf", doc_id="hidden")
    assert service.document_relative_paths("dataset", {"doc"}) == {"doc": "ai-ref/valid.pdf"}


@pytest.mark.parametrize("name", ["", ".", "..", "/srv/docmind", "C:\\docmind", "bad\x00name"])
def test_omits_unsafe_source_root_names(source_tree, name):
    File.update(name=name).where(File.id == "root").execute()
    source_tree("leaf", "document.pdf", doc_id="doc")
    assert service.document_relative_paths("dataset", {"doc"}) == {}
