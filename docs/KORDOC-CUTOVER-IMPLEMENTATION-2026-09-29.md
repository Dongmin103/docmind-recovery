# Kordoc 검색 파서 전환 구현 상태 (2026-09-29)

이 문서는 `KORDOC-CUTOVER-HANDOFF-2026-09-29.md`에서 지적한 구현 전 상태 이후의 코드 변경을 기록한다. 실행 중인 서비스와 데이터는 이 작업에서 변경하지 않았다.

## 현재 검색 파싱 경로

지원 형식 PDF, DOC, DOCX, XLS, XLSX, PPTX, HWP, HWPX는 작업 생성과 실행에서 Kordoc 한 경로를 사용한다. 지원 형식의 Kordoc 설정이 꺼져 있거나 서비스 준비 조건이 충족되지 않으면 작업을 거부하며 기존 파서로 되돌아가지 않는다. `ParserPlatformStandardBridge`는 Kordoc v2 응답의 원본 해시, 파서 버전, 관리 패치 해시, PDF 페이지 메타데이터를 검증하고 raw artifact와 정규화된 문서를 저장한다. 이후 `OfficeChunker`가 검색 청크를 만든다.

PDF 페이지 수와 이미지/OCR 메타데이터는 Kordoc의 기존 PDF 페이지 순회에서 수집한다. 등록 단계에서 `pypdf`로 페이지를 미리 세거나 정규화 단계에서 `pdfplumber`로 다시 열지 않는다. PDF 페이지 제한은 Kordoc 내부에서 페이지 처리 전에 적용하고, 초과 시 실제 페이지 수를 수집 오류에 전달한다. Kordoc Markdown은 진단 artifact에만 남기며 검색 텍스트는 구조 블록에서 한 번 만든다.

Excel은 Kordoc 셀·행 정보를 사용해 토큰 수를 계산하고 청크가 닫힐 때만 표시 HTML을 생성한다. 세로 병합은 겹치는 행 구간만 원자 단위로 묶는다. 빈 시트는 기록하고 건너뛰며, 전체가 빈 통합문서는 검색 내용 없음으로 실패한다. 중첩 셀 블록은 순서대로 처리한다. HWP의 페이지 번호는 레이아웃 페이지로 확인된 경우에만 탐색 가능한 출처에 넣는다.

Kordoc이 이미지 블록을 반환했지만 HWP/HWPX/XLS/XLSX에서 OCR 또는 원본 추출이 수행되지 않은 경우에는 내용 누락을 감추지 않고 실패한다. DOCX 삽입 이미지는 원본 ZIP 이미지 해시를 검증한 OCR 결과를 사용한다. PDF는 페이지별 이미지/OCR 증거를 검증한다. DOC→DOCX와 PPTX→PDF LibreOffice 변환은 Kordoc 4.15.7의 형식 지원 공백 때문에 유지한다. `rhwp-python`은 원문 미리보기 이미지에만 남는다.

## 배포 구성과 재현성

검색 파서 Compose 오버레이에는 Kordoc 서비스만 있고 PDF·Office·HWP 스위치는 기본으로 켜진다. Windows generationless E2E 오버레이도 Kordoc healthcheck를 기다린다. 메인 Python 패키지에서 `docling-core` 의존성을 제거하고 잠금 파일을 갱신했다. 사용하지 않는 Surya/Docling/rhwp 검색 서비스와 Python 모듈은 제거했다. Node 서버는 격리된 작업 프로세스 한 개를 재사용하고, 오류·시간 초과·연결 종료 후 폐기해 다음 요청에서 새로 시작한다.

작업 fingerprint는 형식별로 Kordoc 버전·패치 SHA-256·OCR 모델 revision·정규화기/청커 revision과 DOC/PPTX용 LibreOffice converter revision을 포함한다. 패치 파일은 이미지 빌드에서 SHA-256을 확인하고, Python 브리지는 응답의 동일한 `patch_revision`을 요구한다. LibreOffice 패키지 버전은 이미지 빌드에서 고정한다. OCR 모델 번들을 교체할 때는 `PARSER_PLATFORM_KORDOC_OCR_MODEL_REVISION`도 갱신해야 한다.

## 확인 범위

Kordoc 서비스의 Node 단위 테스트 24개, 관리 패치의 frozen install, 격리된 이미지 빌드와 `/health` 및 DOCX/PDF/스캔 PDF/DOC/PPTX 파싱 smoke test를 수행했다. Python에서는 parser-platform 130개, 수집 74개, 작업 생성/재사용·활성 청크 범위 26개, Compose YAML 7개가 통과했다. 구체적인 명령과 결과는 `.local/kordoc-cutover-*-report.md`와 `.local/kordoc-cutover-progress.md`에 남긴다. 등록 서비스 단위 테스트는 기존 테스트 하네스의 `common.settings` stub이 API 전체 자동 등록에 필요한 `TIMEZONE`, `FACTORY_LLM_INFOS` 등을 제공하지 않아 일반 실행에서는 수집 단계에서 실패한다. API 자동 등록만 우회하고 stub 값을 임시로 보충한 격리 실행에서는 16개가 통과했지만, 나머지 11개는 테스트 이미지에 비동기 pytest 플러그인이 없거나 외부 MySQL 접속을 시도해 실패했다. 임시 조치는 원복했으며 해당 수정 파일의 Python 구문은 별도로 확인했다. 실제 운영 문서의 검색 품질과 처리 시간, 최고 메모리는 아직 측정하지 않았다.

## 운영 적용 전 확인

`KORDOC_OCR_MODEL_DIR`에 검증된 `ppocr/{det.onnx,rec_korean.onnx,rec_korean.yml}`을 준비하고, 새 RAGFlow/Kordoc 이미지를 함께 빌드한다. 이미지와 OCR 모델 번들의 불변 식별값을 운영 환경에 기록하고 Kordoc의 `/health`를 확인한다. 합성 문서뿐 아니라 실제 PDF·DOC(X)·XLS(X)·PPTX·HWP(X)에서 출처, 표, OCR 누락, 검색 청크 수와 처리 자원을 검증한 뒤 서비스를 교체한다. 이 구현 작업은 운영 컨테이너 재기동이나 기존 데이터 재색인을 수행하지 않았다.
