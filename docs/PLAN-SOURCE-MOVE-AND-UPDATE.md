# 구현 인수인계: 부서 내부 이동 시 색인 재사용 + 수정 감지 + 실패 복구

작성일: 2026-09-28. 이 문서는 구현 계획이며, 아래 기능을 구현 완료했다는 보고가 아니다.

## 1. 목표와 범위

사용자는 다른 세션에서 다음 작업을 구현하려고 한다.

- DEPT_1, DEPT_2, DEPT_3 각각의 내부에서 상위↔하위 폴더 이동 또는 이름 변경 시, 내용이 같으면 기존 문서 ID와 검색 색인을 유지한다.
- 위치와 무관하게 내용이 수정되면 변경을 감지하고 재인덱싱한다.
- 복사와 이동을 구분하고, 불확실한 파일을 잘못 합치지 않는다.
- 현재 실패한 Excel 처리의 복구 및 잘못된 UI 상태 표시를 먼저 해결한다.
- DEPT_1→DEPT_2 같은 부서 간 이동의 색인 재사용은 이번 범위에서 제외한다. 기존 검색/source 경계를 완화하지 않는다.

현재 요청은 계획 전달까지다. 다음 세션에서는 사용자의 구현 지시를 받은 뒤 실행한다. 원본 삭제, 원본 암호화 저장소 직접 수정, 무관한 서비스 재시작은 이 문서만으로 허가된 것이 아니다.

## 2. 시작 전 확인

- 저장소: `C:\DocMindDev\docmind`.
- `AGENTS.md`가 요구하는 `PRD.md`, `WINDOWS_DEVELOPMENT.md`, `docs/CURRENT-ARCHITECTURE.md`, `docs/HANDOFF-STATUS.md`를 읽는다. `app/AGENTS.md`, 프런트 수정 시 `app/web/CLAUDE.md`도 따른다.
- 현재 HEAD, git status, 실행 이미지, 실제 예약 작업 인자부터 확인한다. 다른 세션의 변경을 덮어쓰지 않는다.
- 계획 작성 시 기존 untracked 문서: `docs/DEPT1-XLSX-VALIDATION-2026-09-28.md`, `docs/HANDOFF-2026-09-23.md`. 보존한다.
- 실제 실행 설정은 `.local/uEncryptor2/host-worker.live.json`이었다. 과거 `C:\DocMindHostE2E\host-worker.json`을 현재 설정으로 오인하지 않는다.
- 설정/키/토큰/문서 본문을 출력하거나 Git에 넣지 않는다. 자격증명을 인수인계 문서에 복사하지 않는다.
- Windows에서 uEncryptor2, Docker에서 DocMind를 실행하는 구조를 유지한다. C128/BGE-M3/Jina v3.5 검색 계약도 유지한다.
- 사용자는 DEPT 1/2/3을 원하지만, 마지막 관찰된 설정에는 DEPT_1, DEPT_2와 TEST1 home이 있었다. DEPT_3 연결·활성화를 추정하지 말고 확인하며, home을 임의로 교체하거나 비활성화하지 않는다.
- S: 업로드가 DEPT_1의 암호화 원본에 반영되는 것은 해당 시험 Excel로 확인했다. D: 파일시스템은 NTFS이고 파일 ID 조회도 성공했지만, uDrive 이동 전후 ID 유지 여부는 아직 검증하지 않았다.

## 3. 긴급 선행 사항: 실패한 Excel 작업

아래는 2026-09-28 16:44경 KST의 관찰이다. 오래된 상태일 수 있으므로 반드시 재조회한다.

| 항목 | 관찰값 |
| --- | --- |
| Source | `dept-1-e2e` |
| 이동 전 document ID | `244efabc016740eaa030db8206f6fe07` |
| 이동 후 document ID | `3ee63480f91447a983237d5e5f5cd138` |
| 이동 후 job ID | `45326ad90a5fc3467fb2c8c4c462bc7a` |
| ParserRun ID | `2391a50cf0a19fb0a7cdd2c93c3a56a4` |
| Task ID | `41db2d5e670977e288ec3a247f18ac33` |
| 최초 수집 | 이동 전 문서는 40개 청크 생성, 임시 평문 정리, 폴더 검색까지 성공 |
| 이동 후 작업 | 16:37:58 생성, 16:38:04 파싱 시작 |
| 프로세스 종료 | 16:38:34 API 서버 exit 137, 16:38:35 자동 재시작 |
| 실패 보고 | 16:39:49 `FAILED` / `HOST_WORKER_ERROR` |
| 남은 상태 | 청크 0, 활성 색인 없음, ParserRun QUEUED, task 약 9.3% |
| 정리 | Windows COMPLETE, 컨테이너 PENDING |

당시 컨테이너 `/run/docmind-ephemeral-parser`의 대상 작업에 19,847바이트 임시 XLSX, manifest, 빈 lock이 남아 있었다. Windows 정리 완료를 전체 평문 정리 완료로 보고하면 안 된다.

메모리 제한은 4 GiB, 관찰 사용량 약 3.6 GiB, cgroup `oom_kill=2`였다. API 종료와 OOM 기록은 부합하지만 정확히 어떤 할당이 원인인지는 확정하지 않았다. 같은 시각 기존 진단에서 실행한 무거운 `api.db.db_models` import도 메모리 압박에 기여했을 가능성이 있다. Excel 자체 오류로 단정하지 않는다.

현재 복구 지연 원인으로 확인한 코드:

- 파싱이 API 요청 수명 안에서 실행되어 프로세스 강제 종료 시 finally 정리도 실행되지 못할 수 있다.
- 당시 reaper TTL 3600초, 실행 간격 300초. 즉시 실패해도 정리까지 긴 대기가 생긴다.
- `cleanup_state=PENDING`인 FAILED 작업은 재시도 예약에서 제외한다. 이 안전 조건을 삭제해서 해결하지 않는다.
- 재시도 예약 호출이 reconciliation claim 경로에 있고, 일반 watcher manual scan에서는 실행되지 않는다. 당시 구성에서는 자정 scheduled 경로까지 지연될 수 있었다.
- UI는 검색 자격이 없으면 일괄 PENDING으로 표시하여 FAILED도 ‘인덱싱 대기’로 보인다.

### 3-A. 복구 구현 순서

1. 대상 job, 실행 중 프로세스, lease/fencing, 임시 manifest 소유 관계를 가벼운 진단으로 재확인한다.
2. 작업이 종료됐다는 근거로 정상 정리 경로를 실행/보완한다. 활성 작업 폴더를 삭제하지 않으며 TTL만 일괄 축소하지 않는다.
3. 실패 작업 정리와 재시도를 상시 유지보수 루프에 연결한다. 재시작/중복 호출에도 안전하고 횟수 제한·백오프·최종 실패 상태가 있어야 한다.
4. 메모리 사용, 동시 실행, 파서 실행 위치를 조사해 재발 방지안을 선택한다. 단순 메모리 증설만을 근본 해결로 간주하지 않는다. 무거운 파싱을 API 수명에서 분리하는 것은 변경 범위를 별도 평가한다.
5. 정리 후 대상 Excel만 재처리하여 검색·임시 평문 삭제를 확인한다.
6. UI 상태를 대기/처리 중/실패/정리 중/재시도/완료로 실제 상태에 맞게 표현한다. 안전한 오류 코드만 노출한다.

### 진단 주의

실행 중인 제한 4 GiB 컨테이너에서 ORM 앱 전체를 별도 Python 프로세스로 import하는 진단을 반복하지 않는다. 필요한 경우 `.local/uEncryptor2/audit-dept1-move-light.py` 또는 `audit-xlsx-pending-diagnosis.py`의 가벼운 pymysql/yaml SELECT 방식을 참고한다. 설정은 메모리에서 읽되 비밀값을 출력하지 않는다. 본문/청크/전체 로그 덤프는 금지한다.

## 4. 실험: 이동 식별 방식 결정

### 확정 방향: DocMind 내부 API 기반

사용자는 후속 대화에서 **DocMind 내부 API 기반이면 충분하다**고 확인했다. 현재 서버 파일시스템 감시→DocMind 내부 API 전달 구조를 유지하며, 클라우드 API 확보를 구현의 필수 선행 조건으로 두지 않는다. 아래 클라우드 연계 사항은 공식 규격이 확보되는 경우의 선택지다.

- 파일 작업을 **실행하는 API**와, 클라우드/S드라이브에서 일어난 작업을 **관찰하는 webhook·변경 이력·delta API**를 구분한다. 실행 API만 존재한다고 외부 이동까지 자동 통보되는 것은 아니다.
- 안정적인 원본 파일 ID, 부모 폴더 ID, 내용 버전/해시, 삭제 표식, 변경 커서와 재전송 계약이 제공되면 해당 정보를 우선 사용한다.
- 순서 뒤바뀜·중복·누락·권한 변경·커서 만료를 검증하고 정합성 스캔은 보조 복구 수단으로 유지한다.
- 현재는 아래 Windows 파일 ID 관찰 방식을 검증하여 내부 API에 연결한다. 존재가 확인되지 않은 uDrive API를 추정하여 구현하지 않는다.

### 내부 API 목록 (공통 `/api/v1`)

기존 문서 작업 요청 API: `POST /docmind/documents`, `PUT /docmind/documents/{id}/content`, `PATCH /docmind/documents/{id}`, `POST /docmind/documents/{id}/move`, `POST /docmind/documents/{id}/copy`, `DELETE /docmind/documents/{id}`. 이들은 실제 원본 작업 adapter 검증을 전제로 하며, 외부 S드라이브에서 이미 수행된 작업을 다시 실행하기 위해 호출하면 안 된다. 이번 자동 이동 최적화가 모든 원본 쓰기 API의 활성화를 의미하지 않는다.

자동 관찰 API: `POST /cloud-sync/host-worker/observations`, `POST /cloud-sync/host-worker/scans/events`, `POST /cloud-sync/host-worker/deletions`. 외부 이동은 우선 스캔 API의 식별 정보와 대조 처리를 확장한다. 별도 이동 관찰 endpoint는 필요가 확인될 때만 추가한다.

작업 실행/정리 API: `POST /cloud-sync/host-worker/claim`, `PUT /cloud-sync/host-worker/jobs/{id}/artifact`, `POST /cloud-sync/host-worker/jobs/{id}/status`.

조회 API: `GET /docmind/documents/{id}`, `GET /docmind/operations/{id}`, `GET /docmind/admin/hierarchy`, `POST /docmind/search`. ingestion 상태와 source-operation 상태는 서로 다른 작업이므로 조회에서 구분한다. host-worker API의 서명·source 경계와 사용자 API의 로그인/권한 검사를 유지한다.

### 파일시스템 보완 경로 실험

업무 문서 대신 비민감 합성 파일로 정상 클라우드/S드라이브 경로를 통해 실험한다. 암호화 원본 저장소에 평문을 직접 쓰거나 원본 파일을 직접 이동하지 않는다.

각 DEPT에서 실제 연결을 확인한 뒤 다음 값을 이동 전후 기록한다: 원본 source, 볼륨 식별자, 파일 ID, 상대경로, 암호화 해시, 크기/mtime. 필요 시 승인된 복호화 파이프라인으로 평문 해시를 확인한다.

시험 동작:

- 상위→하위, 하위→상위, 이름 변경, 폴더 자체 이동
- 내용 수정 후 저장(Office의 임시 파일 교체 방식 포함)
- 복사, 같은 이름 덮어쓰기, 동일 내용 파일 여러 개

판단:

- ID가 유지된다면 NTFS 식별자 기반으로 동일 부서 내부 이동을 연결한다.
- ID가 바뀐다면 클라우드 공식 고유 ID 또는 검증된 rename 정보 등 보조 근거를 조사한다. uDrive 공개 파일 ID API는 앞선 검색에서 확인하지 못했다.
- 단일 해시 일치만으로 복사본을 원본과 동일 문서로 합치지 않는다. 애매한 경우 기존 신규 처리로 보수적으로 처리한다.
- 파일 ID는 볼륨/호스트 문맥과 함께 관리한다. 삭제 후 재사용, hard link, 파일 교체를 고려하여 영구적 전역 ID로 간주하지 않는다.

## 5. 구현 설계

### 5-A. 문서 정체성, 위치, 내용 버전을 분리

- DocMind document ID: 사용자에게 보이는 문서 정체성.
- 외부 식별 정보: provider ID 또는 검증된 host/volume/file ID. 서로 다른 종류를 같은 값으로 혼용하지 않는다.
- 위치: source + folder + relative path.
- 내용 버전: ciphertext SHA-256 및 plaintext SHA-256.
- 검색 버전: parser/config/embedding 관련 fingerprint와 active chunk set.

`source_object_id` 필드가 이미 있지만 기존 provider 연동 용도와 충돌하지 않게 네임스페이스/별도 필드 필요 여부를 결정한다. 스캔 항목에는 파일 ID가 없으므로 호스트 payload, 엄격한 API 검증, 스캔 DB 기록과 마이그레이션을 함께 바꿔야 한다. 기존 문서는 안전한 baseline 관찰로 식별 정보를 연결하며 연결만을 위해 전체 재인덱싱하지 않는다.

### 5-B. 스캔 처리 순서 변경

현재는 상대경로로 mapping을 찾고 스캔 완료 시 사라진 경로를 삭제 처리한다. 새 설계는 **완전하고 안정적인 스캔의 이동 대조를 먼저 수행한 뒤, 남은 신규/삭제를 확정**한다.

- 기존 경로가 사라지고 같은 source에서 동일 식별자의 새 경로가 확인되면 이동 후보.
- 내용까지 동일하고 충돌이 없을 때 동일 mapping/document/chunk set을 유지하며 위치를 원자적으로 갱신.
- 경로/이름만 바뀌어도 mtime 변화를 내용 변경으로 오판해 불필요한 job을 만들지 않도록 계약 조정.
- 폴더 이동은 하위 문서 경로와 검색 폴더 구조까지 반영.
- 스캔 배치 경계를 넘어 old/new가 나뉘는 경우, 불완전 스캔, 중복 이벤트, watcher 재시작 처리.
- 새 위치가 확인되지 않았거나 권한/source 경계가 불확실한 문서를 이전 범위에서 계속 노출하는 식으로 검색 공백을 감추지 않는다.
- 기존 앱 요청 MOVE 완료 함수를 가짜 operation으로 호출하지 않는다. 재사용 가능한 mapping 갱신을 올바른 공통 소유 경로로 정리한다.

### 5-C. 내용 수정 규칙

| 상황 | 동작 |
| --- | --- |
| 동일 source, 같은 문서, 내용 동일, 위치 변경 | 문서·활성 색인 ID 유지; 위치만 갱신 |
| 위치 동일, 내용 변경 | 새 버전 파싱·색인 후 활성 버전 교체 |
| 위치와 내용 모두 변경 | 위치 연결 + 새 내용 재인덱싱 |
| 암호화 해시 변경, 복호화 해시 동일 | 안전하게 새 원본 버전과 기존 파싱/색인을 연결; 임시 평문 정리 후 재사용 |
| 복사 | 별도 document ID; 이동으로 합치지 않음 |
| 증거 불충분 | 보수적 신규 처리; 잘못된 색인 공유 금지 |
| source 간 이동 | 이번 최적화에서 제외; 검색 경계 유지 |

1차 구현은 이미 색인 완료된 문서의 동일 source 내부 이동을 우선한다. 복호화 해시 비교를 통한 재사용은 데이터 모델 정합성·parser/config fingerprint 조건까지 확인한 뒤 별도 단계로 구현할 수 있다. 색인 재사용을 위해 search_cleanup_complete 등 안전 표식을 임의로 세팅하지 않는다.

Office 파일의 ZIP 메타데이터만 변했는지까지 구분하는 의미 기반 해시는 1차 범위에서 제외한다. 평문 파일 해시가 달라지면 재인덱싱한다. 이동하면서 수정한 파일을 놓치지 않도록 ID만으로 내용 검사를 생략하지 않는다.

### 5-D. 동시성 및 검색 정합성

- 연속 저장은 안정화/중복 제거 후 처리한다.
- 파싱 중 이동·수정·삭제 시 오래된 작업 결과가 새 mapping/색인을 덮어쓰지 못하도록 generation/fencing을 적용한다.
- 재시작 후 미완료 이동 처리의 재실행은 멱등적이어야 한다.
- 검색 결과의 폴더 필터, 경로, 파일명, 문서 링크, 임시 미리보기 경로 및 관련 캐시를 갱신한다.
- 순수 이동에는 새 임베딩/파싱 job이 없어야 한다. 내용을 수정하면 새 색인 준비 후 활성 버전을 전환하며 중간 상태는 UI에 명시한다.

## 6. 주요 코드 위치

아래는 조사 당시 위치이며 수정 전 다시 확인한다.

| 경로 | 역할 |
| --- | --- |
| `tools/windows/Watch-DocMindEncryptedSources.ps1` | FileSystemWatcher, 이벤트 대기, discovery scan |
| `tools/windows/DocMindUEncryptorHostWorker.Common.ps1` | `Get-DocMindSourceSnapshotEntries`; 현재 경로/암호화 해시/크기/mtime만 수집 |
| `tools/windows/Invoke-DocMindSourceReconciliation.ps1` | 스캔 전송, scheduled reconciliation claim |
| `tools/windows/Start-DocMindUEncryptorHostWorker.ps1` | 복호화 작업·정리·작업 claim |
| `app/api/db/db_models.py` | SourceDocument, SourceScanEntry, SourceVersion, IngestionJob 등 |
| `app/api/apps/services/docmind_reconciliation_service.py` | `record_scan_batch`, `complete_scan`, `apply_authoritative_deletions`, `reschedule_retryable_jobs` |
| `app/api/apps/services/docmind_source_operation_service.py` | `mark_indexing_complete`; MOVE/RENAME mapping 갱신, cross-source MOVE는 현재 거부 |
| `app/api/apps/services/docmind_ingestion_service.py` | 내용 관찰, claim/fencing, 파싱 요청, 정리 maintenance |
| `app/api/apps/services/docmind_ingestion_runtime.py` | 실제 파서 연결 |
| `app/rag/parser_platform/ephemeral_input.py` | 임시 입력 수명/정리 |
| `app/api/apps/restful_apis/docmind_reconciliation_api.py` | 재시도 예약 호출 경로 |
| `app/api/apps/services/docmind_source_projection_service.py` | 검색 자격·폴더 projection; 미완료를 일괄 PENDING 표시 |
| `app/api/apps/services/docmind_api_service.py` | 폴더→문서 scope, 결과 경로 갱신 |
| `app/api/db/services/docmind_document_path_service.py` | 문서 표시 경로 |
| `app/web/src/pages/docmind/source-tree.tsx` | PENDING → ‘인덱싱 대기’ 라벨 |

기존 MOVE 테스트는 `app/test/unit_test/api/apps/services/test_docmind_source_operation_service.py`의 `test_supported_same_source_move_preserves_identity_until_index_confirmation` 등을 참고한다. 이는 외부 S드라이브 이동 E2E가 아니다.

## 7. 검증과 합격 기준

### 자동 테스트

- `tools/windows/Test-DocMindUEncryptorHostWorker.ps1`: 파일 ID 수집, snapshot 계약, 기밀값 미노출.
- 기존 reconciliation/source_operation/ingestion/source_projection 단위 테스트에 이동·재시도 회귀 추가.
- 동일 파일 ID/해시 이동, 이름 변경, 폴더 이동, 동일 내용 복사, ID 재사용/충돌, Office 저장 교체, 덮어쓰기.
- 스캔 배치 분리, 중복·누락 이벤트, 부분 스캔, worker 재시작, 처리 중 이동·수정·삭제.
- 정리 실패·프로세스 강제 종료는 격리된 합성 테스트 환경에서 재현한다. 실제 서비스에 의도적으로 OOM을 일으키지 않는다.
- UI 실패/정리/재시도 상태와 새 위치 경로 표시 테스트.

### 실제 합성 파일 E2E

DEPT_1 검증 후 DEPT_2, DEPT_3 순서. 각 source의 감시 활성화와 경로는 먼저 확인한다.

| 시험 | 합격 기준 |
| --- | --- |
| 최초 업로드 | 수집→복호화→색인→정리→검색 성공 |
| 상위↔하위/이름 변경 | document ID, active chunk set ID 유지; 추가 파싱/임베딩 job 0 |
| 새 폴더 검색 | 새 위치에서 검색, 이전 직접 위치에 중복 없음; 상위 재귀 검색은 계속 포함 가능 |
| 내용 수정 | 고유 시험 문구 변경이 검색에 반영; 이전 버전과 활성 색인이 중복되지 않음 |
| 수정 동반 이동 | 위치·내용 모두 최신 상태 |
| 복사 | 원본과 복사본 별도 document ID |
| 실패 후 회복 | 정확한 실패 표시, 안전 정리 후 제한된 자동 재시도 |
| 정리 | 완료 작업의 Windows/컨테이너 임시 평문 0 |

문서 본문·원시 청크·키 대신 ID 유지 여부, job 개수, 상태, 해시 일치 여부, 검색 scope 위반 건수로 증거를 남긴다. 실제 업무 파일을 수정하거나 내용 출력하지 않는다.

## 8. 구현 단계와 복잡도

1. **실패 복구/상태 표시**: 중간~높음. 현재 장애 해결과 재발 방지 우선.
2. **이동 식별 실험**: 낮음~중간. 결과가 후속 설계의 분기점.
3. **동일 source·완료 문서·내용 동일 이동 최적화**: 중간. 호스트/API/DB/reconciliation을 함께 수정.
4. **암호화 변경·평문 동일 재사용 및 동시 수정 처리**: 중간~높음. 버전과 정리 계약에 주의.
5. **각 DEPT 검증·단계 적용**: 중간. 설정/권한 경계 및 운영 영향 확인.

검증 결과 없이 ‘각 DEPT 이동 완료’라고 보고하지 않는다. 부분 완료, 판정 불가 시 재인덱싱하는 fallback, 미지원 부서 간 이동을 명시한다. 구현 완료 후 변경 파일, 회귀 결과, 실제 검증 증거, 남은 제한을 별도 보고한다.

## 9. 다음 세션에 붙여넣을 요청

> C:\DocMindDev\docmind\docs\PLAN-SOURCE-MOVE-AND-UPDATE.md를 읽고 이 계획대로 구현해줘. 먼저 현재 실패한 Excel 작업의 상태를 재확인하고 안전한 정리·복구·UI 상태 표시부터 해결해. 그다음 uDrive의 파일 ID 유지 여부를 비민감 시험 파일로 검증하고, DEPT_1/2/3 각각 내부의 이동·이름 변경은 내용이 같으면 기존 문서 ID와 색인을 유지하도록 구현해. 내용 수정은 재인덱싱하고 복사는 별도 문서로 구분해. 부서 간 이동 최적화는 제외해. 기존 사용자 변경과 원본·암호화 키는 보존하고, 실제 감시 설정을 확인한 뒤 단계별 테스트 결과를 보고해. 현재 계획의 관찰값은 과거 시점이므로 모두 재확인해.
