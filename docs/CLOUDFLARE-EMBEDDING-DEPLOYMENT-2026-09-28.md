# Cloudflare BGE-M3 운영 적용 및 컨테이너 점검

작성일: 2026-09-28. 대상: 현재 Windows 개발/시험 서버의 DocMind 공유 workspace.

## 적용 결과

- 기존 `OpenAI-API-Compatible` 공급자에 `cloudflare` 인스턴스와 `@cf/baai/bge-m3` 모델을 등록했다. 별도 프록시나 임베딩 컨테이너를 추가하지 않았다.
- 공유 tenant의 기본 임베딩과 dataset `5ba70756384649c20b8ef2c4cb214d88`의 임베딩을 함께 전환했다. 모델 ID는 `20e6a852bb1d11f1b46ffd22ce356b26`이다.
- 설정과 자격증명은 앱의 기존 모델 관리 DB에 저장된다. 재시작에 의존하는 환경변수 덮어쓰기를 하지 않았다. 등록 원본 curl은 Git에서 제외된 `.local/docker/cloudflare-curl.txt`이며 파일 ACL을 현재 사용자·관리자·SYSTEM으로 제한했다. 키를 문서나 Git에 넣지 않았다.
- 기존 청크/벡터/활성 청크 세트는 수정하지 않았다. 신규 수집·내용 변경 색인 및 질문 검색은 같은 dataset binding을 사용한다. Jina v3.5, C128, prefix2400, 기존 청커와 OCR 설정은 그대로다.
- Cloudflare 전환을 위해 앱/worker를 재시작하지 않았다. 동시에 진행 중인 내부 이동 기능 배포는 별도 작업이다. 그 작업의 코드·감시 설정·컨테이너 배포를 덮어쓰지 않았다.
- Cloudflare 장애/한도 초과 때 자동으로 로컬 모델로 전환하지 않는다. 기존 오류 처리 경로를 따른다. 유료 플랜 변경이나 추가 결제를 수행하지 않았다. 계정 플랜·잔여 Neurons는 이번 적용에서 확인하지 않았다.

## 검증

### 사전 비교와 실패 기록

사전 시험은 합성 입력으로 진행했고 이 단계에서는 앱 설정·기존 색인을 바꾸지 않았다. 짧은 한국어/영어 6문장 최초 측정은 Cloudflare 0.807/0.847초, 로컬 12.973초였지만 반복 측정은 Cloudflare 0.430/0.806초, 로컬 0.404/0.340초였다. 따라서 짧은 요청까지 언제나 API가 더 빠르다는 결론은 내리지 않았다. 최초 로컬 지연 원인은 분리 검증하지 않았다.

합성 영어 장문 40개를 16/16/8로 동일 배치 처리했을 때 Cloudflare 2.678/2.089초, 로컬 31.792/30.460초였다. 최소 코사인 0.99999290, 최대 좌표 차이 0.00052572로 근접하지만 비트 단위 동일하지 않다. 예비 40개 단일 요청은 로컬 최대 batch 32 제한으로 HTTP 422가 발생했으므로 성능 비교에서 제외했다. Cloudflare usage 응답이 없어 실제 청구 토큰·Neurons는 검증하지 못했다.

### 실제 입력 및 적용 후 확인

| 검증 | 결과 |
| --- | --- |
| DocMind 기존 공급자 등록 API의 실제 adapter 호출 | 통과. OpenAI SDK 경로 및 `drop_params` 요청 포함 |
| 실제 엑셀 활성 청크 40개 + 제목 1개, 16개 단위 요청 | Cloudflare 1.985초 / 로컬 CPU 34.333초 |
| 같은 입력의 공급자 간 최소 코사인 유사도 | 0.99999219898, 양쪽 1024차원, finite 확인 |
| 제목 가중치 0.1을 적용한 Cloudflare 벡터와 기존 저장 벡터 비교 | 최소 코사인 0.99999307642. 색인은 읽기만 수행 |
| 전환 후 선택 문서 검색 | HTTP 성공, BM25 40 / dense 40, 후보 8, 결과 5, 2.407초 |
| 전환 후 폴더 범위 검색 | HTTP 성공, BM25 40 / dense 40, 후보 8, 결과 5, 2.398초. 지정 엑셀만 반환 |
| 설정 전환·보상 롤백·tenant 경계 등 mock 검사 | 6개 통과. 실제 네트워크/DB 쓰기 없음 |

위 임베딩 시간은 이미 파싱된 실제 엑셀 청크를 **읽기 전용으로 재계산**한 측정이다. 복호화·파싱·전체 색인 시간이 1.985초라는 뜻이 아니다. API/모델 ID 읽기 확인과 검색은 실제 실행했지만, 이번 작업에서 원본을 새로 업로드하거나 이동해 전체 수집 E2E를 다시 실행하지는 않았다. 장문/모든 형식/대량 동시 처리 성능을 보증하지 않는다. 원문과 벡터, 키는 비교 결과 로그에 출력하지 않았다.

등록 후 데이터셋 API는 `@`로 시작하는 모델 문자열을 거부했으므로, 지원되는 **등록 모델 ID**로 연결했다. 제품 코드를 변경해 유효성 검사를 우회하지 않았다.

## 운영 및 되돌리기

실행 위치: `C:\DocMindDev\docmind`. 진행 중 색인 작업이 없는지 확인하고 전환한다.

```powershell
# 현재 등록된 Cloudflare 모델로 연결 (자격증명을 출력하거나 재등록하지 않음)
& tools/windows/Set-DocMindCloudflareEmbedding.ps1 -Target Cloudflare

# 로컬 모델로 되돌릴 때: 먼저 기존 컨테이너를 실행/health 확인
& C:\Users\uplex\bin\docker.cmd start docmind-windows-dev-bge-m3-cpu-1
& C:\Users\uplex\bin\docker.cmd inspect docmind-windows-dev-bge-m3-cpu-1 --format '{{.State.Health.Status}}'
& tools/windows/Set-DocMindCloudflareEmbedding.ps1 -Target Local

# 설정 도구 자체의 무네트워크 회귀검사
& tools/windows/Test-DocMindCloudflareEmbedding.ps1
```

전환 도구는 dataset 먼저, tenant 기본값을 다음에 설정한다. 두 번째 요청 실패 시 dataset을 원래 값으로 보상 복원한다. 두 HTTP 요청은 하나의 DB 트랜잭션이 아니므로 동시 관리자 변경이나 통신 단절 후에는 DB/API 값을 다시 확인한다. 정상 적용은 두 곳을 읽어 검증했다. 실제 운영 롤백 재전환은 대량 수집이 시작되어 실행하지 않았고, mock으로 검증했다.

현재 글로벌 신규 사용자용 서비스 설정은 로컬 TEI 기본값을 유지한다. 이번 범위는 현재 공유 tenant와 그 dataset이다. 별도 tenant를 만드는 경우 모델 등록/기본값 적용이 별도로 필요하다. 로컬 컨테이너는 롤백 및 기존 글로벌 기본 설정을 위해 제거하지 않았다.

## 컨테이너 조사 (서브에이전트)

조사 시점 전체 12개: 실행 9개 / 종료 3개. 메모리는 시점 측정치다.

| 서비스/컨테이너 | 상태와 판단 |
| --- | --- |
| ragflow-cpu | 실행. 앱/API/색인 worker. 유지 (약 3.415 GiB) |
| es01 | 실행. 청크·벡터 검색 저장소. 유지 (약 1.208 GiB) |
| mysql | 실행. 문서·작업 상태 DB. 유지 (약 281 MiB) |
| redis | 실행. 큐·캐시. 유지 (약 28 MiB) |
| minio | 실행. 앱 저장소 의존성이 남아 있음. 유지 (약 136 MiB) |
| docling-office-parser | 실행. Office 파서. 유지 (약 120 MiB) |
| surya-parser-cpu | 실행. 이름과 달리 GPU OCR 이미지. 유지 (약 378 MiB, GPU VRAM과 별도) |
| docmind-preview-processor | 실행. 임시 원문 미리보기. 유지 (약 63 MiB) |
| bge-m3-cpu | 실행. 공급자 비교/롤백 및 글로벌 기본 모델. 유지 (약 2.04 GiB) |
| security-gate | 정상 종료된 기동 전 검사. 미사용 장애 컨테이너가 아니므로 유지 |
| model-secret-gate | 정상 종료된 자격증명 검사. 유지 |
| docmind-preview-tests | 종료된 일회성 시험. 정리 후보 1개 |

`docmind-preview-tests` (`1bbd2d8a4fea`)는 16:01 종료, restart=no, 연결된 데이터 볼륨 없음, preview checkout 읽기 전용 bind다. 쓰기층은 61,562,880 bytes(약 58.7 MiB). 다른 배포 작업의 재사용 여부 확인 전에는 삭제하지 않는다. 이를 삭제해도 9.49GB 기본 이미지 전체가 제거되는 것은 아니다.

실행 중 컨테이너, 이미지, 볼륨, 빌드 캐시에 대해 prune나 삭제를 수행하지 않았다. 앱 컨테이너 쓰기층에 배포 UI가 있을 수 있어 재생성에 주의해야 한다. 로컬 BGE를 추후 정지하면 약 2 GiB RAM을 절약할 수 있으나, 현재 진행 중인 다른 배포/수집과 조율 후 수행해야 한다.

## 동시 배포 관찰

임베딩 전환 직전 ingestion은 COMPLETE 9개, 진행 중 0개였다. 이후 다른 작업의 DEPT_2 discovery가 실행되면서 COMPLETE 10개, DISCOVERED 238개, PARSING 1개가 관찰됐다. 감시/수집 범위는 이 작업에서 변경하지 않았고, 해당 배포 작업에 확인을 요청했다. 이 대량 수집의 완료 여부는 위 Cloudflare 검증 결과와 구분한다.

후속 배포 작업의 회신에서 DEPT_2 watcher를 중지하고 추가 처리 억제 중임을 확인했다. 해당 작업의 요청에 따라 로컬 BGE는 유지하고, 기존 테스트 컨테이너와 job 삭제/정리는 보류했다. 검색 제외 구버전의 24시간 정리 기능은 사용자 후속 요청으로 별도 SOL 에이전트가 구현 중이며, 이 임베딩 전환 성과에 포함하지 않는다.
