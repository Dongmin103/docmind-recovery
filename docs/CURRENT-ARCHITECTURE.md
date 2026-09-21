# 현재 코드 구조와 목표 구조

## 현재 상태

app/는 2026-09-21 변경 전 소스 스냅샷이다. 원본 HEAD는 432dd7e6f98796b875beb41dd1abf2ced3ad8d5b이며 백업 시점의 미커밋 변경을 포함한다. 원본 Git 이력은 새 브랜치에 병합하지 않았다. 실제 환경설정·개인키·문서 데이터는 제외하고 공개용으로 정리했다. 이후 운영 서버 변경 여부는 이 인수인계에서 확인하지 않았다.

백업의 실행 설정은 Python API + MySQL + Elasticsearch + Redis 호환 Valkey, CPU였다. Docker 앱에는 커스텀 PDF/Office/HWP 처리 및 DocMind 화면 변경이 포함돼 있었다. 소스 기본값과 실행 이미지가 같다고 가정하지 말고 새 이미지로 빌드한다.

| 위치 | 역할 / 주의점 |
|---|---|
| api/apps/restful_apis/docmind_api.py | DocMind API, 로그인·공용 workspace bootstrap 진입점 |
| api/apps/services/docmind_* | 검색·카탈로그·등록·폴더·경로·generation/publish. OpenViking 제거 주요 영역 |
| api/db/services/{file,document,task,knowledgebase}_service.py | 문서 등록·파싱 작업·저장소·색인 관리. UI 제거와 별개로 필요 |
| rag/svr/task_executor.py | 파싱/청킹/색인 작업, 큐·재시도. 메모리/그래프/agent 공통 의존 포함 |
| rag/app, deepdoc, rag/parser_platform | 문서 청킹과 파서 연결. 기존 청킹/BGE-M3를 이번 작업에서 교체하지 않음 |
| parser_services/docling_pdf, docling_office, surya, rhwp | 커스텀 PDF/Office/OCR/HWP sidecar의 소스와 Containerfile |
| rag/llm, api/db/services/llm_service.py | 모델 어댑터와 설정. Jina reranker와 답변 LLM은 서로 다른 역할 |
| web/src/pages/docmind | DocMind 문서 선택·검색 화면 |
| web/src/pages/chunk, web/src/components/document-preview | 청크 목록과 원문 미리보기 공통 컴포넌트 |
| common/settings.py | DB/저장소/검색기와 메모리·그래프 초기화. 미사용 패키지 삭제 전 정리 |
| docker/ | 기존 Linux 배포 Compose·env 예제·entrypoint. Windows 실행용으로 아직 전환되지 않음 |
| internal/, cmd/ | Go 대체 실행/파서 경로. Python 배포라는 이유만으로 일괄 삭제하지 않음 |

현재 청크 텍스트는 ES, 원본·청크 이미지는 객체 저장소를 통해 조회한다. 현재 파일 업로드는 MinIO에 원본을 저장하고 워커가 읽는다. 새 요구의 임시 입력·평문 삭제로 치환하기 전 MinIO 서비스를 먼저 끄면 안 된다.

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

## 배포 시 반드시 수정할 기존값

- docker-compose-docmind.yml은 OpenViking 의존, Jina v3 기본값, Linux 모델 경로와 고정 container_name을 포함한다. 목표 설정으로 변경해야 한다.
- 답변 모델 환경 기본값은 qwen3.6-plus 계열로 남아 있으나 실제 운영 DB 모델 바인딩을 확인한 것은 아니다. Jina v3.5는 답변 생성 모델이 아니다.
- 공용 세션 resolve_user 경로는 한 사용자로 연결한다. 30일 개인 대화를 구현하면서 실제 로그인 소유자를 보존해야 한다.
- 원본 파싱 및 이미지 저장은 기존 STORAGE_IMPL 경로에 의존한다. 임시 입력과 조회 권한을 먼저 구현한다.
- Windows CLI 및 클라우드 작업 API는 아직 연계 구현·실행 검증 전이다. 제품 미지원 답변을 무시하거나 새 플래그를 추측하지 않는다.
