# Kordoc 핵심 결함 해결 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 현재 요청은 계획 수정만이며 구현은 시작하지 않는다.

**Goal:** 출처 오류, 정상 Excel 거절, 변환 중단 후 평문 잔존, 실행 충돌을 해결한다.

**Architecture:** Kordoc 관리 패치에서 페이지 경계를 보존하고 Python 정규화가 실제 Excel 응답을 처리하도록 수정한다. 부모 프로세스가 작업 정리를 소유하고, 큐와 worker가 동일한 청킹 설정·실행 식별자를 사용한다.

**Tech Stack:** Kordoc 4.15.7, Node.js, LibreOffice, Python, pytest, MySQL/Redis, Docker/WSL.

**Spec:** `.local/KORDOC-INDEPENDENT-VERIFICATION-2026-09-29.md`. 범위와 OCR 정책은 2026-09-29 사용자의 최신 지시를 우선한다. 원 보고서는 당시 검증 기록으로 보존한다.

## 확정된 범위와 OCR 정책

- 모든 이미지의 OCR 인식·추출을 보장하지 않는다. 기존 OCR로 얻을 수 있는 텍스트는 그대로 활용한다.
- 이미지/OCR 누락 여부를 사용자에게 표시하지 않는다. 누락 배지, 경고, ‘부분 완료’ 상태를 추가하지 않는다.
- 이미지가 빠졌거나 일부 글자를 인식하지 못했다는 이유만으로, 검색 가능한 본문·셀이 있는 문서 전체를 실패시키지 않는다. OCR 완전성만 강제하는 기존 검사도 이 정책에 맞게 완화한다.
- 검색 가능한 내용이 전혀 없는 문서, 손상된 파일, 실제 파서 실행 실패는 기존 실패 처리를 유지한다.
- C128/prefix 2400/BGE-M3/Jina v3.5와 기존 형식별 청킹 정책은 유지한다.
- 기존 미커밋 변경을 보존하며 제품 수정·배포·재색인은 이번 계획 작성에서 실행하지 않는다.

## 남기는 작업

| 순서 | 작업 | 남기는 이유 | 완료 기준 |
|---|---|---|---|
| 1 | 변환 작업 정리와 요청 충돌 방지 | 임시 평문·LibreOffice 잔존, 다른 요청 상태 침범 | 중단 후 파일/프로세스 정리, 다음 요청 정상 처리 |
| 2 | PDF/PPTX 페이지 출처 보존 | 다른 페이지의 내용을 1쪽 출처로 색인 | 3쪽/3장 marker의 출처가 각각 일치 |
| 3 | 정상 Excel 처리 및 OCR 허용 정책 반영 | 빈 시트·유효 병합 때문에 정상 파일 거절 | 본문·셀 색인 성공, OCR 누락 표시 없음 |
| 4 | 청킹 설정별 실행 분리와 중복 방지 | 다른 설정의 작업이 같은 run을 공유 | 다른 설정은 다른 run, 같은 run은 중복 실행 방지 |

## Review Focus

- 중단된 worker의 LibreOffice와 임시파일이 남는 경우 → Task 1.
- 거절된 요청의 늦은 이벤트가 다음 작업의 busy 상태를 해제하는 경우 → Task 1.
- 같은 스타일의 연속 페이지가 하나의 출처로 합쳐지는 경우 → Task 2.
- 빈 시트·병합 placeholder·이미지가 섞여도 정상 셀을 처리해야 하는 경우 → Task 3.
- 설정 변경·동시 등록·늦은 완료가 실행 및 활성화 식별자를 혼동시키는 경우 → Task 4.

## Task 1: 변환 작업 정리와 요청 소유권

**Files:** `app/parser_services/kordoc/src/{server,worker,parse,convert,cli}.mjs`, `app/parser_services/kordoc/test/server.test.mjs`; 생성 `app/parser_services/kordoc/test/integration/conversion-cleanup.test.mjs`.

**Interfaces:** 부모가 생성한 내부 `jobId/workDir`를 worker에 전달한다. `convertOffice(source, sourceFormat, { workDir }) -> Promise<Buffer>`로 경로를 주입하고 종료 처리는 소유 jobId를 확인한다. 외부 요청이 내부 경로를 지정하지 못하게 한다.

- [ ] 기존 PPTX 변환 SIGKILL 재현과 413→다음 요청 실행→이전 요청의 늦은 end 재현을 회귀 시험으로 고정하고 현재 실패를 확인한다.
- [ ] 부모가 작업 디렉터리를 소유하고 Linux worker·LibreOffice를 같은 전용 프로세스 그룹으로 관리한다. worker의 finally가 실행되지 않아도 부모가 프로세스 종료와 파일 삭제를 완료한다.
- [ ] 프로세스 종료 확인→소유 디렉터리 삭제→요청 슬롯 해제 순서를 적용한다. 전용 루트 밖은 삭제하지 않으며 정리 실패 시 새 작업을 받지 않는다.
- [ ] busy/pending 변경, timeout, close, aborted는 해당 jobId의 소유권이 맞을 때만 처리한다. 정리 완료 전이나 다른 요청 실행 중에는 새 요청을 허용하지 않는다.
- [ ] 실제 Linux 변환에서 정상 완료·취소·timeout·worker 강제 종료 후 잔존 파일/프로세스가 없고 다음 변환이 성공하는지 확인한다. 이전 요청의 늦은 이벤트 이후에도 새 작업의 busy가 유지되는지 확인한다.

## Task 2: PDF/PPTX 페이지 출처 보존

**Files:** `app/parser_services/kordoc/patches/kordoc@4.15.7.patch`, `app/parser_services/kordoc/test/parse.test.mjs`, `app/test/unit_test/rag/parser_platform/test_kordoc_canary_bridge.py`. 패치 변경에 필요한 `src/parse.mjs`, `Containerfile`, `pnpm-lock.yaml`, Python `config.py` 및 배포 설정의 patch digest도 함께 갱신한다.

**Interfaces:** 한 텍스트 블록은 원본 페이지/슬라이드의 `pageNumber/bbox`를 유지한다. Python에서 이미 합쳐진 텍스트의 출처를 추정 복원하지 않는다.

- [ ] 실제 3쪽 PDF와 3장 PPTX의 각 marker가 첫 페이지에 합쳐지는 실패를 재현한다.
- [ ] 관리 패치에서 `joinPageBreakWraps`의 페이지 간 텍스트 병합을 막고 같은 페이지 안의 기존 처리는 유지한다.
- [ ] 새 patch SHA와 lock·Node 응답·Python 기대값을 일치시켜 설치 및 응답 검증이 깨지지 않게 한다.
- [ ] 실제 파일→HTTP→정규화→청크에서 marker 1/2/3의 페이지·슬라이드가 각각 1/2/3인지 검사한다. 기존 페이지 제한 시험도 유지한다.

## Task 3: 정상 Excel 처리와 OCR 허용 정책 반영

**Files:** `app/rag/parser_platform/kordoc_office_pilot.py`, 필요 시 `app/rag/parser_platform/office_chunker.py`, `app/parser_services/kordoc/src/parse.mjs`; 시험 `app/test/unit_test/rag/parser_platform/{test_kordoc_office_pilot,test_office_chunker_kordoc,test_kordoc_canary_bridge}.py`, `app/parser_services/kordoc/test/parse.test.mjs`.

**Interfaces:** `_normalize_excel_document(...)`는 원래 시트 순서를 유지한다. `_table_content(...)`는 기존 text/HTML 반환 계약을 유지한다. OCR 누락을 위한 새 응답 필드·경고·사용자 상태는 만들지 않는다.

- [ ] 실제 `blank-plain.xlsx`와 `merged-only.xlsx` 응답으로 각각 현재 실패를 확인한다.
- [ ] heading 다음에 table이 없어도 빈 시트로 건너뛰고 나머지 시트를 처리한다. 모두 비어 있으면 검색 가능한 내용 없음으로 종료한다.
- [ ] 병합에 덮인 좌표의 빈 dict를 정상 placeholder로 허용한다. 내용이 있거나 별도 span을 가진 충돌 셀은 계속 거절한다. 병합 anchor 텍스트와 HTML span, 기존 원자적 행 묶음을 보존한다.
- [ ] 공통 정규화·Node 이미지 처리에서 OCR 완전성만으로 전체 문서를 거절하는 지점을 확인한다. 검색 가능한 본문·셀이 있으면 이미지 미추출/미인식만으로 실패시키지 않고 가능한 텍스트를 사용한다. 이미지 파일명을 본문으로 색인하지 않는다. 실제 파일 손상·실행 실패 검사는 유지한다.
- [ ] 빈 시트 앞뒤 배치·실제 세로/가로 병합 파일이 성공하는지 확인한다. `Visible cell`과 이미지가 있는 XLSX는 셀을 정상 색인하고 OCR 누락 경고·부분 완료 표시를 만들지 않는지 확인한다. 모든 이미지의 인식 여부는 합격 기준으로 삼지 않는다.

## Task 4: 청킹 설정별 run 분리와 중복 실행 방지

**Files:** `app/rag/parser_platform/{config,coordinator}.py`, `app/api/db/services/{parser_run_service,task_service}.py`, `app/rag/svr/task_executor_refactor/chunk_service.py`; 필요 시 `app/api/db/db_models.py`, `app/api/db/services/chunk_set_activation_service.py`. 시험 `app/test/unit_test/rag/parser_platform/test_coordinator.py`, `app/test/unit_test/api/db/services/{test_parser_run_service_office,test_parser_platform_office_queue,test_chunk_set_activation_db}.py`.

**Interfaces:** `ParserRunRequest`에 실제 적용할 `chunking_config`를 전달한다. config fingerprint는 기본값을 해석한 설정을 포함하고 queue/readback/worker는 같은 설정 스냅샷을 사용한다.

- [ ] `.local/kordoc-independent-deploy/test_queue_config_reuse.py`로 NORMALIZING 중 청크 크기·구분자 변경이 동일 run을 재사용하는 실패를 고정한다.
- [ ] 실제 청킹 설정을 한 번 해석해 fingerprint에 포함한다. chunk_token_num 기본 128, delimiter 기본 `\n!?。；！？`, Excel의 excel_chunk_token_num 우선 규칙을 반영한다. 동일 의미의 설정은 같은 fingerprint를 만든다.
- [ ] 저장된 설정을 worker까지 전달하고 다른 설정의 요청은 새 run/chunk_set으로 분리한다. 같은 run의 동시 등록은 DB transaction/lock 안에서 기존 task를 재사용하고 worker가 중복 실행하지 못하게 한다.
- [ ] 기존 활성화 조건이 최신 요청의 run/chunk_set을 검사하는지 확인하고 부족한 부분만 보완한다. 늦게 끝난 예전 설정 작업이 새 결과를 덮어쓰지 못하게 한다. 일반 큐 복구 체계의 재설계로 범위를 넓히지 않는다.
- [ ] 같은 설정 중복 요청·다른 설정 요청·늦은 완료를 시험한다. 실제 MySQL/Redis의 격리된 동시성 시험으로 대역 시험의 한계를 보완한다.

## 필요한 검증만 실행

각 Task는 해당 재현 시험의 실패→수정→통과와 영향받는 기존 시험으로 완료한다. 변경된 파서의 실제 파일 HTTP 처리까지 확인한다. 검증을 전체 전환 인증 사업으로 확대하지 않는다.

```bash
# app/parser_services/kordoc
node --test test/*.test.mjs
# 실제 LibreOffice가 있는 격리 Linux parser 컨테이너
node --test test/integration/conversion-cleanup.test.mjs

# app, 의존성이 준비된 격리 Python 환경
python -m pytest -q test/unit_test/rag/parser_platform/test_kordoc_office_pilot.py test/unit_test/rag/parser_platform/test_office_chunker_kordoc.py test/unit_test/rag/parser_platform/test_kordoc_canary_bridge.py test/unit_test/rag/parser_platform/test_coordinator.py
python -m pytest -q test/unit_test/api/db/services/test_parser_run_service_office.py test/unit_test/api/db/services/test_parser_platform_office_queue.py test/unit_test/api/db/services/test_chunk_set_activation_db.py
```

패치 변경 시 frozen pnpm 설치와 Kordoc 컨테이너 빌드를 검증한다. 기존 HTTP 재현은 `.local/kordoc-independent-root/README.md`의 독립 환경을 재사용한다. 합성 fixture만 사용하고 운영 데이터·서비스는 변경하지 않는다.

## 이번 계획에서 제외

OCR 정확도 개선·이미지 전수 추출·누락 탐지/표시, 하이라이트 좌표 확대, pretty JSON 지원, 잔여 Docling 정리, 앱 버전 표기 정리, 실제 파일에서 미재현된 계약 방어, HWP 표본 확대·발표자 노트, 모델 해시 체계, 전체 lock/앱 빌드/startup 감사, 성능 시험, 전체 검색 종단간 인증, 운영 재색인 계획은 제외한다. 제외 항목을 숨은 후속 필수 작업이나 이번 완료 조건으로 다시 넣지 않는다.

완료 판정은 위 네 작업에 한정한다. 전체 보고서의 모든 문제 해결이나 운영 전환 완료를 뜻하지 않는다.
