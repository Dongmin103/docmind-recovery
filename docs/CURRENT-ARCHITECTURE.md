# 현재 코드 구조와 목표 구조

2026-11-10 AVDC에 PDF 제목·본문 묶음 청킹 v3를 적용했다. Kordoc과 기존 색인은 유지하며 이후 색인부터 적용한다. [배포·검증·복구 기록](AVDC-CHUNKING-DEPLOYMENT-2026-11-10.md)을 참조한다.

2026-11-06 AVDC CPU 운영 임베딩은 OpenVINO FP32로 전환됐다. Windows GPU 경로는
별개로 유지한다. [현재 AVDC 설정·검증 기록](AVDC-OPENVINO-DEPLOYMENT-2026-11-06.md)을 참조한다.

## 현재 상태

2026-10-15: 승인된 RAGFlow 기능 축소 버전 `dd967245`를 이미지 `docmind-ragflow:diet-dd967245-27ae58d8`로 배포했다. 별도 관리자·플러그인·외부 메시징·범용 앱·에이전트/워크플로·장기 메모리·그래프·MCP·범용 편집·미사용 공급자/대체 저장소/Go 실행 경로를 정리하고 DocMind 공통 기능은 보존했다. 같은 244개 원본의 비OCR 전체 재색인 1회는 마지막 정리/ACK까지 283.436초, 정상223개/12,460벡터, 추가 오류0이었다. 이전267.398초보다6.00% 느렸고 API cgroup 최고1.413GiB/OOM0, PC 전체 RAM 최고99%였다. 현재 배포/복구 manifest 위치와 시험 한계는 [최종 검증·배포 기록](FEATURE-DIET-2026-10-15.md)을 따른다. 아래 2026-10-14 기록은 이전 상태다.

2026-10-14 추가 정리 배포 완료: 옛 BGE의 `full` 자동 시작과 API 의존성을 제거하고, 선택하지 않은 검색·저장소 드라이버를 지연 로딩하며, 참조가 없는 프런트 파일 14개를 정리했다. 현재 API 이미지는 `docmind-ragflow:lazy-backends-20261014`(코드 `9447a9b`)다. WSL 연결 장애를 승인된 재시작으로 복구한 뒤 실제 검색·GPU 벡터 일치·원본 244개·정상 문서 223개/12,460벡터 보존과 워커 재개를 검증했다. 기존 파서/GPU 설정과 선택 OCR은 유지하며 추가 전체 색인 시간·RAM 절감량은 측정하지 않았다. [정리 범위와 배포 검증](UNUSED-FEATURE-CLEANUP-2026-10-14.md)을 참조한다.

2026-10-14 19시 추가: Windows 배포의 별도 관리자 API를 기본 중지·필요 시 실행으로 전환했다. 같은 기본 API 프로세스에서 관리자만 종료했을 때 cgroup 메모리 약 789MiB가 줄었다. 이후 같은 244개 원본의 비OCR 전체 재색인 1회는 267.398초, 정상 223개/12,460벡터, 새 오류 0으로 완료됐다. 이전 276.576초보다 3.32% 단축됐고 GPU 평균은 59.58%, RAM 최대 사용률은 98%였다. [시작·종료 명령과 검증 범위](ADMIN-API-ON-DEMAND.md)를 참조한다.

2026-10-14 18시 추가: API 재시작·옛 BGE 중지·1회 캐시 반환 후 전체 시험은 276.576초, 정상 223개/12,460벡터로 완료됐다. 관리자 API는 유지 중이다. RAM은 여전히 최대 99%여서 반복 안정성을 보장하지 않는다. 상세 조건은 [메모리 정리 후 실측](CLOUD-ONLY-CLEANUP-2026-10-14.md)을 참조한다.

2026-10-14 추가: Python 외부 커넥터 API·데이터 소스 화면과 별도 task executor/datasync를 제거하고 클라우드 인덱싱 공통 처리만 유지했다. 복호화 5초 대기를 작업 임대 예산으로 수정해 배포했으며, 244개 전체 1회 실측은 정상 223개/12,460벡터, 시작·정리 포함 315.2초였다. OCR 18개와 기존 형식 오류 3개는 색인 제외다. 같은 날 추가 1회 측정은 420.0초였으며 API 요청 시간 초과 1건으로 측정 중에는 222개만 완료했다. 서비스 복구 후 자동 재처리로 현재는 223개/12,460벡터이며 추가 재처리는 측정값에서 제외했다. 변경·부하·검증 한계는 [정리 및 실측 기록](CLOUD-ONLY-CLEANUP-2026-10-14.md)을 참조한다.

2026-10-12 로컬 개발 배포: Windows GPU의 인증된 OpenAI 호환 GTE 서비스 → 기존 Docker 모델 어댑터 → Elasticsearch `q_768_vec` 경로를 사용한다. 데이터셋/tenant 기본 임베딩은 GTE로 전환했다. 아래 초기 스냅샷의 BGE-M3 설명은 이전 상태이며, 현재 구성은 [배포 기록](LOCAL-GTE-DEPLOYMENT-2026-10-12.md)이 우선한다.

app/는 2026-09-21 커스텀 소스 스냅샷에서 시작했으며, 현재 개발 브랜치는 제품 경로의 OpenViking route/catalog generation/publish와 배포 의존성을 제거하고 공통 scope/C128 검색으로 전환했다. 원본 HEAD는 432dd7e6f98796b875beb41dd1abf2ced3ad8d5b이며 실제 환경설정·개인키·문서 데이터는 포함하지 않는다. 이 checkout의 변경이 운영 이미지에 배포됐거나 운영 서비스가 중지됐다는 뜻은 아니다.

백업의 실행 설정은 Python API + MySQL + Elasticsearch + Redis 호환 Valkey, CPU였다. Docker 앱에는 커스텀 PDF/Office/HWP 처리 및 DocMind 화면 변경이 포함돼 있었다. 소스 기본값과 실행 이미지가 같다고 가정하지 말고 새 이미지로 빌드한다.

| 위치 | 역할 / 주의점 |
|---|---|
| api/apps/restful_apis/docmind_api.py | DocMind API, 로그인·공용 workspace bootstrap 진입점 |
| api/apps/services/docmind_* | 공통 scope 검색·등록·폴더·경로와 계층 draft 상태 |
| api/db/services/{file,document,task,knowledgebase}_service.py | 문서 등록·파싱 작업·저장소·색인 관리. UI 제거와 별개로 필요 |
| rag/svr/task_executor_refactor/task_handler.py | DocMind 인덱싱이 직접 호출하는 공통 파싱·청킹·임베딩·색인 처리 |
| rag/app, deepdoc, rag/parser_platform | 문서 청킹과 파서 연결. 청킹 유지, 임베딩은 로컬 GTE 서비스 사용 |
| parser_services/docling_pdf, docling_office, surya, rhwp | 커스텀 PDF/Office/OCR/HWP sidecar의 소스와 Containerfile |
| rag/llm, api/db/services/llm_service.py | 모델 어댑터와 설정. Jina reranker와 답변 LLM은 서로 다른 역할 |
| web/src/pages/docmind | DocMind 문서 선택·검색 화면 |
| web/src/pages/chunk, web/src/components/document-preview | 청크 목록과 원문 미리보기 공통 컴포넌트 |
| common/settings.py | DB/저장소/검색기와 메모리·그래프 초기화. 미사용 패키지 삭제 전 정리 |
| docker/ | Linux 배포 구성과 격리된 Windows 개발 Compose·env 예제·entrypoint |
| Python/MySQL/Elasticsearch/MinIO | 기능 축소 브랜치에서 Go 및 대체 DB/저장소 소스 제거. 배포 통합 검증은 별도 필요 |

현재 청크 텍스트는 ES, 원본·청크 이미지는 객체 저장소를 통해 조회한다. 현재 파일 업로드는 MinIO에 원본을 저장하고 워커가 읽는다. 새 요구의 임시 입력·평문 삭제로 치환하기 전 MinIO 서비스를 먼저 끄면 안 된다.

## 목표

```text
Windows All-in-One 암호화 원본 (세 루트)
  → Windows 감지/복호화 실행부 (임시 출력)
  → Docker DocMind 파서 → 기존 청커 → Windows GPU GTE → ES
  → 임시 평문 및 파생물 정리

사용자 전체/폴더/문서 선택
  → 같은 범위로 BM25 + dense → RRF → C128
  → 청크별 앞 2400자 → Jina v3.5 → 일반 검색: 관련성 판정·최대 32문서·화면에서 8개씩 더 보기·문서별 위치 묶음
  → AI 답변: 기존 이름 포함 재평가·최대 16개 근거 → LLM 답변·근거
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
