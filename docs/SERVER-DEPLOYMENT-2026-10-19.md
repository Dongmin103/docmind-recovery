# GitHub 기반 다른 서버 배포

현재 DOCMIND 배포 소스는 **기본 브랜치 `main`**에 있다. 초기 인수인계·복구 브랜치 대신 `main`을 받는다. 검색 구현 커밋은 `59a3005`, 평가·기존 색인 보강 도구는 `d5cac9b`에 포함된다. `feat/answer-jobs`는 작업 이력을 보존한 브랜치다.

```bash
git clone --branch main --single-branch https://github.com/Dongmin103/docmind-recovery.git docmind
cd docmind
git rev-parse HEAD
```

실제 배포에 사용한 전체 SHA를 서버 배포 기록에 남긴다. 기존 checkout은 작업 파일과 현재 버전을 확인한 뒤 해당 브랜치를 fetch/checkout한다. 변경 파일을 강제로 덮어쓰는 reset은 필요하지 않다.

## 서버 구성과 이미지

Git에는 애플리케이션 소스, Dockerfile, 설정 예제, 호스트 워커 도구 및 검증 코드가 들어 있다. Docker 이미지·모델 가중치·상용 uEncryptor2·실제 설정과 키·원본·DB·벡터·객체 저장소 데이터는 별도로 준비해야 한다. 현재 PC의 `.local` 디렉터리를 다른 서버의 설정으로 복사하지 않는다.

새 Windows 서버의 기초 구성은 [Windows 개발 안내](../WINDOWS_DEVELOPMENT.md), [Docker 구성](WINDOWS-DEVELOPMENT-DOCKER.md), [GTE 구성](LOCAL-GTE-DEPLOYMENT-2026-10-12.md)을 참고한다. Windows 복호화 실행부와 Docker Linux 서비스의 역할을 유지한다. 기존 개발 Compose의 합성 데이터 보안 경계를 임의로 해제해 운영 구성으로 사용하지 않는다. 서버별 원본 경로, 계정, 비밀값, 모델 주소, 도메인과 TLS를 그 서버에서 설정한다.

전체 소스 이미지 빌드의 context는 `app/`이다. Docker Linux 환경에서 저장소 루트 기준 예시는 다음과 같다.

```bash
docker build -f app/Dockerfile -t docmind-ragflow:git-20261019 ./app
```

이 Dockerfile은 의존 이미지·패키지·모델 자원을 내려받고 프런트도 빌드한다. 해당 서버의 네트워크·메모리·가용 버전을 확인해야 한다. 현재 PC에서 검증한 배포는 기존 이미지에 변경 파일을 반영한 방식이며, 다른 서버에서 위 전체 빌드를 완료했다고 주장하는 기록은 아니다. `Dockerfile.search-documents` 등 과거의 일부 파일 전용 overlay만 빌드하면 이번 브랜치 전체가 반영되지 않는다.

새 이미지를 서버의 실제 Compose에 지정한다. 기존 설치라면 환경변수 값, 마운트, 키, 볼륨, 리소스 설정을 유지하며 API와 프런트를 함께 전환한다. 이 브랜치의 프런트는 비동기 답변 API를 사용한다. 별도 웹 호스팅을 사용하면 그 프런트도 같은 소스에서 빌드·배포한다. 답변 작업용 추가 테이블과 동작은 [답변 배포 기록](ANSWER-JOBS-2026-10-18.md)을 따른다.

## 새 서버에서 다른 원본 폴더를 색인하기

이 절차는 새 서버의 uEncryptor2 암호화 원본을 새로 색인할 때 사용한다. 기존 PC의 루트 경로, source ID, 문서나 색인을 그대로 가져올 필요는 없다. 일반 평문 폴더는 현재 암호화 원본 처리 경로에 바로 연결한다고 가정하지 않는다.

### 1. DOCMIND 쪽 논리 원본 등록

대상 tenant의 프로젝트·데이터셋을 준비하고, 원본마다 다음 등록을 맞춘다. 물리 경로는 호스트 설정에만 둔다.

| 등록 항목 | 의미 |
|---|---|
| `DocmindSource.id` | 원본의 고유 ID. 예: `company-docs`, `department-docs` |
| `project_id` | 대상 서버의 올바른 DOCMIND 프로젝트 |
| `display_name` | 화면에 표시할 원본 이름 |
| `default_folder_id` | 같은 프로젝트의 활성 `DocmindFolder` ID. 신규 발견 문서를 등록할 기본 폴더 |
| `enabled` | 색인할 원본은 활성 상태 |

현재 이 초기 등록을 한 번에 수행하는 전용 설치 명령이나 화면은 제공하지 않는다. 서버 관리자 초기 구성에서 프로젝트·폴더·source 관계를 등록하고 확인해야 한다. `default_folder_id`가 없으면 새 파일을 발견해도 자동 문서 등록이 진행되지 않으므로, 호스트 JSON에 경로만 추가하는 것으로 초기 구성이 끝나지 않는다. 기존 source를 전혀 다른 문서 루트로 재사용하면 이전 문서가 삭제된 것으로 관찰될 수 있으므로, 새로운 문서 집합에는 새 source ID와 새로운 동기화 상태를 사용한다.

### 2. 호스트의 실제 경로 연결

[호스트 워커 예제](../tools/windows/docmind-host-worker.config.example.json)를 Git 밖의 비공개 설정 파일로 복사하고 서버에 맞게 채운다. 예를 들어 두 원본 루트는 다음과 같다. 아래 JSON은 전체 설정 중 `sources` 부분의 예시다.

```json
{
  "sources": [
    { "source_id": "company-docs", "root": "E:\\회사자료" },
    { "source_id": "department-docs", "root": "F:\\부서문서" }
  ]
}
```

위 ID는 DOCMIND 등록과 같아야 한다. 워커 실행 계정이 각 루트와 하위 파일을 읽을 수 있어야 한다. `api_base_uri`는 그 서버 Docker가 공개한 loopback API 주소로 설정하고, 워커 HMAC 인증을 API 설정과 맞춘다. `executable_path`와 승인된 실행 파일 해시·서명은 새 서버의 uEncryptor2 설치를 기준으로 채운다. 임시 평문 작업·정리 receipt·동기화 상태 디렉터리는 원본 루트와 분리하며 설정·키의 ACL과 작업 볼륨 요구사항은 [호스트 워커 안내](UENCRYPTOR2-VALIDATION.md)를 따른다.

### 3. 시작 스캔과 색인 실행

다음은 설정과 API·파서·모델 연결이 준비된 뒤 저장소 루트에서 실행하는 PowerShell 예시다. `C:\DocMindHost\worker.private.json`은 실제 설정 파일 위치로 바꾼다. 감시기와 워커는 서로 다른 PowerShell 세션에서 실행한다.

```powershell
# 세션 1: 등록 루트의 시작 전체 조사와 이후 변경 감지
powershell -NoProfile -File .\tools\windows\Watch-DocMindEncryptedSources.ps1 -ConfigPath C:\DocMindHost\worker.private.json
```

```powershell
# 세션 2: 등록된 색인 작업을 가져와 복호화·처리
powershell -NoProfile -File .\tools\windows\Start-DocMindUEncryptorHostWorker.ps1 -ConfigPath C:\DocMindHost\worker.private.json
```

감시기는 기본 실행에서 source 세션을 획득한 뒤 하위 폴더를 재귀적으로 조사한다. 초기 전체 조사가 필요하면 `-Once`나 `-SkipStartupScan`을 붙이지 않는다. 특정 원본만 감시할 때는 `-SourceId company-docs`, 처리 워커 범위를 제한할 때는 `-ClaimSourceId company-docs`를 지정할 수 있다. 상시 실행은 검증 후 해당 서버의 Windows 작업으로 등록한다. 기본 작업 등록 도구는 `Register-DocMindHostSyncTasks.ps1 -ConfigPath <실제 설정>`이며, 현재는 Interactive 로그온 실행이므로 로그아웃 상태의 무인 실행을 보장하는 설정으로 취급하지 않는다.

처리 흐름은 **탐색 → 문서·버전 및 작업 등록 → 임시 복호화 → 파싱·청킹 → GTE 임베딩·색인 → 활성화 및 임시 평문 정리**다. 목록에서 발견된 개수만으로 색인 성공을 판단하지 말고, 해당 source의 작업 상태·오류·정리 완료와 실제 검색 가능 문서를 확인한다. 새로 색인한 문서에는 제목·한국어·식별자 보조 필드가 자동으로 생성된다. 기존 색인을 옮기지 않고 새로 만드는 서버는 아래 backfill이 필요하지 않다.

## 기존 문서가 있는 서버의 제목·보조 색인 보강

새로 색인하는 문서는 코드가 제목·보조 필드를 생성한다. 이미 색인된 문서는 코드 교체만으로 채워지지 않으므로 `tools/backfill_docmind_search_fields.py`를 **대상 서버 설정과 새 코드가 로드된 환경**에서 실행한다. 재파싱·재임베딩은 필요하지 않다. 이 도구는 활성 source 카탈로그와 chunk-set ID가 있는 문서를 대상으로 한다. 카탈로그가 비었거나 이전 형식이면 처리 건수 0을 완료로 오인하지 말고 해당 서버의 카탈로그 구성을 먼저 확인한다.

아래는 Bash/WSL 예시다. `API_CONTAINER`는 대상 API 컨테이너명으로 바꾸고, `JOURNAL`은 **컨테이너 내부에서 쓰기 가능하고 호스트에 영구 마운트된 비공개 위치**로 정한다. journal에는 파생 텍스트가 포함되므로 Git 밖에 보관한다.

```bash
API_CONTAINER=your-docmind-api-container
JOURNAL=/your-persistent-private-mount/search-fields-20261019.private.jsonl
docker cp tools/backfill_docmind_search_fields.py "$API_CONTAINER":/tmp/backfill_docmind_search_fields.py

# 사전 조회: 변경 예상 건수 확인, journal은 생성하지 않음
docker exec -w /ragflow "$API_CONTAINER" /ragflow/.venv/bin/python /tmp/backfill_docmind_search_fields.py --journal "$JOURNAL"

# 적용: 동시에 바뀐 청크는 덮어쓰지 않고 충돌로 집계
docker exec -w /ragflow "$API_CONTAINER" /ragflow/.venv/bin/python /tmp/backfill_docmind_search_fields.py --journal "$JOURNAL" --apply

# 본문·벡터·기존 메타데이터 해시 보존 확인
docker exec -w /ragflow "$API_CONTAINER" /ragflow/.venv/bin/python /tmp/backfill_docmind_search_fields.py --journal "$JOURNAL" --verify
```

`--apply`는 기존 journal을 덮어쓰지 않는다. 반복 적용은 새 journal 경로를 사용한다. 충돌·통신 실패가 있으면 해당 레코드를 확인한다. 중단 직전의 적용 ACK가 유실된 intent는 별도 대조가 필요하다. `--verify`는 ACK가 기록된 청크의 보호 필드와 추가 필드 존재를 검증하며, 전체 카탈로그의 처리 누락까지 증명하는 명령은 아니다. 성공·충돌·변경 없음 건수를 예상 활성 청크 수와 함께 확인한다.

## 전환 확인과 복귀

1. DB, Elasticsearch, Redis, 스토리지, GTE와 Jina 연결을 확인한다. 임베딩 차원 768과 기존 벡터 필드를 유지한다.
2. 비로그인 검색 차단과 전체/폴더/문서 범위를 확인한다. BM25·dense 각 128, 통합 128, 문서당 8, 본문 prefix 2400, Jina v3.5, 최종 5개 계약을 확인한다.
3. 대상 서버 문서로 제목, 한국어 띄어쓰기, 제품·모델 번호를 조회한다. 검색과 답변의 범위·인용 버전을 확인하고 이전 버전과 비교한다. 이 PC의 36건 평가를 다른 데이터셋의 정확도로 간주하지 않는다.
4. 이전 이미지와 실행 설정을 보존한다. 프로그램 이미지만 되돌려도 기존 본문·벡터를 복원할 필요는 없다. 추가 색인 필드도 되돌릴 필요가 있을 때만 같은 도구에 `--rollback`을 사용한다. 이후 변경된 레코드는 버전 조건 때문에 복원이 거부된다. 문서 원본이나 DB 전체를 과거 백업으로 덮어쓰는 절차가 아니다.

현재 PC에서의 검증 수치와 한계는 [검색 개선 보고서](SEARCH-LEXICAL-2026-10-19.md)와 [평가 JSON](SEARCH-LEXICAL-2026-10-19.json)에 기록돼 있다.
