# 설정 작성표

[인수인계서](../ENGINE-HANDOFF-2026-11-10.md) · [실행 순서](RUNBOOK.md)

실제 값은 Git 밖에 둔다. 아래는 키 값이 아닌 **이름·형식·확인 방법**이다. 기존 AVDC에서는 현재 제한된 설정 파일을 인계받아 사용하며, 새 키를 임의로 발급해 기존 값을 덮어쓰지 않는다.

## Windows 워커

[JSON 예제](../../tools/windows/docmind-host-worker.config.example.json)를 복사한다. 필요한 전체 워커 소스는 `tools/windows` 묶음이다. 단일 ps1만 복사하면 Common·게이트·C# 감시기 의존 파일이 빠질 수 있다.

| 필드 | 입력 / 확인 |
|---|---|
| `worker_id` | 영문·숫자·`_.:-` ID, 최대 128자. 12슬롯이면 코드가 슬롯 접미사를 붙임 |
| `api_base_uri` | Windows에서 도달 가능한 loopback HTTP(S) API. 예: `http://127.0.0.1:19380`. 실제 전달 포트와 대조 |
| `key_id` | API의 `DOCMIND_HOST_WORKER_KEY_ID`와 같아야 함 |
| `shared_secret_file` | 32바이트 HMAC 키 파일. API에 마운트한 동일 키와 대응 |
| `executable_path` | 해당 서버 uEncryptor2.exe. 실제 서명·해시 확인 |
| `executable_sha256` | 승인된 실행 파일의 SHA256. 새 바이너리면 재검증 후 지정 |
| `executable_signer_thumbprint` | 승인된 서명 인증서 thumbprint. 해시와 함께 대조 |
| `work_root` | 원본과 분리한 작업용 평문 폴더. 시험은 전용 빈 폴더 사용 |
| `cleanup_receipt_root` | 정리 ACK 영수증. 평문 폴더와 분리, 재시도 전에 삭제하지 않음 |
| `decrypt_control_root` | 기존 워커와 같은 공유 복호화 게이트 경로. 달리 잡아 동시 실행 제한을 우회하지 않음 |
| `sync_state_root` | 감시기 SQLite 상태, source 원본 밖. 운영 상태를 시험용으로 복사/삭제하지 않음 |
| `sources` | `[{"source_id":"handoff-sample","root":"D:\\ApprovedEncryptedSampleRoot"}]`. 서버 논리 source 등록과 동일 ID |
| `job_timeout_seconds` | 복호화 CLI 제한, 예제 120초. 전체 색인 제한이 아님 |
| `claim_lease_seconds` | 전체 작업 임대, 예제 1,800초. 서버 범위 30~1,800초 |
| `lease_safety_seconds` | 전달·정리 안전 여유, 예제 10초 |
| `cleanup_receipt_replay_seconds` | ACK 재전송 주기, 예제 60초 |
| `reaper_minimum_age_seconds` | 만료 작업 회수 최소 나이, 예제 300초 |
| `preview_enabled` | 색인 전용 인수 시험은 false. preview는 별도 역할 |
| `require_bitlocker` | 예제 true. 실제 보호 볼륨 상태와 일치해야 함. 실패를 숨기려고 낮추지 않음 |
| `backup_exclusion_acknowledged` | 실제 임시 폴더 백업 제외 여부를 확인한 후 true. 폴더 생성만으로 제외되는 것은 아님 |
| `scan_batch_size` | 예제 250. 전체 정합성 스캔의 배치 크기 |
| `daily_reconciliation_timezone/local_time` | 예제 `Asia/Seoul` / `00:00`. 상시 운영 작업 등록은 별도 |

키 파일 ACL은 실행 계정·SYSTEM 등 필요한 주체로 제한한다. 기존 [키 생성 도구](../../tools/windows/Initialize-DocMindHostWorkerSecret.ps1)는 저장소 `.local` 개발 환경용이다. AVDC 기존 키를 회전하는 용도로 그대로 실행하지 않는다.

## API·파서·임베딩

| 위치 / 이름 | 기준값·의미 |
|---|---|
| API `DOCMIND_HOST_WORKER_KEY_ID` | Windows `key_id`와 일치 |
| API `DOCMIND_HOST_WORKER_HMAC_SECRET_FILE` | 컨테이너 내부 키 파일 경로. Windows와 같은 32바이트 키 |
| API DB/ES/Valkey/스토리지 설정 | 현재 Compose·서비스 설정과 일치. 새 설치는 새 인증값, 기존 볼륨에는 기존 값 유지 |
| API `DOCMIND_SHARED_WORKSPACE_ENABLED` | 현재 AVDC `1`. 공용 작업공간 설치이며 사용자별 ACL 검증 완료로 확대 해석하지 않음 |
| API 공유 사용자 | 기존 설치의 공유 사용자 설정과 DB 계정이 대응해야 bootstrap 가능 |
| API 문서 동시성 | `DOCMIND_INGESTION_DOCUMENT_CONCURRENCY=12` |
| API 임베딩 요청 상한 | `MAX_CONCURRENT_EMBEDDING_REQUESTS=24` |
| API 파서 동시성 | `KORDOC_CLIENT_CONCURRENCY=2` |
| 파서 프로세스 수 | `KORDOC_WORKERS=2`, 합산 CPU 한도 4개분, 메모리 1GiB |
| API 청킹 버전 | `PARSER_PLATFORM_KORDOC_CHUNKER_REVISION=docmind-kordoc-chunker-v3` |
| 임베딩 설정 | `device=cpu`, `threads=4`, `cpu_short_batch=16`, `cpu_groups` 22개 |
| 임베딩 인증 | `api_key_file`로 별도 Bearer 키 공급. HMAC 키와 혼용하지 않음 |
| OpenVINO 모델 | `model.xml/bin` 모두 필요, FP32·768차원, tokenizer/신뢰 코드 캐시는 기반 이미지에 있음 |
| 외부 검색 | KURE 키·통신 경로는 별도. 색인 시험에 KURE 호출은 필요하지 않음 |
| 답변 생성 | 답변 모델 URL·인증은 별도. 이번 합성 색인 합격 조건에 포함하지 않음 |

96 vCPU보다 작은 환경에서는 AVDC의 CPU 번호를 그대로 복사하지 않는다. 컨테이너 메모리 합계·VM 메모리·호스트 여유를 함께 확인한다. Compose에 `cpus: 88`이 있어도 항상 서버 전체 CPU의 88%를 사용하는 뜻은 아니다.

## 설정 인계 시 채울 비공개 표

키 자체 대신 다음 위치만 승인된 채널로 전달한다: 실제 Compose 경로, 워커 JSON 경로, HMAC 키 공급 위치, 임베딩 키 공급 위치, source/project ID, Windows 원본 루트, API 로컬 포트, 실행 계정, 임시 작업·영수증·동기화 상태 경로. 본 저장소의 예시 경로는 자동 생성된 실제 경로가 아니다.
