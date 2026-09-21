from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParserPlatformErrorInfo:
    code: str
    message_ko: str
    retryable: bool


class ParserPlatformError(RuntimeError):
    def __init__(self, info: ParserPlatformErrorInfo, *, detail: str | None = None):
        self.info = info
        self.detail = detail
        super().__init__(f"{info.code}: {detail or info.message_ko}")

    @property
    def code(self) -> str:
        return self.info.code

    @property
    def retryable(self) -> bool:
        return self.info.retryable

    @property
    def user_message(self) -> str:
        return self.info.message_ko


ERRORS = {
    "PARSER_PLATFORM_DISABLED": ParserPlatformErrorInfo(
        "PARSER_PLATFORM_DISABLED",
        "새 문서 파서 기능이 비활성화되어 있습니다.",
        False,
    ),
    "PARSER_PLATFORM_NOT_READY": ParserPlatformErrorInfo(
        "PARSER_PLATFORM_NOT_READY",
        "새 문서 파서 연결이 아직 준비되지 않았습니다.",
        True,
    ),
    "PARSER_PLATFORM_TE_RUN_MODE_UNSUPPORTED": ParserPlatformErrorInfo(
        "PARSER_PLATFORM_TE_RUN_MODE_UNSUPPORTED",
        "새 문서 파서는 TE_RUN_MODE=0에서만 사용할 수 있습니다.",
        False,
    ),
    "PARSER_PLATFORM_DATAFLOW_UNSUPPORTED": ParserPlatformErrorInfo(
        "PARSER_PLATFORM_DATAFLOW_UNSUPPORTED",
        "새 문서 파서는 현재 표준 인덱싱 경로에서만 사용할 수 있습니다.",
        False,
    ),
    "PARSER_PLATFORM_HWP_DISABLED": ParserPlatformErrorInfo(
        "PARSER_PLATFORM_HWP_DISABLED", "HWP/HWPX 문서 파서가 비활성화되어 있습니다.", False
    ),
    "PARSER_PLATFORM_HWP_NOT_READY": ParserPlatformErrorInfo(
        "PARSER_PLATFORM_HWP_NOT_READY", "HWP/HWPX 문서 파서 연결이 아직 준비되지 않았습니다.", True
    ),
    "PARSER_PLATFORM_HWP_CANARY_BOOTSTRAP_REQUIRED": ParserPlatformErrorInfo(
        "PARSER_PLATFORM_HWP_CANARY_BOOTSTRAP_REQUIRED",
        "먼저 RAGFlow upload-only로 문서를 등록해 ID를 확인한 뒤 canary 설정을 적용하고 수동 분석을 시작하세요.",
        False,
    ),
    "PARSER_PLATFORM_HWP_CANARY_MISMATCH": ParserPlatformErrorInfo(
        "PARSER_PLATFORM_HWP_CANARY_MISMATCH", "이 문서는 현재 HWP/HWPX canary 허용 대상이 아닙니다.", False
    ),
    "PARSER_PLATFORM_HWP_PROMOTION_INVALID": ParserPlatformErrorInfo(
        "PARSER_PLATFORM_HWP_PROMOTION_INVALID", "HWP/HWPX global 전환 승인 증거가 유효하지 않습니다.", False
    ),
    "PARSER_SOURCE_FORMAT_UNSUPPORTED": ParserPlatformErrorInfo(
        "PARSER_SOURCE_FORMAT_UNSUPPORTED",
        "지원하지 않는 문서 형식입니다.",
        False,
    ),
    "PARSER_SOURCE_TYPE_MISMATCH": ParserPlatformErrorInfo(
        "PARSER_SOURCE_TYPE_MISMATCH",
        "파일 확장자와 실제 문서 형식이 일치하지 않습니다.",
        False,
    ),
    "PARSER_SOURCE_SIZE_LIMIT_EXCEEDED": ParserPlatformErrorInfo(
        "PARSER_SOURCE_SIZE_LIMIT_EXCEEDED",
        "문서 크기가 파서 안전 한도를 초과했습니다.",
        False,
    ),
    "PARSER_OOXML_INVALID": ParserPlatformErrorInfo(
        "PARSER_OOXML_INVALID",
        "Office 문서 구조가 손상되었거나 유효하지 않습니다.",
        False,
    ),
    "PARSER_OOXML_ARCHIVE_LIMIT_EXCEEDED": ParserPlatformErrorInfo(
        "PARSER_OOXML_ARCHIVE_LIMIT_EXCEEDED",
        "Office 문서 압축 구조가 안전 한도를 초과했습니다.",
        False,
    ),
    "PARSER_HWP_INVALID": ParserPlatformErrorInfo(
        "PARSER_HWP_INVALID", "HWP/HWPX 문서가 손상되었거나 유효하지 않습니다.", False
    ),
    "PARSER_RHWP_UNAVAILABLE": ParserPlatformErrorInfo(
        "PARSER_RHWP_UNAVAILABLE", "HWP/HWPX 분석 서비스에 연결할 수 없습니다.", True
    ),
    "PARSER_RHWP_TIMEOUT": ParserPlatformErrorInfo(
        "PARSER_RHWP_TIMEOUT", "HWP/HWPX 분석 시간이 제한을 초과했습니다.", True
    ),
    "PARSER_RHWP_INVALID_OUTPUT": ParserPlatformErrorInfo(
        "PARSER_RHWP_INVALID_OUTPUT", "HWP/HWPX 분석 결과가 비어 있거나 완전하지 않습니다.", False
    ),
    "PARSER_SURYA_UNAVAILABLE": ParserPlatformErrorInfo(
        "PARSER_SURYA_UNAVAILABLE",
        "Surya PDF 분석 서비스에 연결할 수 없습니다.",
        True,
    ),
    "PARSER_SURYA_TIMEOUT": ParserPlatformErrorInfo(
        "PARSER_SURYA_TIMEOUT",
        "Surya PDF 분석 시간이 제한을 초과했습니다.",
        True,
    ),
    "PARSER_SURYA_INVALID_OUTPUT": ParserPlatformErrorInfo(
        "PARSER_SURYA_INVALID_OUTPUT",
        "Surya PDF 분석 결과가 완전하지 않습니다.",
        True,
    ),
    "PARSER_SURYA_PAGE_LIMIT_EXCEEDED": ParserPlatformErrorInfo(
        "PARSER_SURYA_PAGE_LIMIT_EXCEEDED",
        "Surya 자동 분석 허용 페이지 수를 초과했습니다. 수동 검토가 필요합니다.",
        False,
    ),
    "PARSER_DOCLING_UNAVAILABLE": ParserPlatformErrorInfo(
        "PARSER_DOCLING_UNAVAILABLE",
        "Docling Office 분석 서비스에 연결할 수 없습니다.",
        True,
    ),
    "PARSER_DOCLING_TIMEOUT": ParserPlatformErrorInfo(
        "PARSER_DOCLING_TIMEOUT",
        "Docling Office 분석 시간이 제한을 초과했습니다.",
        True,
    ),
    "PARSER_DOCLING_INVALID_OUTPUT": ParserPlatformErrorInfo(
        "PARSER_DOCLING_INVALID_OUTPUT",
        "Docling Office 분석 결과가 완전하지 않습니다.",
        True,
    ),
    "MEDIA_RENDER_UNSUPPORTED": ParserPlatformErrorInfo(
        "MEDIA_RENDER_UNSUPPORTED",
        "이 Office 이미지 형식은 안전하게 변환할 수 없습니다.",
        False,
    ),
    "MEDIA_RENDER_TIMEOUT": ParserPlatformErrorInfo(
        "MEDIA_RENDER_TIMEOUT",
        "Office 이미지 변환 시간이 제한을 초과했습니다.",
        True,
    ),
    "MEDIA_RENDER_LIMIT_EXCEEDED": ParserPlatformErrorInfo(
        "MEDIA_RENDER_LIMIT_EXCEEDED",
        "Office 이미지가 안전 변환 한도를 초과했습니다.",
        False,
    ),
    "MEDIA_EXTERNAL_REFERENCE_BLOCKED": ParserPlatformErrorInfo(
        "MEDIA_EXTERNAL_REFERENCE_BLOCKED",
        "외부 파일이나 링크를 참조하는 Office 이미지는 차단되었습니다.",
        False,
    ),
    "ACTIVE_CHUNK_SCOPE_KB_REQUIRED": ParserPlatformErrorInfo(
        "ACTIVE_CHUNK_SCOPE_KB_REQUIRED",
        "검색할 지식베이스 범위가 필요합니다.",
        False,
    ),
    "ACTIVE_CHUNK_SCOPE_ID_INVALID": ParserPlatformErrorInfo(
        "ACTIVE_CHUNK_SCOPE_ID_INVALID",
        "검색 범위 식별자 형식이 올바르지 않습니다.",
        False,
    ),
    "ACTIVE_CHUNK_SCOPE_TOO_LARGE": ParserPlatformErrorInfo(
        "ACTIVE_CHUNK_SCOPE_TOO_LARGE",
        "활성 문서 검색 범위가 안전 한도를 초과했습니다.",
        True,
    ),
    "ACTIVE_CHUNK_FILTER_UNSUPPORTED": ParserPlatformErrorInfo(
        "ACTIVE_CHUNK_FILTER_UNSUPPORTED",
        "현재 검색 저장소는 활성 청크 필터를 지원하지 않습니다.",
        False,
    ),
    "CHUNK_SET_ACTIVATION_INCOMPLETE": ParserPlatformErrorInfo(
        "CHUNK_SET_ACTIVATION_INCOMPLETE",
        "새 청크 묶음이 완전하지 않아 검색에 반영하지 않았습니다.",
        True,
    ),
    "CHUNK_SET_ACTIVATION_TRANSACTION_FAILED": ParserPlatformErrorInfo(
        "CHUNK_SET_ACTIVATION_TRANSACTION_FAILED",
        "새 청크 묶음을 검색에 반영하는 중 데이터베이스 오류가 발생했습니다.",
        True,
    ),
    "CHUNK_SET_ACTIVATION_CONFLICT": ParserPlatformErrorInfo(
        "CHUNK_SET_ACTIVATION_CONFLICT",
        "문서의 활성 청크 버전이 다른 작업에 의해 변경되었습니다.",
        True,
    ),
    "CHUNK_SET_ROLLBACK_TARGET_INVALID": ParserPlatformErrorInfo(
        "CHUNK_SET_ROLLBACK_TARGET_INVALID",
        "롤백할 수 있는 유효한 이전 청크 버전이 아닙니다.",
        False,
    ),
    "CHUNK_SET_STAGING_IDENTITY_MISMATCH": ParserPlatformErrorInfo(
        "CHUNK_SET_STAGING_IDENTITY_MISMATCH",
        "임시 청크의 문서 또는 실행 식별자가 일치하지 않습니다.",
        False,
    ),
    "PARSER_NORMALIZATION_FAILED": ParserPlatformErrorInfo(
        "PARSER_NORMALIZATION_FAILED",
        "문서 구조를 검색용 형식으로 정리하지 못했습니다.",
        True,
    ),
    "PARSER_MEDIA_OCR_FAILED": ParserPlatformErrorInfo(
        "PARSER_MEDIA_OCR_FAILED",
        "필수 Office 이미지의 글자를 읽지 못했습니다.",
        True,
    ),
    "SURYA_PDF_PAGE_LIMIT_EXCEEDED": ParserPlatformErrorInfo(
        "SURYA_PDF_PAGE_LIMIT_EXCEEDED",
        "PDF 페이지 수가 Surya 안전 한도를 초과했습니다.",
        False,
    ),
    "SURYA_PDF_DEADLINE_EXCEEDED": ParserPlatformErrorInfo(
        "SURYA_PDF_DEADLINE_EXCEEDED",
        "PDF 전체 분석 제한 시간이 초과되었습니다.",
        True,
    ),
    "SURYA_PDF_HEARTBEAT_TIMEOUT": ParserPlatformErrorInfo(
        "SURYA_PDF_HEARTBEAT_TIMEOUT",
        "PDF 분석 진행이 정해진 시간 동안 갱신되지 않았습니다.",
        True,
    ),
    "PARSER_RUN_TRANSITION_INVALID": ParserPlatformErrorInfo(
        "PARSER_RUN_TRANSITION_INVALID",
        "문서 파서 상태 전이가 올바르지 않습니다.",
        False,
    ),
}


def parser_error(code: str, *, detail: str | None = None) -> ParserPlatformError:
    return ParserPlatformError(ERRORS[code], detail=detail)
