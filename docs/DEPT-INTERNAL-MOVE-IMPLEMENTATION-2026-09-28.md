# DEPT_1·DEPT_2·TEST1 내부 이동 색인 재사용 구현 및 검증

2026-09-28 기준. 실제 서비스 반영·원본 이동 E2E 완료 보고가 아니다.

## 구현

- Windows 전체 스캔은 볼륨 일련번호, 파일 ID, 생성 시각으로 구성한 `host_file_id`를 전송한다. 하드 링크와 파일 ID 조회 실패는 이동 최적화에서 제외한다.
- `source_object_id`와 별도인 `host_file_identity`를 문서 mapping과 스캔 항목에 저장한다. 서버는 서명된 스캔의 `worker_id`를 붙이고 같은 source에서만 비교한다.
- 완전한 스캔의 모든 배치가 도착한 뒤 이동을 대조하고, 남은 신규/삭제를 처리한다. 동일 파일 ID가 하나만 존재하고 완료된 활성 색인이 있으며 원본 암호화 해시·크기가 같으면 문서 ID와 활성 청크 세트를 유지한다. 파일명·검색 경로는 새 위치로 바꾼다.
- 내용 해시가 바뀌면 기존 문서 ID를 유지하면서 안정화 관찰 후 새 ingestion job을 만든다. 복사, 파일 ID 충돌, 불완전 스캔, 처리 중인 문서, 다른 source는 이동으로 합치지 않는다.
- 기존 mapping의 파일 ID는 정상 완료 스캔으로 연결한다. 연결 자체가 재인덱싱을 만들지 않는다.

## 검증

- 별도 메모리 제한 1 GiB 테스트 컨테이너에서 reconciliation 단위 테스트 **35개 통과**. DEPT_1·2·TEST1 source 이름 각각, source 간 경계, 폴더 이동의 배치 분리, 내용 수정 후 job 생성, 복사, 충돌, 불완전 스캔을 포함한다.
- Windows 호스트 worker self-test 통과. 합성 NTFS 파일의 이동은 파일 ID 유지, 복사는 다른 ID를 확인했다.
- `ruff check --select E,F,I --ignore EXE002,E712`를 변경한 reconciliation 서비스·테스트에 적용해 통과했다. `E712`는 기존 Peewee 질의 관례이고, `EXE002`는 Windows 마운트의 실행 비트 표시다.
- `git diff --check` 통과.

## 운영 적용 전 남은 확인

- 사용자의 최신 범위는 DEPT_1, DEPT_2, TEST1이다. 사용자 확인에 따르면 TEST1의 `D:\uPLEXsoft\uDrive\USER\test1` 암호화 저장 루트는 `U:\`와 연결된다. 호스트 설정의 `home-test1-e2e`가 이 루트를 가리키는 것은 확인했고, TEST1 전용 discovery watcher와 자정 정합성 작업도 있다. DEPT_2에는 discovery 활성 watcher·전용 자정 정합성 작업이 보이지 않는다. DEPT_1은 둘 다 있다.
- uDrive를 통한 실제 이동 전후 파일 ID 유지와 각 source의 검색/청크 ID 유지 E2E는 실행하지 않았다. 실패한 Excel 복구 작업과 동시에 운영 원본을 움직이지 않기 위해 합성 NTFS·격리 DB에서만 검증했다.
- 암호화 해시가 달라졌지만 복호화 내용이 같은 재암호화는 재인덱싱 fallback이다. 확장자 변경도 신규 문서 fallback이다. 같은 암호화 내용에서 mtime만 바뀌면 활성 색인을 재사용한다.
- DB 열 추가와 서비스/호스트 worker 코드 배포가 필요하다. 운영 서비스는 이 작업에서 재시작하지 않았다.
