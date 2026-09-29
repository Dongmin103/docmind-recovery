# Excel 수집 실패 복구 — 2026-09-28

## 범위

`PLAN-SOURCE-MOVE-AND-UPDATE.md`의 1번: 실패한 수집 작업의 안전 정리·재시도·UI 상태 표시. 이동 시 색인 재사용(2번 이후)은 구현하지 않았다. 원본 문서와 클라우드 저장소는 수정하지 않았다.

## 원인과 관찰

- 이동 후 문서 `3ee63480f91447a983237d5e5f5cd138`, 작업 `45326ad90a5fc3467fb2c8c4c462bc7a`.
- 16:38:04 파싱 시작, 16:38:34 API 서버 exit 137, 16:39:49 HOST_WORKER_ERROR로 FAILED.
- Windows 평문은 정리됐지만 컨테이너 정리 PENDING이라 재시도가 차단됐고, UI는 이를 ‘인덱싱 대기’로 표시했다.
- 당시 cgroup OOM 종료 기록이 있었다. API/admin/task executor/generic datasync가 4 GiB 한도를 공유했다. 진단을 위한 추가 앱 전체 import가 메모리 압박에 기여했을 가능성도 있으며 Excel 자체 결함으로 단정하지 않는다.
- 후속 점검 시 컨테이너 시작 시각은 16:57:06으로 바뀌어 있었고 tmpfs 입력은 이미 없었다. 이번 수정 적용 전에 다른 작업에서 재시작된 것으로 보이나 수행 주체는 확인하지 않았다. 이 세션은 당시 임시 파일을 직접 삭제하지 않았다.

## 수정

1. 임시 입력 소비 동안 OS 파일 잠금 유지. reaper와 명시적 cleanup 모두 잠긴 작업 삭제를 거부.
2. 검증된 종료 작업은 긴 일반 TTL을 기다리지 않고 version/fencing·호스트 정리·파일 잠금 조건을 확인한 후 정리.
3. tmpfs가 재시작으로 사라진 경우, 만료된 작업의 토큰 해시에 대응하는 정확한 경로 부재를 확인하여 정리 상태 복구. 활성 lease/남은 경로/불확실한 root는 거부.
4. 일반 호스트 claim 유지보수에서 안전 정리 후 제한 재시도 예약. 기존 최대 8회, backoff, source 경계와 삭제 제외 유지.
5. 최신 수집 작업 상태를 검색 가능 여부와 분리. 실패·처리 중·정리·재시도·확인 필요를 구분하고 허용한 오류 코드만 노출.

## 시험 환경 적용

- 기존 개발 Compose 6개 파일에 `app/docker/docker-compose-windows-dev-ingestion-recovery.yml`을 추가.
- 등록된 범용 connector 0개를 확인한 뒤 `--disable-datasync` 적용. Windows DEPT 감시와 task executor, admin, 검색, parser sidecar는 유지.
- reaper 유지보수 주기는 60초. 활성 작업 안전 조건이나 일반 TTL을 일괄 완화하지 않았다.
- 서버 코드는 기존 읽기 전용 source overlay와 동일한 방식으로 적용했으며 `ephemeral_input.py`도 일치하는 버전으로 마운트했다.
- Docker 이미지 빌드는 로컬 I/O 지연으로 중단했고, 기존 검증 이미지와 개발용 source mount로 먼저 적용했다. 완료되지 않은 이미지를 배포했다고 보고하지 않는다.

## 검증

- 격리 컨테이너 backend 109개 통과: ephemeral 13 / ingestion 44 / reconciliation 24 / projection 28.
- frontend 관련 Jest 25개, 변경 파일 oxlint 통과.
- backend Ruff 통과(NTFS bind의 실행 비트로 인한 EXE002 제외).
- 전체 frontend typecheck는 변경 범위 밖 오류로 통과하지 못했고 변경 파일 경로에서는 진단이 없었다.
- frontend 기존 의존성 폴더에 누락 모듈이 있어 원본을 보존하고 별도 ignored 디렉터리에 lockfile 기준 npm ci를 수행했다. 2 GiB Node heap 빌드는 실패했으며 이는 문서 파싱 실패와 별개다.

## 실제 복구 타임라인 (KST)

- 17:16:56 수정된 API 기동.
- 17:17:29 수동 DB 수정 없이 일반 host claim 유지보수가 대상 작업을 RETRY_WAIT로 전환, fencing 1→2.
- 재시도 예약 시각 17:19:09, 실제 17:19:13에 attempt 2 / fencing 3으로 파싱 시작.
- 17:20:31 COMPLETE, 활성 청크 40개. 실행 약 78초.
- 새 ParserRun `6720f025f06da59f75cbaf2c54120731` READY, active chunk set `3be5ee0bc17d85f20a17c795a904b319`.
- Windows와 parser cleanup 모두 COMPLETE. Windows 작업 루트 파일 0, 대상 컨테이너 manifest/임시 입력 없음.
- 재기동 후 cgroup OOM 및 oom_kill 0. 일반 claim이 자동 복구했으며 직접 SQL UPDATE나 강제 성공 처리는 하지 않았다.
- 문서 단독 및 새 폴더 scope 검색 각각 후보 8 / 반환 5 / 대상 발견 / 다른 문서 0. 이전 위치 문서는 검색 목록에서 제외됐고 같은 이름의 활성 문서는 1개.
- API 상태 INDEXED, cleanup COMPLETE, searchable=true. 원본 S: 및 서버 암호화 파일 해시는 복구 전후 동일.

최종 검색·임시 평문 정리 검증: 통과. 화면 빌드·배포·표시 검증: 통과.

- 별도 lockfile 설치 환경에서 Node heap 4096 MiB / esbuild / sourcemap 비활성으로 frontend 빌드 성공(1분 1초). entry는 `index-DXCj3nj-.js`.
- `.local/frontend-ingestion-recovery/dist`의 리소스를 현재 컨테이너에 먼저 복사하고 `index.html`을 마지막에 적용. API/backend 재시작 없이 HTTP 200과 새 entry 제공을 확인.
- 기존 열린 상세 화면을 새로고침하여 ‘인덱싱 완료’, `Total 40`, 청크 표시를 확인했다. 진행 중/실패 등의 다른 상태는 단위 테스트로 검증했고 실제 작업을 재실행해 모두 재현하지는 않았다.
- 이 UI 적용은 현재 컨테이너에 복사한 개발용 산출물이다. 컨테이너 재생성 시에는 검증 dist를 다시 적용하거나 새 이미지를 빌드해야 한다. 다른 세션이 bind-mounted backend 파일을 편집하고 있어 이를 임의 재시작하지 않았다.

## 벡터 저장소 읽기 전용 점검

- 이동 전 문서: ES 청크 40개, 모두 `available_int=0`, DB `status=0`. 검색 제외됐으나 물리적으로 보관 중이다.
- 현재 문서: ES 청크 40개, 활성 chunk set과 일치. 앞선 문서/폴더 범위 실검색도 통과했다.
- 실패 ParserRun `2391a50cf0a19fb0a7cdd2c93c3a56a4`: ES 청크 0개. 다만 `QUEUED` run/task metadata가 남아 있다.
- 이전 문서에는 보관 만료 `2026-10-28 07:37:44 UTC`가 지정됐지만 ParserRun은 `READY`, 문서 active pointer도 남아 있다. `cleanup_expired`는 RETAINED/FAILED 계열만 선택하고 active pointer를 제외하므로 현재 상태로는 이 문서를 정리하지 않는다.
- 만료 청소 함수의 호출도 일반 task executor 활성화 뒤에 있고, source 임시 입력 활성화 경로에는 같은 호출이 없다. 따라서 삭제된 source 데이터의 30일 후 자동 물리 삭제가 완성됐다고 판단할 수 없다.
- 이 점검에서 DB/ES 삭제·상태 수정은 하지 않았다. 보관 정책에 맞춘 source 삭제 만료 처리와 고아 run 정리가 별도 후속 과제다.

## 남은 설계 한계

- 파싱은 아직 API 요청 수명 안에서 실행한다. 별도 영속 작업 실행기로의 분리는 이번 수정에 포함하지 않는다.
- 원인이 확인된 비활성 과거 ParserRun/task 기록은 임의 삭제하지 않았다. 새 처리에는 새 ParserRun을 사용한다.
- Windows 파일 ID 기반 이동 최적화, 클라우드 원본 쓰기 API 활성화, 부서 간 이동 최적화는 이번 범위 밖이다.
