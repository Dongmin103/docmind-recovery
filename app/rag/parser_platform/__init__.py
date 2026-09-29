"""Search parser contracts and Kordoc integration."""

from rag.parser_platform.active_scope import (
    ActiveChunkScopeResolver,
    ActiveChunkSetFilterExpr,
    ActiveScopeCache,
    ActiveScopeMetrics,
    ActiveScopeResolution,
    DocumentScopeRow,
    ElasticsearchActiveChunkFilterTranslator,
    InfinityActiveChunkFilterTranslator,
    OceanBaseActiveChunkFilterTranslator,
    OpenSearchActiveChunkFilterTranslator,
    active_chunk_filter_translator,
    translator_snapshot,
)
from rag.parser_platform.artifact_repository import ParserArtifactRepository
from rag.parser_platform.canonical import canonical_json, canonical_sha256, canonicalize_provenance
from rag.parser_platform.chunk_adapter import CommonToStandardChunkAdapter
from rag.parser_platform.chunk_sets import (
    ActivationResult,
    ChunkSetActivationCoordinator,
    ChunkSetFinalizationRequest,
    CleanupResult,
    StagingChunkTagger,
)
from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.coordinator import ParserCoordinator, ParserRunRequest, PreparedParserRun, validate_transition
from rag.parser_platform.dispatch import FormatDispatcher, ParserSelection, SourceDescriptor, document_source_format
from rag.parser_platform.errors import ParserPlatformError
from rag.parser_platform.gates import enforce_prequeue_gate
from rag.parser_platform.schemas import (
    ActivationMetadata,
    BlockType,
    DocxProvenance,
    HwpProvenance,
    HwpTableCell,
    OcrRequirement,
    ParsedBlock,
    ParsedDocument,
    ParserRunStatus,
    PdfProvenance,
    PptxProvenance,
    Provenance,
    SourceFormat,
    XlsxProvenance,
)
from rag.parser_platform.stable_id import make_stable_block_id
from rag.parser_platform.standard_bridge import ParserPlatformStandardBridge

__all__ = [name for name in globals() if not name.startswith("_")]