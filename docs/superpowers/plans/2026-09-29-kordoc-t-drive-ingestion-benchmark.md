# Kordoc 기존 개발 서비스 교체와 T: 실제 문서 검증 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **이 문서는 실행 계획이다. 지금은 배포, Docker 기동·중지, 볼륨 삭제, T: 파일 변경, 수집·측정을 실행하지 않는다.**

**Goal:** 기존 `docmind-windows-dev` 서비스 자리와 endpoint를 `ce1f7ae` 기반 Kordoc 앱으로 교체하고, 초기화한 개발 MySQL·Elasticsearch에 실제 T: 문서를 재수집해 파싱·청킹 시간과 임베딩·검색·재시도·정리를 검증한다.

**Architecture:** 기존 개발 Compose 프로젝트의 컨테이너를 모두 내리고 MySQL·Elasticsearch의 두 named volume만 새로 만든 다음 같은 프로젝트·포트·인증/host worker 연결에 새 이미지를 올린다. cloud ephemeral 롤백의 객체 저장소 호출을 국소 수정하고, MinIO 프로세스 없는 구성에서 합성 회귀와 호출 시도 0건을 확인한 뒤 실제 T: 수집→Kordoc→청킹→임베딩→검색을 같은 서비스에서 수행한다. T: 원본은 Windows 호스트에서만 읽는다.

**Tech Stack:** Windows/PowerShell, WSL2 Docker Compose, MySQL 8.0.40, Elasticsearch 8.11.3 기본값, Python 3.13, Kordoc 4.15.7, LibreOffice, BGE-M3.

**Spec:** `AGENTS.md`, `PRD.md`, `docs/WINDOWS-DEVELOPMENT-SECURITY.md`, `docs/CLOUD-SOURCE-EPHEMERAL-STORAGE.md`, `docs/KORDOC-CUTOVER-IMPLEMENTATION-2026-09-29.md`, `docs/PHASE4-SOURCE-RECONCILIATION.md`. 이 계획을 승인된 실행 명세로 사용한다.

## Global Constraints

- `ce1f7ae`를 Kordoc 전환의 기준 커밋으로 삼는다. cloud 롤백을 수정하면 앱 소스는 `ce1f7ae`에 그 국소 delta를 더한 상태이므로, 기준 SHA·수정 커밋/패치·앱/Kordoc 이미지 ID·Kordoc patch/OCR revision을 함께 기록한다. 현재 `docmind-kordoc-parser:ce1f7ae`만 빌드됐으며 앱 이미지는 다시 빌드해야 한다. 첨부의 과거 실행 이미지/작업 트리 불일치는 당시 관측이지 현재 실행 상태가 아니다.
- 현재 기존 실행 컨테이너 8개는 중지 상태이고 named volume은 그대로 있다. 사용자는 기존 개발 스택 전체를 내려도 되며 MySQL·벡터 검색 DB(현재 Elasticsearch)를 밀어도 된다고 확정했다. 따라서 실행 시 삭제·재생성 대상은 **`docmind-windows-dev_mysql_data`와 `docmind-windows-dev_es_data` 두 개발 볼륨만**이다. 다른 볼륨, 운영 데이터, T: 원본으로 권한을 넓히지 않는다.
- `app/docker/docker-compose-windows-dev.yml`의 현재 security-gate는 `DOCMIND_DEV_DATA_CLASS=synthetic-only`만 허용해 **실제 T: 문서로 운영에 준해 검증하려는 목표와 불일치**한다. 실행 전 이 서비스의 명시적 시험 데이터 등급·gate·검사 스크립트를 목적에 맞게 함께 바꾸고 실제 입력/원본 보존/비밀값 보호/임시 평문 정리 조건을 시험한다. env 값만 바꿔 gate를 우회하거나 TLS·조직 보안 사업을 자동으로 범위에 넣지 않는다.
- `.local/docker/windows-dev.env`, host worker 설정·키, 원문, 파서 JSON, 청크 본문을 계획·Git·공유 보고서에 복사하지 않는다. 실행 산출물은 접근 제한된 ignored `.local/` 아래에 두고 식별자는 불투명 ID 또는 집계값으로 다룬다.
- T:의 원본은 읽기 전용이며 Docker bind mount를 만들지 않는다. Windows 사용자/worker 계정에서 동일 경로가 보이는지 확인하고, cloud placeholder·오프라인·junction/reparse point를 검증한다. hydration이 파일 쓰기·캐시 생성을 수반할 수 있으므로 접근 방식은 실행 전에 확정한다.
- 파싱+청킹 지표와 전체 수집·임베딩·Elasticsearch 활성화 지표를 분리한다. 첫 실행 비용과 warm 실행을 섞어 평균 내지 않는다.
- 이번 MinIO 부재 검증의 결론은 **새 cloud ephemeral 수집 기능**에만 적용한다. 일반 파일 업로드·썸네일·일반 task의 원본/이미지 경로는 여전히 `STORAGE_IMPL`을 사용하므로 전체 앱 또는 운영 MinIO 제거를 결론 내리지 않는다. 실제 교체 서비스에서 사용하는 일반 기능과 MinIO 필요 여부를 확인하며, 기존 `minio_data`는 삭제하지 않고 일반 업로드 동작도 변경하지 않는다.

## Review Focus

- T:가 대화형 사용자에게만 연결되고 worker 계정에는 없는 경우 → Task 1의 동일 계정 가시성 검사에서 중단.
- T: 파일이 cloud placeholder 또는 변동 중인 경우 → Task 1의 비내용 메타데이터 스냅샷·안정성 검사에서 제외.
- 기존 `synthetic-only` gate가 그대로인데 실제 T: 문서가 유입되는 경우 → Task 2의 목적에 맞춘 구성·검사 결과가 나올 때까지 중단.
- 초기화한 MySQL/ES와 보존된 Valkey의 과거 작업 메시지가 충돌하는 경우 → Task 3의 큐 격리·빈 baseline 검사에서 중단.
- cloud ephemeral 청크 ID 갱신 실패 후 객체 삭제가 호출되는 경우 → Task 4의 실패 주입 및 Task 5의 전 호출 계측에서 검출.
- 실패·재시도가 성공 사례의 평균 시간에 섞이는 경우 → Task 7의 attempt별 기록과 Task 8의 집계 검증에서 분리.

## 파일·구성 지도

| 실행 시 대상 | 책임 |
| --- | --- |
| `app/docker/docker-compose-windows-dev.yml`, `app/docker/docker-compose-windows-dev-generationless-e2e.yml` 또는 `app/docker/docker-compose-parser-platform.yml` | 기존 프로젝트·endpoint의 앱, MySQL, ES, BGE, Kordoc 구성. cloud 기능 시험용 MinIO 미기동 override와 실제 Compose 결과를 검증한다. |
| `app/parser_services/kordoc/Containerfile`, `app/Dockerfile` | 같은 커밋에서 파서·앱 이미지 빌드. |
| `tools/windows/Watch-DocMindEncryptedSources.ps1`, `Invoke-DocMindSourceReconciliation.ps1`, `Start-DocMindUEncryptorHostWorker.ps1` | 승인된 암호화 source의 발견, 등록, 서명된 claim, 복호화, cleanup. |
| `tools/windows/Run-DocMindSupportedBacklog.ps1` | 기존 제한된 실행 도구. 아래 범위 제약 때문에 전체 T: 측정의 기본 도구로 사용하지 않는다. |
| `app/rag/parser_platform/standard_bridge.py`, `kordoc_pilot.py`, `kordoc_office_pilot.py`, `app/rag/svr/task_executor_refactor/chunk_service.py` | Kordoc HTTP 파싱, 정규화, 청킹의 실제 경계. `chunk_service.py`의 cloud rollback만 수정하고 호출·시간 계측은 최소한으로 둔다. |
| `app/api/apps/services/docmind_ingestion_runtime.py`, `docmind_ingestion_service.py`, `app/api/db/db_models.py` | job → parser run → stage → index → activate → 양쪽 cleanup의 식별자와 상태. |
| 실행 시 새 ignored `.local/...` 결과 디렉터리 | 비내용 메타데이터, 단조시계 duration, 상태·오류코드, 자원 샘플, 집계. Git에 포함하지 않는다. |

## Task 1: 입력 범위와 T: 접근 계약 확정

- [ ] 현재 T: 루트에서 관찰된 하위 폴더 하나와 단일 `.doc` 파일을 시작점으로 삼되, 선택 범위를 `T:\` 전체 재귀 또는 명시한 폴더/파일 목록 중 하나로 기록한다. 지금 루트 목록만으로 하위 파일 수나 형식 분포를 추정하지 않는다.
- [ ] 벤치 실행 계정과 host worker 계정에서 T: 드라이브의 연결 대상, 읽기 권한, 온라인 상태가 같은지 읽기 전용으로 확인한다. 드라이브 문자가 세션별 mapping이면 서비스 계정이 접근 가능한 승인 경로로 별도 설정한다.
- [ ] 파일 내용을 열거나 원본을 수정하기 전에 상대 경로 대신 익명 ID, 확장자, 크기, mtime, cloud hydration 상태, reparse point 여부만 집계한다. 지원 대상은 Kordoc 경로의 `pdf/doc/docx/xls/xlsx/pptx/hwp/hwpx`이고, 기존 backlog 스크립트는 `pdf/doc/docx/xlsx/pptx`만 허용한다. 잠금 파일·중복·64 MiB 초과·PDF 페이지 상한·변동 파일을 별도로 집계한다.
- [ ] 실제 T: 파일이 uEncryptor2 암호문인지, 평문 cloud 파일인지 분류한다. 암호문이면 source 등록·복호화 경로를 사용한다. 평문이면 복호화 worker에 그대로 넣지 말고, 승인된 별도 읽기 전용 입력 adapter와 동일한 임시 workspace/cleanup 계약을 먼저 설계·검증한다.
- [ ] 완료 기준: 정확한 파일 집합의 비내용 manifest, 접근 계정, 지원/제외 기준, 예상 총 바이트와 파일 수가 확정된다. 접근·분류가 불확실한 파일은 처리하지 않는다.

## Task 2: 실제 T: 시험을 위한 기존 서비스 설정 정합성

- [ ] T: 문서의 데이터 소유자와 분류를 확인하고, 현재 `synthetic-only`로 고정된 Compose security-gate·`Test-WindowsDevelopmentSecurity.ps1`·env 생성/검사 값을 운영에 준한 **이 개발 시험 서비스의 실제 입력**에 맞는 명시적 등급으로 일치시킨다. 기존 프로젝트와 endpoint를 유지하고 비밀값 보호·loopback 경계·원본 read-only·작업별 임시 평문 정리 검사를 유지한다. 단순 env 값만 바꾸거나 다른 배포 사업으로 전환하지 않는다.
- [ ] host worker 설정에서 승인된 source ID, source root, `default_folder_id`/문서 매핑, 작업·receipt root, BitLocker/백업 제외, executable identity, 서명 키를 비밀값 노출 없이 확인한다. discovery/reconciliation은 source 전체를 등록할 수 있으므로 Task 1의 선택 범위를 넘지 않도록 등록 방식 또는 명시적 문서 목록을 확정한다.
- [ ] 원본은 Windows에서만 읽고, API에는 해시·상대 위치·job 식별자만 전달한다. 복호화 평문과 Kordoc 파생물은 ephemeral root에서만 살고, 성공·실패·취소 후 host와 container cleanup 완료를 검사한다.
- [ ] 완료 기준: 기존 서비스의 같은 인증/host worker endpoint로 승인된 T: 입력을 받되 선택 범위 밖 원본을 건드리지 않고, 설정 검사와 임시 평문 정리 gate가 실제 시험 등급에 맞게 통과한다.

## Task 3: 기존 개발 스택 정지와 MySQL·ES 초기화

- [ ] `docmind-windows-dev`의 Compose 구성에 연결된 컨테이너를 전부 식별하고 중지·제거한다. 이미 중지된 8개도 소속을 재확인한다. `docker compose down`은 named volume을 보존하므로 `down -v`, `volume prune`, 전체 Docker cleanup을 사용하지 않는다. 자동 watcher/host worker도 대상 job을 다시 만들지 않도록 멈춘 상태를 확인한다.
- [ ] `docker volume inspect`와 Compose volume mapping으로 `docmind-windows-dev_mysql_data`가 MySQL `/var/lib/mysql`, `docmind-windows-dev_es_data`가 ES `/usr/share/elasticsearch/data`에 붙은 **현재 개발 프로젝트의 정확한 두 볼륨**인지 확인한다. 사용 중인 컨테이너가 0개일 때 이 두 볼륨만 제거하고 Compose 재기동으로 재생성한다. 사용자 결정에 포함된 초기화이므로 별도 재승인을 요구하지 않는다. 원하면 삭제 전에 상태 메타데이터를 기록할 수 있지만 백업은 필수 gate가 아니다.
- [ ] Valkey `redis_data`는 삭제 권한 범위 밖이므로 보존한다. 그러나 예전 DB의 job ID를 담은 큐/진행 메시지를 새 MySQL에 재생하면 안 된다. 실행 전 키·큐의 namespace와 소비자 중지 상태를 비내용 메타데이터로 확인하고, 벤치 실행에는 **새 빈 Valkey data volume을 바인딩하는 Compose override**를 사용한다. 원래 `redis_data`는 그대로 두고, 새 Valkey가 빈 큐로 시작한 것을 확인한 뒤 worker를 켠다. MinIO·로그·모델 캐시 등 다른 기존 볼륨도 삭제하지 않으며, 과거 객체가 benchmark에 섞일 위험은 새 비파괴적 volume/namespace override로 격리한다.
- [ ] 기존 `docmind-windows-dev` 프로젝트 이름·loopback 포트·인증·host worker API 주소를 유지할 Compose overlay 조합을 고정한다. `Run-DocMindSupportedBacklog.ps1`의 Surya 의존과 형식 제약은 Task 7에서 해결한다.
- [ ] Compose `config --quiet`와 infra만의 건강 상태를 확인하고 새 MySQL 스키마·빈 source/job/parser_run, 새 ES의 문서 색인·청크 수 0, Valkey 빈 큐를 기록한다. ES 서비스 내부 인덱스는 문서 색인과 구별한다.
- [ ] 완료 기준: 기존 프로젝트의 두 DB만 새 빈 baseline이고 Valkey 과거 메시지가 재생되지 않는다. 두 DB 외의 기존 볼륨과 T: 원본은 그대로 있다. 앱·Kordoc 배포와 계정/설정 재구축은 Task 5에서 이어진다.

## Task 4: cloud ephemeral 롤백의 객체 저장소 호출 수정

- [ ] `ce1f7ae`의 `app/rag/svr/task_executor_refactor/chunk_service.py`에서 `_insert_main_chunks`의 chunk ID 갱신 실패 → `_rollback_insertion` → `_delete_image` → `STORAGE_IMPL.delete` 연결을 확인한다. 이는 첨부의 **정적 도달 경로**이며 실제 요청 발생 증거가 아니다. `TaskService.update_chunk_ids`는 DB update 영향 행 수를 확인하지 않고 `_update_task_chunk_ids`는 `DoesNotExist`에서만 `False`이므로 task 행 삭제만으로 분기 도달을 가정하지 않는다.
- [ ] 해당 분기에 실패를 명시적으로 주입하는 집중 테스트를 추가한다. cloud ephemeral context(`_docmind_ephemeral_workspace`)에서는 색인 청크 rollback을 유지하면서 `STORAGE_IMPL.delete` 시도 0건을 요구한다. 일반 task context에서는 기존 이미지 삭제 동작을 유지한다. 시험에서 의도된 rollback 분기까지 실제 도달했음을 별도 assertion으로 남긴다.
- [ ] `chunk_service.py`의 cloud ephemeral 분기에만 객체 이미지 삭제를 건너뛰는 최소 수정을 적용한다. 다른 STORAGE_IMPL 호출·일반 업로드/썸네일/일반 task 경로는 건드리지 않는다. 관련 집중 pytest와 기존 수집 회귀를 통과시킨다.
- [ ] 완료 기준: 실패 주입에서 cloud 경로는 ES rollback 및 임시 workspace cleanup을 수행하고 객체 저장소 get/put/delete 시도는 0건이며, 일반 task의 원래 롤백 동작은 보존된다.

## Task 5: MinIO 프로세스 없는 cloud 기능 회귀

- [ ] `ce1f7ae` 기준과 Task 4의 국소 수정을 반영해 앱 이미지를 새로 빌드한다. Kordoc `docmind-kordoc-parser:ce1f7ae` 이미지 ID, 앱 이미지 ID, 소스 SHA, patch/OCR revision을 기록하고 실행 컨테이너의 이미지·코드를 대조한다. 첨부의 과거 이미지 불일치는 현재 증거로 쓰지 않는다.
- [ ] `ragflow-cpu`의 Compose `depends_on.minio`, `STORAGE_IMPL=MINIO` client 생성, API storage health, 초기 계정/설정·모델 provider bootstrap의 실제 연결 의존을 조사한다. 같은 `docmind-windows-dev` 서비스에서 **cloud 시험 범위만** MinIO 프로세스 없이 띄우는 override를 만들고 합성된 Compose config로 `minio` 미기동과 앱·worker/Kordoc 연결을 확인한다. 설정 우회로 일반 업로드 기능이 정상이라고 주장하지 않는다.
- [ ] 새 MySQL에 실제 계정·프로젝트·폴더·source mapping·parser 설정과 BGE-M3/Jina 모델 provider 설정을 필요한 범위로 재구축한다. `app/docker/entrypoint.sh`, `app/common/settings.py`, storage client 생성, health check 및 기동 관찰로 각 bootstrap 단계의 MinIO 연결 의존을 확인하고, cloud 경로에 한정된 해결만 적용한다. 기존 `minio_data`는 건드리지 않는다.
- [ ] app 프로세스의 `STORAGE_IMPL` 경계에 계측 wrapper를 두어 cloud job 동안 **호출 시도 자체**를 get/put/delete 및 동등한 `rm`·`obj_exist`·bucket/health 호출까지 방법별·job별로 집계한다. MinIO 네트워크 차단이나 서비스 부재만으로 요청 0건이라고 판단하지 않는다. 원문/키/object 이름은 기록하지 않는다.
- [ ] 합성 입력의 성공, 파싱 실패, ES 색인 실패, Task chunk ID 갱신 실패 주입, 재시도, host/container cleanup 실패를 각각 같은 cloud 수집 경로로 검증한다. 모든 사례에서 호출 시도 0건, 예상 DB/ES 상태, lease/fencing, 중복 색인 방지, cleanup 실패의 명시적 상태와 재시도 후 정리를 확인한다.
- [ ] 완료 기준: 기존 endpoint에 새 앱·Kordoc 이미지와 실제 계정/설정/모델이 맞춰지고, MinIO 프로세스 없는 cloud 경로가 성공과 지정된 실패·재시도 경로를 완주하며 get/put/delete 호출 시도 0건이 계측으로 입증된다. 일반 파일 업로드 등 사용 중인 기능이 MinIO를 필요로 하면 실제 교체 서비스에서는 그 프로세스를 유지하고 기능 목록·의존·검증 범위를 기록한다. 전체 서비스의 MinIO 제거 완료로 결론 내리지 않는다.

## Task 6: 파싱·청킹 계측 준비

- [ ] job ID, document ID, version ID, parser_run ID, chunk_set ID, attempt/fencing token을 한 행으로 연결한다. `ParserRun`의 상태·staged chunk/token count와 `DocmindIngestionJob`의 상태·cleanup·error code를 쓰되 본문·원본 경로·error message 전문은 결과에 남기지 않는다.
- [ ] Python 단조시계(`perf_counter_ns`)로 `ParserPlatformStandardBridge.parse` 진입→Kordoc HTTP `parse_pilot_service` 반환(HTTP+Node 변환/OCR 포함), 반환→정규화·artifact 기록 완료, `chunk_parser_platform_document` 진입→청크 반환을 각각 측정한다. `chunk_service.py`의 두 호출 경계가 제품 경로이며 `ParserRun.lifecycle`의 `PARSING_KORDOC`/`NORMALIZING`만으로는 정확한 duration이 나오지 않는다.
- [ ] 별도 값으로 host claim→평문 인도, parser stage, embedding/ES staging, activation, host/container cleanup, 전체 observation→`COMPLETE`를 잰다. Node 내부 변환·OCR 시간을 별도 분리하려면 `app/parser_services/kordoc/src/{server,worker,parse,convert}.mjs`의 안전한 stage 타이머를 추가하고 Python HTTP 시간과 중복 합산하지 않는다.
- [ ] 계측은 저장된 청크·토큰·출처·상태를 바꾸지 않아야 한다. 합성 PDF/DOC/XLSX 등 1건으로 단조시계 값 ≥ 0, 단계 합계 ≤ 전체 작업시간, 오류 시 부분 기록, 동일 run ID 연결을 확인한다. 첫 모델 로드/토크나이저 초기화 시간은 cold로 표기한다.
- [ ] 시간 계측 변경을 반영한 앱 이미지를 다시 빌드하고 기존 endpoint의 실행 이미지 ID·코드 해시를 결과에 기록한다. Kordoc 이미지·patch revision은 Task 5에서 확인한 값과 대조한다.
- [ ] 완료 기준: 한 문서의 parse HTTP, normalize, chunk, index, cleanup 시간이 서로 구분되고, 원문 없이 재집계할 수 있다.

## Task 7: T: 실제 문서 재수집과 검색 검증

- [ ] 실행 전에 source별 자동 watcher·midnight reconciliation·다른 worker·재처리 작업이 대상 범위를 건드리지 않는지 확인한다. 초기화한 기존 개발 프로젝트에서 한 번에 한 문서만 claim하고, 파일 선택 순서와 형식별 표본 수를 고정한다.
- [ ] `Run-DocMindSupportedBacklog.ps1`는 Surya 사전검사와 오래된 source/PDF 제약, 일부 형식 누락, 프로젝트 이름 고정을 포함한다. 이를 그대로 전체 측정에 쓰지 않는다. Kordoc health 기준의 새 제한형 runner를 만들거나 host worker `-Once -SkipRetries -ClaimSourceId/-ClaimFormats`와 정확한 job 목록으로 동일한 단일-job 절차를 구성한다.
- [ ] Task 5의 합성 회귀를 통과한 뒤 실제 T: 파일을 기존 host worker/source 연결로 재수집한다. 형식별 소수 파일의 첫 실제 smoke에서 Kordoc 선택, 청크 출처, `COMPLETE`, host/container cleanup, ES 활성 청크 수를 확인한다. 실패하면 동일 범위를 무제한 반복하지 않고 원인 수정 후 별도 attempt로 기록한다.
- [ ] 본 측정은 선택 manifest의 지원 파일을 순차 처리한다. 매 문서에서 시작/종료 UTC, 각 단조시계 duration, 형식·바이트·페이지/시트/슬라이드 수(알 수 있을 때), 블록/청크/토큰 수, OCR 사용, peak RSS/컨테이너 메모리, 상태·오류코드·attempt를 기록한다. 파일이 중간에 바뀌거나 cloud 연결이 끊기면 해당 건을 제외·중단한다.
- [ ] 중단 조건: 다른 source job claim, T: 불가, 모델/파서 health 실패, host free memory 하한, timeout, cleanup 미완료, ES staging 불일치, 원본 변경. 중단 후 활성 작업의 lease/cleanup 상태를 확인하고 새 attempt는 원본에서 다시 읽도록 한다.
- [ ] 실제 계정으로 같은 endpoint에서 source/폴더/문서 범위 검색을 실행하고 BGE-M3 임베딩, Elasticsearch 후보, Jina rerank, 응답 출처가 활성 문서/청크와 일치하는지 확인한다. 선정한 질의의 성공률·검색 지연을 파싱·청킹 지표와 별도 기록한다. MinIO 없는 cloud 구성을 유지할 수 있는지와 일반 사용 기능의 MinIO 필요 여부를 함께 보고한다.
- [ ] 완료 기준: 선택 manifest의 실제 T: 파일마다 성공 또는 명시적 제외/실패 사유가 대응하고, 로그인→재수집→파싱→청킹→임베딩→검색·재시도·cleanup을 같은 서비스에서 확인했으며 미완료 평문 workspace가 없다.

## Task 8: 집계·교체 판정·복구

- [ ] 성공한 첫 시도 기준으로 형식별 `parse_http`, normalize, chunk, parse+chunk, index, end-to-end의 count·합계·중앙값·p90/p95·최대값·MiB당 시간·청크당 시간을 낸다. n<20이면 p95는 참고값으로만 표시한다. cold, warm, OCR, 변환(DOC/PPTX), 재시도 결과는 각각 따로 본다.
- [ ] 실패/제외는 확장자·오류코드·단계·attempt·cleanup 상태별 건수만 보고한다. 측정 구간에 포함되지 않는 hydration, queue wait, 모델 cold start, 외부 API 지연을 빠뜨리지 않고 별도 열로 둔다. 합성 HWPX 과거 수치와는 직접 속도 우열을 주장하지 않는다.
- [ ] MySQL job `COMPLETE`/`FAILED`, parser_run `READY`/실패, 활성 chunk_set, Elasticsearch 실제 청크 수와 cleanup receipt를 대조한다. 불일치면 성능 성공으로 집계하지 않는다.
- [ ] 결과는 ignored 로컬 원자료와 원문 없는 공유용 요약으로 나눈다. 실패 후 재시작은 두 개발 DB를 다시 빈 baseline으로 만들 필요가 있는지 판단하되, 기존 다른 볼륨·T: 원본의 정리는 측정 결과 확인 뒤 별도 범위로 다룬다.
- [ ] 완료 기준: 기존 서비스 교체 후 같은 endpoint에서 실제 계정·T: 문서·모델로 수집과 검색이 동작하고, parse/chunk 시간 분포·전체 수집 및 검색 지연·재시도/cleanup 결과와 MinIO 검증의 정확한 기능 범위를 다른 실행자가 재현할 수 있다.

## 실행 전에 확정할 결정

1. T:의 정확한 선택 범위(전체 재귀인지 하위 폴더/표본인지), 파일 소유자·데이터 분류, cloud hydration 허용 방식.
2. T:의 파일이 uEncryptor2 암호문인지 평문인지, 실제 worker 계정에서 T:를 읽을 수 있는지.
3. 실제 T: 입력에 맞는 기존 서비스 시험 데이터 등급·설정, 기존 계정/프로젝트/source 재구축 범위, 현재 사용 중인 일반 업로드 기능의 MinIO 필요 여부.
4. PDF 페이지 상한, 64 MiB 입력 제한, OCR 모델 revision, 시간/메모리 중단값, 형식별 표본 크기와 전체 측정 종료 기준.

기존 `docmind-windows-dev` 컨테이너 전체 중지 및 위 두 개발 DB 볼륨 초기화는 **이미 확정된 결정**이다. 실행 전 선택 항목이나 추가 승인 조건으로 되돌리지 않는다.

## 이번 계획 작성 검증

`AGENTS.md`, 관련 Compose·Windows 도구·수집 코드·보안/파서 문서를 읽고 경계를 확인했다. Docker 기동·중지, DB/볼륨 초기화, 원본 접근·수정, 실제 수집·성능 측정은 수행하지 않았다.
