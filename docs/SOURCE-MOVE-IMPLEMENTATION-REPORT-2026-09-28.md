# DEPT_1·DEPT_2·TEST1 내부 이동 색인 재사용 보고서

작성일: 2026-09-28

## 결론

**내부 이동 색인 재사용과 HWP 제외 수집 선택 기능을 구현·배포했다. uDrive 실이동 검증과 DEPT_2 지원 형식 전체 수집은 진행 중이다.** DEPT_1·DEPT_2·TEST1 감시는 가동하고, 공통 Host Worker는 PDF·DOC·DOCX·XLSX·PPTX만 선택한다. HWP/HWPX 대기 작업과 기존 실패 작업은 보존한다. 기존 원본, 암호화 키 및 다른 에이전트의 미커밋 변경은 보존했다.

최신 대상은 DEPT_1, DEPT_2, TEST1이다. 사용자 확인에 따르면 TEST1의 `U:\`는 암호화 원본 루트 `D:\uPLEXsoft\uDrive\USER\test1`과 연결된다. 호스트 설정의 `home-test1-e2e`가 이 루트를 이미 가리키므로 U:를 별도 원본으로 중복 등록하지 않았다. DEPT_3은 대상에서 제외했다.

## 문제와 구현

기존 스캔은 상대경로로 문서를 찾고 새 경로를 신규 문서로 만든 다음 이전 경로를 삭제했다. 이 순서에서는 이동만 해도 문서 ID와 색인이 바뀔 수 있다. 변경된 흐름은 **완전한 스캔의 모든 배치를 받은 다음 같은 source의 이동을 대조하고, 남은 신규·삭제를 확정**한다.

| 관찰 결과 | 처리 |
| --- | --- |
| 동일 source, 단일 파일 식별자, 동일 암호화 SHA-256·크기, 완료된 활성 색인 | 기존 문서 ID와 활성 청크 세트를 유지하고 경로·파일명을 갱신한다. 새 파싱·임베딩 job은 만들지 않는다. |
| 동일 파일 식별자, 암호화 내용 변경 | 기존 문서 ID와 새 위치를 연결하고 안정화 관찰 후 새 ingestion job을 만든다. |
| 같은 경로에서 mtime만 변경, 암호화 내용 동일 | 완료된 활성 색인을 재사용한다. |
| 복사 또는 파일 ID 충돌 | 별도 문서로 처리한다. 해시 일치만으로 합치지 않는다. |
| 식별자 누락·불완전 스캔·처리 중 문서·source 간 이동 | 보수적인 기존 처리 또는 안전한 거부 경로를 사용한다. source 경계는 유지한다. |

Windows 스캐너는 볼륨 일련번호, 파일 ID, 생성 시각을 `host_file_id`로 관찰한다. 서버는 서명된 스캔의 `worker_id`를 붙여 같은 source에서만 비교한다. 하드 링크와 ID 조회 실패는 이동 식별에서 제외한다. 기존 provider용 `source_object_id`는 보존하고 별도의 `host_file_identity`를 문서 mapping과 스캔 항목에 저장한다. 기존 문서는 정상 완료 스캔으로 ID를 연결하며, 연결만으로 재인덱싱하지 않는다.

이동 처리 변경 파일: `tools/windows/DocMindUEncryptorHostWorker.Common.ps1`, `tools/windows/Test-DocMindUEncryptorHostWorker.ps1`, `app/api/db/db_models.py`, `app/api/apps/services/docmind_reconciliation_service.py`, `app/test/unit_test/api/apps/services/test_docmind_reconciliation_service.py`.

대량 발견 후 지원 형식부터 수집하도록 `tools/windows/Start-DocMindUEncryptorHostWorker.ps1`의 선택 인자와 서명된 claim 본문, `app/api/apps/restful_apis/docmind_api.py`의 입력 검증, `app/api/apps/services/docmind_ingestion_service.py`의 후보 조회·재조회 필터를 추가했다. `~$` Office 잠금 임시파일은 선택하지 않고, 확인된 영구 오류 `PARSER_PLATFORM_HWP_DISABLED`와 `PARSER_SOURCE_TYPE_MISMATCH`는 자동 재시도 예약에서 제외한다. 필터를 지정하지 않은 기존 worker 동작은 유지한다.

## 단계별 검증

| 단계 | 결과 | 검증 범위 |
| --- | --- | --- |
| Windows 파일 ID 수집 | 통과 | 비민감 합성 NTFS 파일의 동일 볼륨 이동은 ID 유지, 복사는 다른 ID. 호스트 worker self-test 통과. |
| 스캔·이동·복사 회귀 | **35개 통과** | 1 GiB 제한의 별도 테스트 컨테이너와 격리 SQLite DB. DEPT_1·DEPT_2·TEST1 source 이름, source 경계, 이름·폴더 이동, 배치 분리, 수정 후 job 생성, 복사·충돌·불완전 스캔 포함. |
| 선택 수집·재시도 회귀 | **88개 통과** | ingestion/reconciliation 격리 테스트. 허용 확장자, HWP/HWPX 및 `~$` 미선택, 후보 재조회, 기존 영구 실패 재시도 제외를 검증. |
| 정적·변경 검사 | 통과 | 변경한 reconciliation 서비스·테스트의 `ruff check --select E,F,I --ignore EXE002,E712`, Python 구문 검사, `git diff --check`. `E712`는 기존 Peewee 비교식, `EXE002`는 Windows 마운트 실행 비트에 대한 예외다. |
| 호스트 설정 확인 | 확인 | 세 대상의 암호화 루트가 호스트 설정에 있고 접근 가능하다. TEST1 루트는 사용자 지정 D: 경로와 일치하며 `U:\`도 존재한다. |
| 배포 확인 | 완료 | API 컨테이너 재시작, `host_file_identity` DB 열 및 실제 스캔 기록 확인. 웹 HTTP 200, 보호 API의 미인증 요청 HTTP 401, UI entry 해시 유지. DEPT_1·DEPT_2·TEST1 watcher 가동. 형식 제한 Host Worker의 첫 signed claim이 XLSX를 선택. HWP/HWPX 미선택. |

## source별 운영 상태

| source | 호스트 설정·감시 | 실제 이동 E2E |
| --- | --- | --- |
| DEPT_1 (`dept-1-e2e`) | 원본 루트와 discovery watcher 가동 | 미실행 |
| DEPT_2 (`dept-2-e2e`) | watcher 가동. 382개 파일 전체 스캔에서 중복 job 0. 전용 자정 정합성 작업은 비활성. 지원 형식 수집 중 | 미실행 |
| TEST1 (`home-test1-e2e`, 사용자 `U:\`) | D: 암호화 루트와 discovery watcher 가동 | 미실행 |

## 제한과 다음 단계

실제 uDrive 이동 전후 파일 ID 유지, 새 폴더 검색, 문서 ID·활성 청크 세트 유지 여부는 아직 검증하지 않았다. 다른 에이전트가 실패한 Excel 작업을 복구하는 동안 운영 원본에 합성 파일을 쓰거나 이동시키지 않았다.

DEPT_2 감시를 처음 가동하자 기존 원본에서 작업 239건이 등록됐다. 이는 새 파일이 239개 생성됐거나 이동됐다는 의미가 아니다. 첫 처리에서 신규 COMPLETE 2건, HWP 파서 비활성화 실패 1건, 165바이트 `~$` Office 잠금 임시파일의 형식 불일치 실패 1건을 확인했다. 두 실패의 임시 파일 정리는 완료됐고 작업 기록은 보존했다. HWP 파서는 현재 환경에서 비활성이고 RHWP sidecar도 실행되지 않는다. 사용자는 HWP 활성화를 다음 날 직접 다루기로 하고, 지금은 처리 가능한 형식부터 진행하도록 지시했다.

이에 감시를 다시 켠 뒤 전체 스캔의 중복 등록 0건을 확인하고, Host Worker에 허용 형식 목록을 영속 설정했다. 필터 적용 직전 미처리 234건 중 HWP 138·HWPX 3건을 제외한 **93건(XLSX 36, PPTX 32, PDF 24, DOCX 1)**이 이번 처리 대상이다. 중간 관찰에서 DEPT_2는 DISCOVERED 230, PARSING 1, COMPLETE 6, FAILED 2, CLEANUP 1이었다. HWP/HWPX 141건은 모두 DISCOVERED 상태로 남았고, 기존 FAILED 2건은 그대로였다. API 메모리는 3.55/4 GiB, OOM 및 재시작 기록은 0이었다. 이는 완료 결과가 아닌 처리 중 스냅샷이다.

현재 재사용 증거는 **동일한 암호화 SHA-256·크기**다. 암호문만 바뀌고 복호화 내용이 같은 재암호화는 재인덱싱 경로로 간다. 확장자 변경도 파서 종류가 달라질 수 있어 신규 문서 처리로 돌아간다. 이 두 경우의 색인 재사용은 추가 설계·검증 전까지 주장하지 않는다.

지원 형식 작업이 끝날 때까지 완료·실패 건수, 정리 상태, API 메모리와 OOM을 관찰한다. 이후 HWP 제외 정책을 유지한 채 각 source에서 비민감 합성 파일로 최초 색인 → 완료 스캔을 통한 ID 연결 → 상위·하위 이동/이름 변경 → 내용 수정 → 복사 순서로 확인한다. 각 단계에서 문서 ID, 활성 청크 세트 ID, 새·이전 폴더 검색 범위, 추가 job 수, 임시 평문 정리 상태를 기록한다. HWP 활성화·재처리는 별도 후속 작업이다.
