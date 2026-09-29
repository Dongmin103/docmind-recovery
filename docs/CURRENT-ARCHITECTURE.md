# 현재 코드 구조와 목표 구조

## 현재 상태

app/는 2026-09-21 커스텀 소스 스냅샷에서 시작했으며, 현재 개발 브랜치는 제품 경로의 OpenViking route/catalog generation/publish와 배포 의존성을 제거하고 공통 scope/C128 검색으로 전환했다. 원본 HEAD는 432dd7e6f98796b875beb41dd1abf2ced3ad8d5b이며 실제 환경설정·개인키·문서 데이터는 포함하지 않는다. 이 checkout의 변경이 운영 이미지에 배포됐거나 운영 서비스가 중지됐다는 뜻은 아니다.

백업의 실행 설정은 Python API + MySQL + Elasticsearch + Redis 호환 Valkey, CPU였다. Docker 앱에는 커스텀 PDF/Office/HWP 처리 및 DocMind 화면 변경이 포함돼 있었다. 소스 기본값과 실행 이미지가 같다고 가정하지 말고 새 이미지로 빌드한다.

| 위치 | 역할 / 주의점 |
|---|---|
| api/apps/restful_apis/docmind_api.py | DocMind API, 로그인·공용 workspace bootstrap 진입점 |
| api/apps/services/docmind_* | 공통 scope 검색·등록·폴더·경로와 계층 draft 상태 |
| api/db/services/{file,document,task,knowledgebase}_service.py | 문서 등록·파싱 작업·저장소·색인 관리. UI 제거와 별개로 필요 |
| rag/svr/task_executor.py | 파싱/청킹/색인 작업, 큐·재시도. 메모리/그래프/agent 공통 의존 포함 |
| rag/app, deepdoc, rag/parser_platform | 문서 청킹과 파서 연결. 기존 청킹/BGE-M3를 이번 작업에서 교체하지 않음 |
| parser_services/docling_pdf, docling_office, surya, rhwp | 커스텀 PDF/Office/OCR/HWP sidecar의 소스와 Containerfile |
| rag/llm, api/db/services/llm_service.py | 모델 어댑터와 설정. Jina reranker와 답변 LLM은 서로 다른 역할 |
| web/src/pages/docmind | DocMind 문서 선택·검색 화면 |
| web/src/pages/chunk, web/src/components/document-preview | 청크 목록과 원문 미리보기 공통 컴포넌트 |
| common/settings.py | DB/저장소/검색기와 메모리·그래프 초기화. 미사용 패키지 삭제 전 정리 |
| docker/ | Linux 배포 구성과 격리된 Windows 개발 Compose·env 예제·entrypoint |
| internal/, cmd/ | Go 대체 실행/파서 경로. Python 배포라는 이유만으로 일괄 삭제하지 않음 |

청크 텍스트는 ES에서 조회한다. 일반 업로드와 기존 청크 이미지 등의 객체 저장소 경로는 남아 있으므로 MinIO 서비스를 전체 제거한 상태는 아니다.

2026-09-28 source 문서의 원문 미리보기에는 별도 임시 경로를 구현했다. 명시적인
원문 보기 클릭 → 인증·원본 버전 확인 → Windows 임시 복호화 → RAM 기반 tmpfs의
형식별 뷰어 전달 → 닫기/만료 후 정리를 사용하며 원문을 MinIO에 저장하지 않는다.
PDF/DOCX/PPTX/XLS(X)는 직접 표시하고 DOC/PPT만 각각 DOCX/PPTX로 처리한다.
HWP(X)는 필요한 페이지의 SVG를 생성한다. 실제 DOC의 복호화·표시·전체 스크롤·
정리를 개발 환경에서 확인했다. 형식별 검증 범위와 남은 제한은
[미리보기 검증 보고서](TEMPORARY-PREVIEW-VALIDATION-2026-09-28.md),
실행 구성은 [미리보기 구현 안내](TEMPORARY-ORIGINAL-PREVIEW.md)를 따른다.

## 목표

```text
Windows All-in-One 암호화 원본 (세 루트)
  → Windows 감지/복호화 실행부 (임시 출력)
  → Docker DocMind 파서 → 기존 청커/BGE-M3 → ES
  → 임시 평문 및 파생물 정리

사용자 전체/폴더/문서 선택
  → 같은 범위로 BM25 + dense → RRF → C128
  → 청크별 앞 2400자 → Jina v3.5 → Top5 → LLM 답변·근거
  → 사용자별 대화 30일 보관

원문 클릭 → 임시 복호화/변환 → 좌측 원문 + 우측 청크 → 임시자료 정리
             (구현 중 가능 조건과 속도를 검증할 경로)
```

원본 저장 경로/표시 경로는 PRD 2절을 따른다. 초기 전체 범위는 등록된 세 루트이며 임의 Windows 드라이브 전체가 아니다. 개인 대화 이력은 공용 검색 scope와 구분한다.

## 배포 전 반드시 확인할 값

- 제품 Compose와 env 예제에는 제거된 서비스가 없고 Jina v3.5가 기본값이다. Linux 모델 절대 경로와 일부 고정 container_name은 대상 환경에 맞게 검증해야 한다.
- 답변 모델 환경 기본값은 qwen3.6-plus 계열로 남아 있으나 실제 운영 DB 모델 바인딩을 확인한 것은 아니다. Jina v3.5는 답변 생성 모델이 아니다.
- 공용 세션 resolve_user 경로는 한 사용자로 연결한다. 30일 개인 대화를 구현하면서 실제 로그인 소유자를 보존해야 한다.
- 원본 파싱 및 이미지 저장은 기존 STORAGE_IMPL 경로에 의존한다. 임시 입력과 조회 권한을 먼저 구현한다.
- Windows CLI 및 클라우드 작업 API는 아직 연계 구현·실행 검증 전이다. 제품 미지원 답변을 무시하거나 새 플래그를 추측하지 않는다.

제품 경로 제거 검증과 운영 전환 절차는 [OpenViking 제품 경로 제거 및 전환 런북](OPENVIKING-CUTOVER.md)을 따른다.
