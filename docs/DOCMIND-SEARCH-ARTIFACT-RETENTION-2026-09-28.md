# DocMind 검색 청크·벡터 24시간 만료 정리

코드 상태: 구현 및 합성 데이터 회귀 검증 완료. 운영 배포·활성화·실데이터 삭제는 수행하지 않았다.

## 범위와 시점

- 클라우드 원본 삭제가 확정되면 문서를 즉시 검색에서 제외한다. 이후 검색 제외 시각과 `ParserRun.retained_until`이 모두 지난 청크·벡터만 물리 삭제한다. 새 삭제 건의 검색 artifact deadline은 24시간이며 삭제 tombstone과 source/version metadata는 기존대로 30일 보존한다.
- 내용 교체로 이전 청크 세트가 `RETAINED`가 된 경우, 새 활성 버전의 색인 정리가 완료되고 새 버전 활성화 후 24시간 및 이전 run의 deadline이 모두 지나야 정리한다. 신규 DocMind 교체 run의 deadline은 24시간이다. 이전에 지정된 7일·30일 deadline은 소급 단축하지 않는다.
- 삭제 대상은 해당 parser run·chunk set으로 식별한 ES 청크 본문과 벡터, `ParserRun`, 연관 `Task`다. 암호화 원본, 복호화 키, DB/ES 볼륨, 현재 활성 버전, source/version metadata, 삭제 tombstone, 대화 이력은 삭제 대상이 아니다. 평문 임시 작업 공간은 기존 별도 수명주기 정리가 담당한다. raw artifact 참조가 남아 있으면 이 정리는 건너뛴다.

## 안전 조건

문서가 `docmind_cloud` source이고 등록 source mapping을 찾을 수 있어야 한다. 삭제 건은 mapping 삭제 상태, 문서 검색 비활성 상태, source version들의 삭제 보관 상태, 완료된 검색 제외 tombstone을 모두 요구한다. 교체 건은 이전 run/version의 `RETAINED` 상태, 현재 활성 chunk set과 다른 ID, 새 version의 활성 및 색인 정리 완료를 요구한다. 진행 중이거나 재시도 대기 중인 ingestion job, 유효한 preview, 활성 reader가 있으면 건너뛴다. 활성화·rollback도 비활성 문서를 거부한다.

정리 직전 문서 행을 잠그고 조건을 다시 검사한다. run/set 식별 필드가 없는 옛 청크가 문서에 있으면 실패 처리하고 운영 확인을 기다린다. ES 삭제 뒤 같은 조건의 검색 결과가 0건인지 확인한 다음 run/task metadata를 지운다. ES 장애나 잔여 청크가 있으면 DB transaction은 rollback되고 다음 회차에 재시도한다. 외부 ES 삭제는 DB와 하나의 분산 transaction이 아니므로 장애 중간에 ES만 먼저 삭제될 수 있다. 같은 식별자로 재시도할 수 있으며 남아 있는 metadata는 복구 근거다.

## 실행

`DOCMIND_RETENTION_PURGE_ENABLED=1`을 명시한 API 배포에서만 정리 loop와 신규 24시간 deadline 정책이 시작된다. 기본값은 비활성이며 기존 7일 교체·30일 삭제 deadline과 공통 정리 동작이 유지된다. OFF 상태에서는 새 정리 모듈도 불러오지 않는다. ON 상태에서는 5분마다 최대 100건 삭제를 시도하고 한 회차에 최대 400개 후보를 검사한다. 순환 cursor로 건너뛴 후보도 재검사한다. 따라서 24시간은 최소 보관 기간이며 작업 지연·대량 backlog·오류 시 실제 삭제는 늦어질 수 있다. 호스트 워커 claim이나 00:00 source scan에 의존하지 않는다.

활성화 전에는 대상 이미지와 실제 ES 색인 필드(`parse_run_id`, `chunk_set_id`)를 합성 데이터로 확인하고, 기존 보관분의 deadline 및 예상 후보 수를 읽기 전용으로 검토해야 한다. 설정을 켜는 작업은 별도 배포 단계다. 이번 작업에서 운영 설정이나 서비스를 변경하지 않았다.

## 최종 검증 — 2026-09-28

- SOL 서브에이전트 구현 후 주 에이전트가 기본 OFF/lazy import/기존 deadline 보존/활성 SourceVersion 참조 보호를 검토했다.
- 1 GiB 제한, 읽기 전용 소스 마운트, `--rm` 격리 컨테이너에서 reconciliation·retention·chunk-set activation 회귀 **47개 통과**. SQLite 및 ES 대역을 사용한 테스트이며 운영 MySQL/ES에서 실제 삭제한 E2E가 아니다.
- 관련 Ruff E/F/I/DTZ 검사(기존 Peewee E712 예외) 및 `git diff --check` 통과. 테스트 컨테이너는 종료 후 제거됐다.
- 실행 중 API 컨테이너를 읽기 전용 확인하여 `DOCMIND_RETENTION_PURGE_ENABLED=1`이 설정되지 않았음을 확인했다. `ingestion_service.py`의 claim/retry 변경은 이 작업에 포함하지 않았다.

재현 명령(프로젝트 루트, Windows Docker wrapper 사용):

```powershell
& C:\Users\uplex\bin\docker.cmd run --rm --name docmind-retention-unit-20260928 --memory 1g --entrypoint uv -v /mnt/c/DocMindDev/docmind/app:/workspace:ro -w /workspace -e PYTHONPATH=/workspace docmind-ragflow:preview-20260928-validated run --no-project --with pytest --with pytest-asyncio --python /ragflow/.venv/bin/python python -m pytest -q -c /dev/null -p no:cacheprovider --disable-warnings test/unit_test/api/apps/services/test_docmind_reconciliation_service.py test/unit_test/api/apps/services/test_docmind_retention_service.py test/unit_test/api/db/services/test_chunk_set_activation_db.py
```
