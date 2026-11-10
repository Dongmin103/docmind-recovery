# 개발자 실행·인수 확인 절차

목표: **합성 DOCX 한 건으로 원본 복호화 → 파싱 → 청킹 → 임베딩 → 활성 색인 → 임시 파일 정리 확인**. 기존 문서·색인을 지우지 않는다. 이 절차는 담당자가 인수 때 실행하는 절차이며, 아래 기대 결과를 이번에 새 색인해 얻었다는 뜻은 아니다.

필수 전제: [실행 자산](ASSETS.md), [설정](CONFIGURATION.md), 준비된 API·DB·파서·임베딩, Windows 실행 계정과 정상 uEncryptor2, 실제 암호화된 합성 표본. 새 서버에서 이 전제가 비어 있으면 먼저 자산 수령·초기화를 한다.

## A. 현재 서비스와 도구 준비 — Ubuntu VM

```bash
sudo /opt/docmind-avdc/manage.sh ps
```

기대: 8개 서비스가 running. healthcheck가 정의된 서비스는 healthy. 시작이 필요한 준비된 설치에서만 `sudo /opt/docmind-avdc/manage.sh up -d`를 실행한다.

Git checkout의 `tools/handoff`를 API 컨테이너의 임시 경로에 복사한다. 아래 `DOCMIND_REPO`는 실제 clone 경로다. **쉘은 Ubuntu Bash**다.

```bash
DOCMIND_REPO=/path/to/docmind
API_CONTAINER=$(sudo /opt/docmind-avdc/manage.sh ps -q docmind)
test -n "$API_CONTAINER" || exit 1
sudo docker exec "$API_CONTAINER" test ! -e /tmp/docmind-handoff || exit 1
sudo docker cp "$DOCMIND_REPO/tools/handoff" "$API_CONTAINER":/tmp/docmind-handoff
```

한 세션에서 다음 함수로 도구를 호출한다. 새 터미널을 열면 변수와 함수를 다시 선언한다.

```bash
dm_handoff() {
  sudo docker exec -w /ragflow -e PYTHONPATH=/ragflow "$API_CONTAINER" \
    /ragflow/.venv/bin/python /tmp/docmind-handoff/engine.py "$@"
}
dm_handoff projects
```

기대: `project_id`, `dataset_id`, `mode: database`, source 개수 목록. 비어 있으면 초기 작업공간이 없는 설치다. 임의 ID를 만들지 않는다. 신규 Windows 패키지는 해당 패키지 초기화 절차를 사용하고, 기존 AVDC에서는 기존 프로젝트 ID를 인계받는다.

## B. 프로젝트·폴더·source 최초 등록

기존 공용 작업공간을 사용하는 API에서 workspace가 아직 없다면, 기존 공유 사용자와 모델 등록이 먼저 완료된 상태에서 아래 기존 서비스를 사용한다. 이 명령은 **필요한 신규 설치에서만** 실행하며 기존 모델·사용자를 새로 만들지 않는다.

```bash
sudo docker exec -w /ragflow -e PYTHONPATH=/ragflow "$API_CONTAINER" /ragflow/.venv/bin/python -c \
  'from common import settings; settings.init_settings(); from api.apps.services.docmind_shared_workspace_service import resolve_user; from api.apps.services.docmind_bootstrap_service import bootstrap_shared_workspace; import json; print(json.dumps(bootstrap_shared_workspace(resolve_user().id)))'
```

기대: `project_id`, `dataset_id`, `created`, `catalog_initialized`. 빈 workspace의 `catalog_initialized: false` 자체는 실패가 아니다. 공유 사용자/tenant 임베딩 미설정 오류는 설치 초기화에서 해결한다. 이를 우회해 임의 SQL로 프로젝트를 만들지 않는다.

다음은 **기존 프로젝트에 새로운 시험 source와 기본 폴더만** 추가한다. 모델·tenant·기존 문서·색인은 바꾸지 않는다.

```bash
PROJECT_ID='<projects에서 확인한 ID>'
SOURCE_ID=handoff-sample-20261110
dm_handoff register-source --project "$PROJECT_ID" --source "$SOURCE_ID" --display-name '엔진 인수 합성 시험'
# action: would_create와 project/source를 확인한 뒤에만 실행
dm_handoff register-source --project "$PROJECT_ID" --source "$SOURCE_ID" --display-name '엔진 인수 합성 시험' --apply
```

기대: 첫 실행 `created`, 같은 등록을 다시 요청하면 `unchanged`. 비활성/다른 프로젝트/이름 충돌은 오류로 멈추며 기존 값을 덮어쓰지 않는다. 여러 source는 서로 다른 기본 폴더 ordinal로 등록한다. 경로 추가는 다음 Windows 설정에서 수행한다.

## C. 합성 입력 준비 — Windows

Python 3가 있는 checkout 루트에서 원본 업무 폴더 밖의 전용 작업 디렉터리를 만들고 실행한다. 출력 파일이 이미 있으면 덮어쓰지 않는다.

```powershell
New-Item -ItemType Directory -Path 'D:\DocMind\management\handoff-sample-20261110' -ErrorAction Stop
python .\tools\handoff\make_sample.py --output 'D:\DocMind\management\handoff-sample-20261110\sample.docx'
```

출력의 `plaintext_sha256`을 인수 기록에 저장한다. 문서에는 `DOCMIND-HANDOFF-20261110` 표식과 비민감한 확인 문장만 들어 있다.

**이 DOCX는 아직 평문이다.** uDrive 등 기존 제품의 검증된 업로드·암호화 경로로 시험용 저장 폴더에 넣어 암호화 원본을 준비한다. `/enc` 등 확인되지 않은 CLI 옵션을 추측해서 실행하지 않는다. 이미 공급자가 준비한 합성 암호문/평문 한 쌍이 있으면 그 파일과 독립적으로 구한 평문 SHA256을 써도 된다. 암호화 준비가 안 됐으면 여기서 미준비로 기록하며 복호화 검증을 건너뛰고 전체 합격으로 처리하지 않는다.

원본 암호문의 SHA256을 기록한다.

```powershell
$encryptedSample = '<암호화가 확인된 sample.docx 전체 경로>'
$cipherBefore = (Get-FileHash -LiteralPath $encryptedSample -Algorithm SHA256).Hash
```

## D. 시험용 워커 설정 연결

[설정표](CONFIGURATION.md)에 따라 기존 인증·복호화 설정을 사용하는 별도 `handoff-worker.private.json`을 Git 밖에 만든다. `sources`는 시험 source 하나와 **암호화된 합성 표본만 있는 루트**로 제한한다. 평문 DOCX를 원본 루트에 직접 넣지 않는다.

- `source_id`는 B의 `SOURCE_ID`와 같게 한다.
- `work_root`, `cleanup_receipt_root`, `sync_state_root`는 다른 작업과 분리한 시험 경로를 쓴다.
- `decrypt_control_root`는 같은 호스트의 기존 워커와 공유한다.
- 기존 색인 워커와 인수 워커를 중복 실행하지 않는다. 진행 중이면 기존 작업 정리·종료를 확인한 뒤 시험한다.
- 현재 계정의 실제 uEncryptor2 해시·서명, 임시 볼륨 보호·백업 제외 조건을 확인한다.

별도 Windows PowerShell 세션 두 개에서 실행한다. 모두 **checkout 루트 기준**이다.

```powershell
# 세션 1: 최초 조사와 이후 안정화 관찰
$handoffConfig = 'D:\DocMind\management\handoff-sample-20261110\handoff-worker.private.json'
$watchStop = 'D:\DocMind\management\handoff-sample-20261110\watch.stop'
powershell.exe -NoProfile -File .\tools\windows\Watch-DocMindEncryptedSources.ps1 -ConfigPath $handoffConfig -SourceId handoff-sample-20261110 -StopFile $watchStop
```

```powershell
# 세션 2: 인수 확인은 1슬롯. 성능 측정이 아님
$handoffConfig = 'D:\DocMind\management\handoff-sample-20261110\handoff-worker.private.json'
$ingestStop = 'D:\DocMind\management\handoff-sample-20261110\ingest.stop'
powershell.exe -NoProfile -File .\tools\windows\Start-DocMindIngestionPool.ps1 -ConfigPath $handoffConfig -Concurrency 1 -ClaimSourceId handoff-sample-20261110 -StopFile $ingestStop
```

기존 stop 파일이 있으면 새 시도 경로를 정한다. 감시기는 안정화·독립 관찰 때문에 바로 작업을 만들지 않을 수 있다. 수정 이벤트는 기본 120초 안정화와 이후 독립 관찰을 거치므로 생성 직후 0건만 보고 실패로 결론 내리지 않는다. 초기 스캔·세션 복구 대기는 별도다. `-Once`를 붙여 전체 조사 완료를 가정하지 않는다.

## E. 색인·정리 합격 확인 — Ubuntu와 Windows

Ubuntu A의 Bash 세션에서:

```bash
dm_handoff jobs --source "$SOURCE_ID"
JOB_ID='<이번 합성 문서의 job_id>'
PLAIN_SHA256='<C에서 저장한 평문 SHA256>'
dm_handoff check-job --job "$JOB_ID" --expected-plaintext-sha256 "$PLAIN_SHA256"
```

정상 기대값:

```json
{"state":"COMPLETE","error_code":null,"indexed_chunks":1,"checks":{"job_complete":true,"server_cleanup":true,"host_cleanup":true,"active_version":true,"active_mapping":true,"active_document":true,"parser_ready_clean":true,"es_count_matches":true,"plaintext_matches_fixture":true},"passed":true}
```

예시의 `indexed_chunks: 1`은 고정 합격 숫자가 아니다. 실제 수가 1 이상이고 파서 staged count와 같아야 한다. `passed: true`, 모든 checks가 true, 종료 코드 0이 도구 합격 기준이다. 진행 중/불일치면 종료 코드 2, 설정/실행 오류는 1이다. 도구는 원문·벡터·비밀값을 출력하거나 상태를 고치지 않는다.

**도구 결과만으로 디스크의 모든 평문 부재를 증명하지 않는다.** 워커와 감시기를 다음 절차로 정상 종료한 뒤, 전용 시험 경로에서 잔존 파일을 별도로 확인한다.

```powershell
New-Item -ItemType File -Path 'D:\DocMind\management\handoff-sample-20261110\watch.stop'
New-Item -ItemType File -Path 'D:\DocMind\management\handoff-sample-20261110\ingest.stop'
```

두 프로세스의 정상 종료를 확인한다. 강제 종료하면 아직 정리 중인 작업을 끊을 수 있다. 아래 `work_root`와 영수증 루트는 **시험 전용** 설정에서 읽는다. 풀은 슬롯별 하위 디렉터리를 사용하므로 재귀적으로 확인한다.

```powershell
$cfg = Get-Content -LiteralPath 'D:\DocMind\management\handoff-sample-20261110\handoff-worker.private.json' -Raw | ConvertFrom-Json
Get-ChildItem -LiteralPath $cfg.work_root -File -Recurse -Force -ErrorAction Stop | Select-Object FullName,Length
Get-ChildItem -LiteralPath $cfg.cleanup_receipt_root -File -Recurse -Force -ErrorAction Stop | Select-Object FullName,Length
$cipherAfter = (Get-FileHash -LiteralPath $encryptedSample -Algorithm SHA256).Hash
if ($cipherBefore -ne $cipherAfter) { throw '원본 암호문 해시가 변경되었습니다.' }
```

기대: 평문 작업 파일·미처리 receipt 없음, 원본 해시 동일. 폴더가 없거나 접근할 수 없으면 확인 불가로 기록하고 0개로 간주하지 않는다. 슬롯 경로가 설정 루트와 다르면 기존 `Get-DocMindWorkerRolePaths`로 실제 경로를 확인한다. 시험 결과는 보관하고 운영 색인 삭제 명령을 실행하지 않는다.

UI의 해당 문서 상세에서 확인 표식과 본문을 눈으로 대조한다. 원문 미리보기는 별도 워커가 필요하므로 청크 본문 확인과 구분한다. KURE 검색까지 검증할 때는 외부 연결·인증이 준비된 후 [API 명세](API.md)와 인수인계서의 검색 예제로 별도 기록한다. 이번 색인 합격만으로 검색·답변 전체 합격이라고 적지 않는다.

## F. 인수 기록 양식

```text
실행자 / 날짜:
Git SHA / 이미지 ID / 모델 SHA256 일치 여부:
서버 / 프로젝트 / 시험 source ID:
설정: 문서 슬롯 / 파서 / 임베딩 / CPU·메모리 한도:
암호문 SHA256 전후 동일 여부:
평문 fixture SHA256 일치 여부:
job_id / document_id / 활성 청크 수:
check-job 종료 코드 / passed / 실패 checks:
전용 평문·receipt 잔존 파일 수:
UI 확인 표식·본문 일치 여부:
검색 시험: 미실행 또는 별도 결과:
시작·마지막 활성화·정리 완료 시각(계측했을 때만):
미완료 사항 / 담당자:
```

이번 문서 보완에서 도구는 격리 SQLite 시험, AVDC 기존 완료 작업의 읽기 검증, source 등록 dry-run을 수행했다. 위 합성 문서를 새로 암호화·색인하는 종단 간 인수 시험은 수행하지 않았다. 기존 154개 실측과 새 도구 검증 결과를 혼합하지 않는다.
