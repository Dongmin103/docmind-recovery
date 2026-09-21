"""Read-only source-tree paths for already-authorized DocMind search results."""

from api.db import FileType
from api.db.db_models import DocmindProject, File, File2Document
from api.db.services.knowledgebase_service import KnowledgebaseService


def document_relative_paths(dataset_id: str, document_ids: set[str]) -> dict[str, str]:
    if not document_ids:
        return {}
    exists, dataset = KnowledgebaseService.get_by_id(dataset_id)
    if not exists or dataset is None:
        return {}
    project = DocmindProject.get_or_none(
        (DocmindProject.dataset_id == dataset_id) & (DocmindProject.tenant_id == dataset.tenant_id)
    )
    if project is None or not project.source_root_file_id:
        return {}

    files: dict[str, File | None] = {}

    def source_file(file_id: str) -> File | None:
        if file_id not in files:
            files[file_id] = File.get_or_none((File.id == file_id) & (File.tenant_id == project.tenant_id))
        return files[file_id]

    root = source_file(project.source_root_file_id)
    if root is None or root.type != FileType.FOLDER.value:
        return {}
    root_name = root.name
    if not root_name or root_name in {".", ".."} or any(character in root_name for character in "/\\:\x00"):
        return {}

    paths: dict[str, str] = {}
    links = File2Document.select().where(File2Document.document_id.in_(document_ids)).order_by(File2Document.id)
    for link in links:
        if link.document_id in paths:
            continue
        node = source_file(link.file_id)
        if node is None or node.type == FileType.FOLDER.value:
            continue
        names: list[str] = []
        seen: set[str] = set()
        while node is not None and node.id != root.id and node.id not in seen and len(seen) < 256:
            name = node.name
            if not name or name in {".", ".."} or any(character in name for character in "/\\:\x00"):
                break
            seen.add(node.id)
            names.append(name)
            node = source_file(node.parent_id)
            if node is not None and node.type != FileType.FOLDER.value:
                break
        else:
            if node is not None and node.id == root.id:
                paths[link.document_id] = "/".join([root_name, *reversed(names)])
    return paths
