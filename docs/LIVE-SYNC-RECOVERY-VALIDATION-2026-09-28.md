# 제한된 자동 동기화와 Surya 복구 실환경 검증

작성 기준: 2026-09-28, Asia/Seoul. 기존 승인 DOC 네 개(TEST1 세 개,
DEPT_1 한 개)와 기존 DEPT_2 표본 한 개가 대상이다. DEPT_2 전체 382개
발견·수집, 원본 수정·삭제, 운영 배포는 수행하지 않았다.

## 적용한 수정

- `b198727`: 정합성 claim과 실패 재예약에 서명된 source 범위를 적용하고,
  비활성 source를 제외한다. RETRY_WAIT의 실행 시각을 반복해서 미루지 않는다.
- `1a3f377`: 승인된 두 source에 한정한 Windows 작업 등록기를 추가했다.
  작업을 Disabled 상태로 만들고 검증 이후 활성화했다.
- `caabcf9`: Surya 모델 시작과 OCR watchdog을 분리했다. `/health`는 생존,
  `/ready`는 모델 준비를 나타낸다. 준비 전 OCR은 재시도 가능한
  `503 PARSER_SURYA_NOT_READY`이며 완료 경고로 숨기지 않는다.
- `be794a7`: OCR 의존 작업은 Surya 준비 전에 claim/복호화하지 않는다.
  활성 source와 삭제 상태도 검사한다. 원본 fingerprint, 활성 버전/청크,
  fencing token, 양쪽 정리 완료를 확인하는 명시적 재처리 서비스를 추가했다.
- `18397f3`: 완료된 활성 버전의 정리 증거를 DB에 보존한다. 같은 작업을
  재처리할 때 기존 검색 노출을 유지하며, 새 청크 활성화와 정리 완료를
  구분한다. 오래된 token의 정리 완료 보고는 거절한다.

실행 앱은 기존 이미지에 변경 Python 파일을 read-only mount한 개발 구성이다.
새 코드가 기존 이미지에 포함됐다는 뜻이 아니다. 기본 Windows Compose,
generationless E2E, 로컬 source overlay, Surya GPU overlay를 모두 유지했다.

## 자동 동기화 실제 상태

TEST1과 DEPT_1을 enabled로 전환했다. 각 source의 watcher와 자정 정합성
작업을 등록·활성화했으며, 기존 Host Worker 및 승인 DEPT_2 표본 watcher를
유지했다. 모든 작업은 `uplex / Interactive / Limited`이다.

두 scoped watcher는 Running, 자정 작업은 Ready이며 수동 실행 결과는 0이었다.
광역 자정 작업은 Disabled이다. DEPT_2의 과거 due schedule은 claim하지 않았고
fence도 변하지 않았다. 다음 실제 자정은 2026-09-29 00:00 KST이며 아직
관찰하지 않았다. 15분 반복 실행과 AtLogOn 트리거가 설정돼 있다.

활성화 후 TEST1 스캔은 6→8, DEPT_1은 2→4로 증가했다. 신규 스캔은 모두
COMPLETE이며 파일 수는 각각 5와 3이다. 이는 DOC 수가 아니라 스캔 파일 수다.
문서/작업 수는 TEST1 3, DEPT_1 1, DEPT_2 1로 유지됐다. DEPT_2 스캔은 0이다.
전역 작업 7개에는 비활성 시험 source의 과거 완료 작업 2개가 포함된다.

사용자는 Windows를 계속 켜 두는 조건을 설명했다. 현 구성은 로그인 세션에
의존하며, 화면 잠금·터미널 종료와 로그아웃은 다르다. 로그아웃/재부팅 뒤의
S4U 실행 검증은 후순위로 남겼으며 현재 로그인 상태의 동작을 막는 조건으로
취급하지 않는다.

## Surya GPU 준비 및 OCR

GPU 이미지: `sha256:e64a78ea3acdc137a0038c23fed1dfca6f7a770aba790f70270a7261cdd5bbec`.
태그는 `docmind-surya-parser:gpu-0.22.1-cuda12.4-sm61`이다.

첫 컨테이너 시작은 NVIDIA prestart의 `ldconfig.real` signal 9로 실패했다.
앱/OCR 요청 전 단계이며 OOMKilled는 false였다. 한 번의 start 재시도로 기동했고
이후 healthy, restart count 0이었다. 이 일시적 초기화 실패의 원인은 확정하지 않았다.

- 기동 중 health=200/starting, ready=503/NOT_READY, OCR=503/NOT_READY를 관찰했다.
- 모델 시작 완료 로그는 22.494초였다. 준비 이후 ready=200이었다.
- 메모리 내 합성 이미지 OCR은 1.780초, 2개 block이며 예상 단어를 인식했다.
  GPU 실행 증거가 확인됐고 추론 중 health 응답 최대 0.003초, 중복 parse는
  503 busy였다. 모델 준비 시간과 OCR 시간을 합쳐 평균 성능으로 설명하지 않는다.
- OCR inference/watchdog/caller 제한은 기존 600/660/720초를 유지했다.
  SDK 재시도 합계는 바깥 watchdog으로 제한된다. 내부 재시도 정책까지 수정한
  것으로 표시하지 않는다.

## 첫 실제 DOC 재처리와 추가 발견

TEST1의 기존 OCR 연결 실패 경고 문서를 명시적 서비스로 재처리했다.
작업 ID는 `369b78bb3f881b642df8cdedad98c8c6`이다. API/UI에 재처리 버튼을
새로 공개한 것은 아니다.

| 항목 | 이전 | 재처리 완료 |
|---|---|---|
| 청크/토큰 | 105 / 3,325 | 146 / 7,489 |
| attempt / fence | 1 / 1 | 2 / 3 |
| 활성 청크 집합 | `425d3c1a720394cbf4438e24ff5c32e7` | `d929a4761b491e8ac31ac019d5412b6a` |
| ParserRun | `e60c3301a77415ff4d348490783fb24e` | `3c14df5dab69f22dbfc2654d1ec13640` |
| 작업 / Windows / Docker 정리 | COMPLETE | COMPLETE |

`PARSER_SURYA_UNAVAILABLE` 경고는 제거됐다. 최종 상태는 READY_WITH_WARNING이며
남은 경고는 LEGACY_DOC_CONVERTED_TO_DOCX, DOCX_GEOMETRY_UNAVAILABLE,
SURYA_MEDIA_EMPTY이다. 이전 run은 RETAINED, 새 raw_artifact_ref는 비어 있다.
활성 버전의 내용 해시는 검증된 작업 평문 해시와 일치했다. 해시 값과 원문은
이 보고서에 포함하지 않는다.

ParserRun 생성 13:37:25 KST부터 활성화 04:42:21 UTC(13:42:21 KST)까지
약 4분 56초였다. DB 생성/수정 시각과 activated_at의 시간대 표현이 달라
변환 후 비교했다. 해당 로그 구간의 HTTP POST 200은 45건, 추론 완료 로그는
42건이었다. 개수 차이가 있으므로 이를 문서의 전체 이미지 수로 단정하지 않는다.
해당 구간 watchdog timeout은 관찰되지 않았다.

첫 재처리 중 기존 청크는 보존됐지만 검색 projection이 현재 job의 COMPLETE를
요구해 해당 문서가 검색에서 빠졌다. 전체 검색 후보가 40에서 32로 줄었으며
완료 후 다시 40으로 돌아왔다. 이 발견을 `18397f3`으로 수정했다.

## 검색 유지 수정의 실제 배포 및 브라우저 검증

비종료 작업 0과 임시 평문 0을 확인한 뒤 관련 Windows 작업 여섯 개를 잠시
정지·비활성화하고 앱만 재생성했다. 표준 DB migration으로 신규 필드가 추가됐고
기존 활성 버전 다섯 개만 정리 증거가 인증됐다. 배포한 model/service/projection
세 파일의 컨테이너 SHA-256은 checkout과 일치했다. 작업 여섯 개는 이전 상태로
복원했고 광역 자정 작업은 계속 Disabled다.

앱 재접속 뒤 공용 workspace와 검색 API가 정상 응답했다. 첫 TEST1 문서를
다시 재처리해 fence 4→5, attempt 3, PARSING/OCR_MEDIA_SURYA를 확인했다.
이때 기존 활성 청크 집합과 search_cleanup_complete=true가 유지됐다.
Codex 내장 브라우저에서 실제 검색을 제출한 결과는 다음과 같다.

| 재처리 중 검색 범위 | 결과 | 결합 후보 | 범위 확인 |
|---|---:|---:|---|
| 대상 TEST1 문서 하나 | 5 | 8 | TEST1 결과 5, 다른 source 표시 0 |
| 전체 문서 | 5 | 40 | 재처리 문서의 후보 소실 없음 |
| TEST1 상위 폴더 + 하위 폴더 | 5 | 24 | TEST1 결과 5, 다른 source 표시 0 |

앞선 DEPT_1 문서 하나 및 상위 폴더 검색도 각각 결과 5/후보 8이며
다른 source 표시가 없었다. 검색 결과 본문은 보고서에 복사하지 않았다.
watcher 재시작 후 완료 스캔 수는 TEST1 10, DEPT_1 6으로 증가했지만
중복 작업은 생성되지 않았으며 DEPT_2 스캔 0/작업 1은 유지됐다.

자료 관리의 source 폴더와 문서 상세/청크 화면 진입도 확인했다. 단, 재처리 중
목록의 `인덱싱 완료` 표시는 이전 활성 색인의 상태를 보여 주며 진행 중인
재처리 작업을 별도로 표시하지 않는다. 검색 가능 여부와 동기화 진행 상태를
함께 보여 주는 UI 개선은 남았다. 원문 미리보기 완결 검증으로 확대하지 않는다.

두 번째 재처리는 13:53:32 KST 생성, 04:58:55 UTC(13:58:55 KST) 활성화로
약 5분 23초였다. 최종 job/Windows cleanup/Docker cleanup 모두 COMPLETE,
search_cleanup_complete=true, attempt 3/fence 5다. 새 ParserRun은
`ba779eee5040934486ca4566fbf46e0b`, 새 청크 집합은
`5f95211bf7d5a10afa55615a41f80a2d`이며 146개 청크/7,486토큰이다.
이전 두 run은 RETAINED이고 세 run 모두 raw_artifact_ref가 비어 있다.
반복 OCR 토큰 수는 첫 재처리와 3토큰 차이로, 완전히 동일한 출력이라는
주장을 하지 않는다. 경고 코드는 첫 재처리와 동일하고 OCR 연결 실패는 없다.
완료 후 대상 문서의 브라우저 검색은 결과 5/후보 8, 다른 source 표시 0이었다.

마지막 확인에서 비종료 작업 및 정리 미완료 작업은 0건이었다. 승인 원본 DOC
네 개의 SHA-256·크기·UTC 수정시각은 시작 전과 같았다. Windows jobs의 파일과
디렉터리, 앱 `/tmp`와 `/run/docmind-ephemeral-parser`의 일반파일, Surya `/tmp`의
일반파일은 모두 0이었다. 이는 검사한 경로의 결과이며 파일 삭제를 완전 소거로
설명하지 않는다. DEPT_2 스캔 0/작업 1과 scoped task 상태도 유지됐다.

## 검증과 남은 범위

Surya 집중 회귀 12개, claim/reprocess 집중 회귀 36개와 기존 Phase5 3개,
정리 증거/검색 projection 회귀 54개가 통과했다. 각 묶음은 중복 가능성이 있어
합산해 전체 독립 테스트 수로 표시하지 않는다. 변경 서비스/테스트 Ruff와
db_models의 F/E 검사가 통과했다. 기존 Office 테스트 일부는 저장소에 없는
fixture 때문에 실행하지 못했으며, 전체 테스트 통과로 확대하지 않는다.
저장소의 제거 runtime 재유입 검사와 `git diff --check`도 통과했다.

기존 660초 OCR 종료의 정확한 원인은 확정하지 못했다. 모델 준비와 OCR 예산의
혼합 위험을 제거하고 실제 실패 표본의 재처리를 성공시킨 것이 이번 근거다.
모든 timeout과 GPU 초기화 실패의 재발 방지를 증명한 것은 아니다.

실제 자정, 로그아웃/재부팅, 서로 다른 본문 A→B의 같은 논리 경로 교체,
장기 처리량·대표 문서군 정확도는 남았다. 동일 DOC 재처리는 A→B 시험의
대체가 아니다. 이전 fixture 설정 읽기·생성·scan 복합 명령은 자동 승인 검토에서
`blocked by policy`로 거절됐고 세부 사유는 제공되지 않았다. 그 작업을
다른 경로로 재시도하지 않았다.
