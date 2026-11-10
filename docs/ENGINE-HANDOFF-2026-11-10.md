# DocMind 엔진 인수인계서

기준일: 2026-11-10 · 대상: AVDC CPU/OpenVINO 엔진 · 코드 기준: `12608ae77108b0c39ac06aaef4766b93d6572eb8`

**이 문서는 문서를 받아 검색 가능한 청크·벡터를 만드는 엔진의 인수인계서다.** 엔진 코드, 실행 설정, 연결 규격, 확인한 결과와 미검증 사항을 전달한다. 서버 운영 체계 구축 완료나 서비스 전체의 운영 인수 승인을 뜻하지 않는다. 이번 문서 작성에서는 재배포·재색인·네트워크 변경을 하지 않았다.

## 1. 무엇을 만들었는가

등록된 Windows 폴더의 암호화 문서를 기존 uEncryptor2로 임시 복호화하고, Ubuntu VM의 Docker 서비스에서 파싱·청킹·임베딩·색인을 수행한다. 원본은 그대로 두고 작업용 평문은 정리한다. AVDC에서는 GPU와 OCR을 사용하지 않는다.

```mermaid
flowchart LR
    A[등록된 Windows 암호화 원본] --> B[Windows 워커: 버전 확인·임시 복호화]
    B --> C[인증된 API: 작업·임대 관리]
    C --> D[Kordoc: 문서 파싱]
    D --> E[DocMind v3: 청크 생성]
    E --> F[OpenVINO GTE: 768차원 임베딩]
    F --> G[Elasticsearch: 청크·벡터 저장]
    G --> H[새 버전 활성화]
    H --> I[임시 파일 정리·호스트 ACK]
```

MySQL은 문서·버전·작업 상태를 관리한다. 문서별 진행이 겹치므로 파싱 중인 문서와 임베딩 중인 문서가 동시에 존재한다. 12슬롯은 동시에 처리할 문서의 상한이며 uEncryptor2를 12개 동시에 실행한다는 뜻은 아니다. 복호화는 공유 게이트로 제어한다.

검색은 저장된 청크와 벡터를 사용한다. KURE는 검색 후보의 순서를 다시 평가하는 외부 서비스이며, 문서를 색인하기 위한 임베딩 서비스와 역할이 다르다. 답변 생성 모델도 별도의 연결이다.

## 2. 전달 범위와 책임 경계

| 구분 | 전달하거나 확인할 내용 |
|---|---|
| 엔진 인계 범위 | 소스와 버전, AVDC 설정 기준, 워커/API 연결 규격, 청킹·임베딩 동작, 시험 결과, 오류·정리 확인 방법 |
| 연동 담당자와 맞출 부분 | 프로젝트·source·폴더 매핑, 원본 경로, 실행 계정, 키 공급, 애플리케이션 인증, 실제 검색·답변 연결 |
| 운영 담당자 범위 | VPN/방화벽/NAT/TLS, OS·VM 관리, 부팅 후 자동 실행, 백업·복구·모니터링·용량 관리 |
| 별도 자산 | Docker 이미지·모델 가중치·uEncryptor2 및 DLL·실제 설정·인증키. Git만으로 모두 복원할 수 없음 |

운영 항목은 엔진 미구현 기능과 구분한다. 다만 아래의 source 초기 등록이나 신규 환경 재현처럼 엔진을 연결하는 데 필요한 미완료 항목은 숨기지 않고 인계한다.

## 3. 코드와 파일 위치

| 저장소 경로 | 역할 |
|---|---|
| `tools/windows/Start-DocMindIngestionPool.ps1` | 최대 12슬롯 Windows 색인 워커 실행 |
| `tools/windows/Start-DocMindUEncryptorHostWorker.ps1` | 작업 claim, 검증, 복호화, 전달, 임시 파일 정리 |
| `tools/windows/DocMindUEncryptorHostWorker.Common.ps1` | HMAC·경로·정리 공통 처리. 워커와 같은 버전으로 전달 |
| `tools/windows/DocMindDecryptGate.ps1`, `DocMindIngestionPool.ps1` | 공유 복호화 제어, 자식 프로세스 관리 |
| `tools/windows/Watch-DocMindEncryptedSources.ps1` | 원본 조사·변경 감지. 현재 AVDC 상시 감시 검증과는 구분 |
| `app/api/apps/restful_apis/docmind_api.py` | 워커·문서·검색 API |
| `app/api/apps/services/docmind_ingestion_service.py` | 문서 버전·작업 임대·색인 활성화·정리 상태 |
| `app/rag/parser_platform/office_chunker.py`, `config.py` | Kordoc 이후 청킹 및 버전 fingerprint |
| `tools/avdc_embedding/` | AVDC OpenVINO 서비스·풀·변환 코드·설정 예제 |
| `tools/local_embedding/` | 별도의 기존 PC GPU 경로. AVDC 코드로 덮어쓰지 않음 |

AVDC 배포 폴더는 아래와 같다. 이는 11월 10일까지의 배포·정리 기록 기준이며 이 문서 작성 시 원격 생존 상태를 새로 조회한 것은 아니다.

| 위치 | 내용 |
|---|---|
| Windows `D:\DocMind\worker\scripts` | 워커 코드 묶음 |
| Windows `D:\DocMind\worker\runtime` | 서버별 워커 설정·실행 파일·정리 영수증 |
| Windows `D:\DocMind\worker\decryptor` | uEncryptor2 및 의존 DLL |
| Windows `D:\DocMind\tmp` | 임시 평문 공간. 원본 저장소가 아님 |
| Ubuntu `/opt/docmind-avdc/runtime` | 실제 API·임베딩 소스 |
| Ubuntu `/opt/docmind-avdc/config/compose.json` | 병합된 실행 구성. 비밀값이 있어 Git에 올리지 않음 |
| Ubuntu `/opt/docmind-avdc/config/embedding.json` | 임베딩 작업자·스레드 설정 |
| Ubuntu `/opt/docmind-avdc/models/openvino` | 변환 모델 `model.xml`, `model.bin` |
| Ubuntu `/opt/docmind-avdc/manage.sh` | 고정 프로젝트·Compose를 사용하는 관리 명령 |

기존 `D:\uPLEXsoft\uDrive\DEPT` 원본은 별도다. Git에는 원본·청크·벡터·실제 `.env`·키·DB 백업을 넣지 않는다. 예전 설치 안내와 이 문서가 다르면 AVDC의 현재 설정은 이 문서 및 연결한 최신 배포 기록을 기준으로 한다. 10월 Windows 실행 ZIP이 최신 AVDC 설정을 포함한다고 가정하면 안 된다.

## 4. 실행 설정 기준

### 자원과 동시성

| 항목 | 현재 AVDC 기준 |
|---|---|
| 물리 호스트 | Xeon Gold 5220R CPU 칩 2개, 총 48물리 코어·96논리 프로세서, RAM 약 127.6GiB |
| DocMind VM | 96 vCPU, 상한 100%, RAM 64GiB |
| 임베딩 엔진 | OpenVINO 2026.4.1, FP32, GTE multilingual base, 정규화한 768차원 CLS 벡터 |
| 임베딩 작업자 | 22개 × 작업자당 4스레드, 작업자당 1 stream |
| 임베딩 최대 허용량 | 논리 프로세서 88개분의 CPU 시간, VM CPU `0-47,56-95`, 메모리 52GiB |
| 파서 | 2개 작업자, 합산 CPU 시간 4개분, 메모리 1GiB |
| 나머지 서비스 | VM CPU `48-55` 공유, API 메모리 2.5GiB·ES 1.5GiB |
| 문서 / 임베딩 요청 상한 | 12문서 / 24요청 |
| 내부 계산 배치 | 128토큰 이하 최대 16개, 긴 입력은 1개, token fill 최소 85% |
| 입력 길이 | 8,192 모델 토큰 초과 시 거절. 조용히 잘라내지 않음 |
| 청킹 | `docmind-kordoc-chunker-v3`, 짧은 제목과 다음 본문 결합, 표 독립 유지 |
| OCR | 비활성. 검색 가능한 텍스트가 없는 PDF는 제외 결과로 처리 |

**한도와 실사용량은 다르다.** 88개분 한도는 CPU 칩 88개, 물리 코어 독점 예약, 항상 88개를 가득 사용한다는 뜻이 아니다. VM의 CPU 번호 역시 물리 호스트 코어 고정 예약을 뜻하지 않는다. 현재 154개 시험에서 서버 전체 CPU는 평균 25%, 관측 최대 83%였다.

설정 위치:

- API `DOCMIND_INGESTION_DOCUMENT_CONCURRENCY=12`, `MAX_CONCURRENT_EMBEDDING_REQUESTS=24`.
- 파서 `KORDOC_WORKERS=2`, API `KORDOC_CLIENT_CONCURRENCY=2`.
- API `PARSER_PLATFORM_KORDOC_CHUNKER_REVISION=docmind-kordoc-chunker-v3`.
- OpenVINO [설정 예제](../tools/avdc_embedding/config.example.json)의 `threads=4`, `cpu_short_batch=16`, `cpu_groups` 22개와 [Compose 예제](../tools/avdc_embedding/compose.example.yaml)의 자원 한도를 함께 사용한다.
- `EMBEDDING_BATCH_CONCURRENCY`는 별도의 문서 내 요청 설정이다. 24요청 상한과 혼동해 이 값을 24로 설정하면 안 된다.

AVDC 예제의 CPU 번호는 96 vCPU VM을 전제로 한다. 작은 서버에 그대로 복사하지 않는다. 문서 슬롯·파서·임베딩 한도·메모리를 함께 맞춘다. Kordoc 자체의 다단 분류·읽기 순서를 이번 청킹 수정에서 변경하지 않았다.

### 실행·확인·정지

아래는 담당자가 준비된 AVDC에서 사용할 명령 예시이며, 문서를 읽는 것만으로 실행되는 작업이 아니다.

```bash
# Ubuntu: 현재 서비스 상태 확인
sudo /opt/docmind-avdc/manage.sh ps

# 기존 설치와 설정이 준비된 경우 서비스 시작
sudo /opt/docmind-avdc/manage.sh up -d
```

새 설치는 위 한 줄로 완성되지 않는다. 베이스 이미지, 로컬 모델/신뢰 코드 캐시, 변환 모델, DB 초기화, 서버별 인증·source 설정이 필요하다. OpenVINO 예제는 **기존 AVDC 스택의 임베딩 override**이며 독립 설치 Compose가 아니다. [빌드·모델 변환·복구 안내](../tools/avdc_embedding/README.md)를 따른다. 해당 README의 11월 6일 이전 경로는 당시 기록이며 현재 파일 위치는 위 폴더 표를 우선한다.

Windows에서 이미 설정된 source의 대기 작업을 처리할 때는 저장소 루트에서 다음처럼 실행한다. 경로와 `source_id`는 인계받은 실제 값으로 바꾼다. 기존 워커가 실행 중이면 중복 시작하지 않는다.

```powershell
$workerConfig = 'D:\DocMind\worker\runtime\worker.json'
$workerStop = 'D:\DocMind\worker\runtime\handoff-ingest.stop'
$sourceId = '<등록된-source-ID>'
if (Test-Path -LiteralPath $workerStop) { throw '이전 중지 표식이 있습니다. 종료 상태를 확인한 뒤 새 실행 경로를 정하세요.' }
powershell.exe -NoProfile -File .\tools\windows\Start-DocMindIngestionPool.ps1 -ConfigPath $workerConfig -Concurrency 12 -ClaimSourceId $sourceId -StopFile $workerStop
```

정상 종료 요청은 다른 PowerShell에서 같은 stop 파일을 생성한다. 진행 중인 작업과 정리 ACK가 끝나고 워커가 종료될 때까지 기다린다.

```powershell
New-Item -ItemType File -Path 'D:\DocMind\worker\runtime\handoff-ingest.stop'
```

그다음 서비스를 내릴 필요가 있을 때만 `sudo /opt/docmind-avdc/manage.sh stop`을 사용한다. `down -v`나 볼륨 삭제는 정지 방법이 아니다. 변경 전에는 현재 Compose·코드·모델 버전과 이미지 ID를 별도로 보관한다. 롤백은 해당 서비스의 코드와 설정을 함께 복원하며, DB·색인 전체 삭제를 일반 복구 절차로 쓰지 않는다.

## 5. 연동 방법

### 원본 폴더를 엔진에 연결하기

1. 대상 프로젝트와 폴더를 준비하고 `DocmindSource.id`, `project_id`, `default_folder_id`, `enabled`를 연결한다. 현재 이를 한 번에 처리하는 전용 설치 명령/화면은 제공하지 않는다.
2. [워커 JSON 예제](../tools/windows/docmind-host-worker.config.example.json)를 비공개 경로에 복사하고 `sources[].source_id`를 같은 ID로, `root`를 Windows 원본 경로로 지정한다. JSON에 경로만 추가하면 서버 등록까지 완료되는 것이 아니다.
3. `api_base_uri`, HMAC 키 파일, uEncryptor2 경로·SHA256·서명, 임시 작업·정리 영수증·동기화 상태·공유 복호화 제어 경로를 채운다. 실제 보관 정책과 볼륨 조건도 설정에 맞게 준비한다.
4. 기존 워커는 인증된 **loopback HTTP(S)** 주소를 요구한다. Ubuntu VM의 원격 주소를 그대로 넣지 않는다. Windows에서 승인된 로컬 전달 경로가 있어야 한다. 이 내부 API 연결과 KURE용 외부 인터넷 연결은 별개이며 NAT 구성은 보류 상태다.
5. 새 문서 집합에는 새 source ID를 사용한다. 기존 source를 다른 루트로 바꾸면 삭제·버전 판단에 영향을 줄 수 있다.

최초 조사·변경 감지를 연결할 때는 별도 세션에서 아래 감시기를 사용한다. 이 코드는 존재하지만 이번 154개 고정 입력 시험이 상시 감시 전체를 검증한 것은 아니다.

```powershell
powershell.exe -NoProfile -File .\tools\windows\Watch-DocMindEncryptedSources.ps1 -ConfigPath 'D:\DocMind\worker\runtime\worker.json' -SourceId '<등록된-source-ID>'
```

### Windows 워커 ↔ 엔진 API

기본 접두사는 `/api/v1`이다. 실제 물리 경로는 Windows에서 관리하며 API에는 `source_id`와 상대 경로·버전 정보로 연결한다. 새 클라이언트를 만들기보다 기존 워커의 서명·임대·정리 처리를 재사용한다.

| 메서드·경로(접두사 제외) | 역할 |
|---|---|
| `POST /cloud-sync/host-worker/observations` | 이미 등록된 문서의 원본 버전 관찰. 신규 폴더 등록 API가 아님 |
| `POST /cloud-sync/host-worker/claim` | 처리 가능한 작업 임대. 작업이 없으면 `job: null` |
| `POST /cloud-sync/host-worker/jobs/{job_id}/validate` | 임대한 버전·작업 유효성 검증 |
| `PUT /cloud-sync/host-worker/jobs/{job_id}/artifact` | 임시 평문 전달 후 파싱·색인 처리 |
| `POST /cloud-sync/host-worker/jobs/{job_id}/cleanup/prepare` | 정리 수명 주기 진행 |
| `POST /cloud-sync/host-worker/jobs/{job_id}/status` | 워커 결과·호스트 정리 상태 보고. 일반 GET 조회 API가 아님 |

인증은 `X-DocMind-Key-Id`, `X-DocMind-Timestamp`, `X-DocMind-Nonce`, `X-DocMind-Content-SHA256`, `X-DocMind-Signature` 헤더를 사용하는 HMAC-SHA256이다. 서명 대상은 `METHOD`, 경로·쿼리, timestamp, nonce, 본문 SHA256을 줄바꿈으로 연결한 값이다. 응답 검증도 기존 공통 라이브러리를 사용한다. 평문 전달에는 worker/version/fencing token/평문 해시·크기 헤더가 추가된다. payload 전체와 유효성 조건의 기준은 [API 코드](../app/api/apps/restful_apis/docmind_api.py)와 [워커 공통 구현](../tools/windows/DocMindUEncryptorHostWorker.Common.ps1)이다.

이 HMAC 키는 임베딩 Bearer 키, KURE 키, 사용자 로그인 인증과 다르다. 임대·fencing token을 생략하거나 만료된 작업 ID를 재사용하지 않는다.

### 임베딩 연동

임베딩 풀 내부 주소에 `GET /health`를 보내면 `status`, `model`, `dimensions`, `device`, `workers`를 확인할 수 있다. 정상 기준은 HTTP 200, `dimensions: 768`, `device: cpu`, `workers: 22`다. 모든 작업자가 준비되기 전에는 503이 반환될 수 있다.

```http
POST /v1/embeddings
Authorization: Bearer <별도 공급한 임베딩 키>
Content-Type: application/json

{"model":"gte-multilingual-base","input":["합성 문서 확인 문장"]}
```

응답은 입력 순서에 대응하는 `data[].index`, `data[].embedding`과 사용 토큰 정보를 제공한다. 벡터는 각각 768차원이며 길이별 계산 후에도 요청의 원래 순서를 복원한다. 키는 헤더로 공급하고 Git·명령 이력·공용 로그에 저장하지 않는다. 메모리 부족이나 큐 포화 503을 색인 성공으로 처리하지 않는다.

### 문서·검색 연동

문서 상세는 `GET /api/v1/docmind/documents/{document_id}`, 검색은 `POST /api/v1/docmind/search`다. 사용자 로그인·tenant/project 범위를 사용하며 워커 HMAC 키로 로그인 인증을 대신할 수 없다.

```json
{
  "question": "합성 문서의 확인 문장은 무엇인가요?",
  "project_id": "<프로젝트-ID>",
  "scope": {"mode": "documents", "document_ids": ["<문서-ID>"]}
}
```

HTTP 성공만 보지 말고 응답의 업무 결과와 근거 문서·청크를 확인한다. KURE 외부 연결과 별도의 답변 모델이 준비되지 않은 상태에서, 색인 완료를 검색·답변 전체 연결 완료로 보고하면 안 된다. 원문 미리보기 역시 별도의 preview 워커·권한·임시 접근 수명이 필요하다.

## 6. 완료·실패 판단과 기본 진단

색인 완료는 작업 등록 수나 UI 문서 수 하나만으로 판단하지 않는다. 성공 작업의 `lifecycle_state=COMPLETE`, `cleanup_state=COMPLETE`, `host_cleanup_state=COMPLETE`, 원본 버전의 활성 상태, 활성 청크 세트 및 ES 항목을 함께 확인한다. 관리 조회는 기존 엔진 서비스/관리 도구를 사용하며 DB 상태를 직접 수정해 완료 처리하지 않는다.

| 현상·코드 | 확인할 부분 |
|---|---|
| `WORKER_AUTH_FAILED` | 키 ID·공유키 대응, 시계 차이, nonce·본문 서명. 키 값은 로그에 출력하지 않음 |
| 작업이 안 잡힘 | source 활성/폴더 매핑, 실제 대기 작업, worker 범위·접근 유효기간·기존 임대 |
| `PARSER_BUSY` | 파서 용량과 클라이언트 동시성. 무제한 즉시 재시도 대신 기존 큐·재시도 경로 사용 |
| `PARSER_PDF_NO_SEARCHABLE_TEXT` | OCR이 필요한 PDF. 이 구성에서는 정상 색인으로 집계하지 않음 |
| 8,192토큰 초과 | 해당 입력·청킹 경계 점검. 임베딩에서 강제 절단하지 않음 |
| 임베딩 503 | health, 작업자 생존, 메모리·큐 대기 확인 |
| `CLEANUP_FAILED` 또는 정리 미완료 | 평문 잔존·정리 영수증·해당 작업 버전 확인. 무조건 새 작업을 만들지 않음 |
| 디스크 부족 | 실제 볼륨과 이미지/로그 사용량 구분. 엔진 재시작이나 기존 색인 삭제로 덮지 않음 |

## 7. 검증된 부분

아래는 기존 기록으로 확인한 범위다. 현재 문서 작성 때문에 시험을 다시 실행하지 않았다.

| 검증 | 결과 | 범위와 한계 |
|---|---|---|
| 실제 154개 새 색인 | 105,638,237바이트(100.74MiB), 152성공·OCR 필요 2제외, 456쪽·1,596청크 | 2026-11-10 단회, 기존 색인 0건에서 시작, 서비스 예열 상태 |
| 전체 시간 | 마지막 활성화 184.64초, 전체 작업 종료·호스트 정리 ACK 185.18초 | 최초 작업 등록부터. 복호화·전송·대기 포함, 준비·백업·사후 검증 제외 |
| 서버 전체 CPU | 작업 전 평균 4% → 색인 중 평균 25%, 색인 중 관측 최대 83% | 약 6초 표본. 순간 최대 전부를 포착한 값이 아님 |
| 메모리 | DocMind 컨테이너 합계 관측 최대 37.17GiB, 임베딩 33.74GiB | 컨테이너 약 1초 표본, 호스트 가용 RAM 최저 18.63GiB |
| 그룹웨어 | HTTP 평균 20.67 → 22.18ms, 색인 중 p95 68.75ms·최대 130.46ms, HTTP 200 31/31 | 기준 13표본·색인 31표본. 시작 페이지 검사이며 로그인 업무 부하 시험이 아님 |
| 원본·정리 | 원본 154개 SHA256 불변, Windows/API/파서 임시 파일 0, 호스트 정리 완료 | 해당 시험의 사후 확인 |
| OpenVINO 전환 | 같은 24입력 CPU 70.159 → 61.697초, PC GPU 39.191초 | 과거 단회. 현재 154개 GPU 실측은 없음 |
| 임베딩 수치 | 같은 1,359벡터 최대 절대 차이 1.63624e-6 | 해당 PyTorch/OpenVINO 비교 입력에 한정 |
| 검색 회귀 | OpenVINO 전환 후 동일 source 12질문의 문서·근거·관련성 상태 12/12 동일 | 전체 검색 정확도 100%를 뜻하지 않음 |
| 청킹 v3 | 사례집 794 → 440청크, 48토큰 미만 44개·표 108개, 8문항 근거 1위 8/8 유지 | 단일 사례집 비교. Kordoc 자체는 수정하지 않음 |

그룹웨어 VM CPU의 정수 지표가 색인 중 0%였다는 것은 실제 CPU 시간이 완전히 0이라는 뜻이 아니다. 서버 전체 CPU와 분모도 다르다. 이번 154개 시험은 KURE 재평가를 병행하지 않았다.

## 8. 남은 부분 — 미검증·개선 가능성과 운영 인계 구분

| 항목 | 현재 판단 | 이어받을 작업 |
|---|---|---|
| 새 환경 설치 재현 | Git 소스·예제는 있음. 최신 AVDC를 빈 서버에서 한 번에 설치하는 완성 패키지는 별도 확인 필요 | 이미지·모델·상용 복호화 모듈·서버 설정 목록을 수령하고 합성 문서로 재현 |
| source 초기 구성 | 프로젝트·폴더·source 수동 구성 필요 | 연동 담당자가 실제 원본에 매핑. 엔진 개선 시 전용 초기화 도구 후보 |
| 상시 변경 감지 | 감시·증분/삭제 코드와 프로토타입 검증 있음 | 현재 AVDC 조합에서 신규/수정/삭제·폴더 삭제·재시작 E2E 확인. 154개 시험으로 대체 불가 |
| 무인 복호화 | 현재 시험 실행 계정에서 성공 | 로그아웃·재부팅·서비스 계정 조건은 별도 검증. 운영 담당자와 수행 |
| 검색 외부 연결 | OpenVINO 검색 회귀 기록은 있음. AVDC 단독 외부 통신 경로는 미완료 | 승인된 KURE/답변 연결 준비 후 실제 사용자 인증·scope 포함 통합 시험. NAT 보류 유지 |
| OCR·형식 범위 | 비OCR 문서 경로를 검증 | OCR 필요 문서는 별도 처리 정책. 모든 형식·손상·초대형 파일을 검증했다고 보지 않음 |
| 파서 수 최적성 | 현재 2작업자/합산 4개분 한도 사용 | 22임베딩 작업자 조건의 전역 최적값은 확정하지 않음. 추가 튜닝은 선택 과제 |
| 대량·장시간·혼합 부하 | 154개 단회 및 과거 비교 시험 | TB 규모·업무 피크·검색 동시 부하·장시간 장애 복구는 별도. 속도 환산은 보장값이 아님 |
| 서버 운영 | 엔진 인계 범위 밖 | 자동 기동·모니터링·백업·복구·네트워크·키 관리 담당자 지정 |

예전 6문서·임베딩 4작업자 시험에서는 파서 1개 81.75초, 2개 83.96초였고 전체 임베딩 작업자의 동시 대기는 10.33초에서 9.08초로만 줄었다. 두 조건 모두 파서 CPU 한도는 4개분이었다. 단회·다른 동시성 조건이므로 파서 2개가 항상 느리다거나 현재 파서 1개가 최적이라고 결론 내리지 않는다.

## 9. 인수 시 확인 목록

- [ ] 코드 기준 SHA와 실제 적용 코드/이미지/모델 식별자를 함께 받았다. Git 최신 커밋이 서버 전체 이미지와 같다고 추정하지 않는다.
- [ ] 실제 설정·키·모델·복호화 모듈은 승인된 별도 경로로 전달받았다. Git 문서에는 값이 없다.
- [ ] 문서 source 등록과 Windows 루트 매핑, API loopback 전달 경로를 확인했다.
- [ ] 서비스 health와 자원 한도를 확인했다. 기존 PC GPU 경로와 AVDC CPU 경로를 구분했다.
- [ ] 지정된 합성 문서 한 건으로 작업 생성 → 활성 청크 → 정리 ACK까지 재현했다. 이 체크는 인수 담당자가 수행할 항목이며 이번 문서 작성 중 실행한 것은 아니다.
- [ ] OCR 제외·실패 결과를 성공과 구분하며, 검색·상시 동기화·운영 과제의 담당자를 정했다.

## 근거 문서

- [154개 최신 측정과 측정 경계](AVDC-INDEXING-154-2026-11-10.md), [집계 JSON](measurements/avdc-fresh154-2026-11-10.json)
- [종합보고서 Markdown](reports/AVDC-STATUS-2026-11-10.md), [7쪽 PDF](reports/AVDC-STATUS-2026-11-10.pdf)
- [OpenVINO 배포·검색 비교](AVDC-OPENVINO-DEPLOYMENT-2026-11-06.md), [OpenVINO 소스·빌드 안내](../tools/avdc_embedding/README.md)
- [청킹 v3 배포·복구](AVDC-CHUNKING-DEPLOYMENT-2026-11-10.md)
- [원본 폴더 연결](SERVER-DEPLOYMENT-2026-10-19.md), [Windows 복호화 워커 안내](UENCRYPTOR2-VALIDATION.md)
- [변경 감지 프로토타입의 검증·한계](DOCMIND-FILE-SYNC-PROTOTYPE.md)
- [GitLab·GitHub 동기화와 보관 브랜치](GIT-MIRRORS-2026-11-10.md)
