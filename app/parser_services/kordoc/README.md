# kordoc 단계적 파서 시험 경로

현재는 합성 문서로 확인하는 독립 시험 경로다. DocMind 수집 작업의 엔진 선택이나 검색 활성화에는 연결되지 않았다.

- kordoc 4.15.7을 고정한다. 컨테이너 빌드에서 의존성을 설치하고 실행 시 `KORDOC_OFFLINE=1`로 구동한다.
- `/v1/parse`는 HWP, HWPX, DOCX, PDF, XLSX 원본 bytes의 base64, SHA256, 형식을 받는다. 원본 파일 경로는 받지 않는다. 결과는 구조 블록만 반환한다.
- 이미지 bytes와 OCR은 이 시험에서 제외한다. 입력 한도는 64 MiB, 동시 파싱은 1건이다.
- `rag.parser_platform.kordoc_pilot`은 Node 파싱 결과를 기존 `parser_services.common.hwp_chunker.HwpHybridChunker`에 전달한다. 새 경로에 `rhwp-python`은 필요하지 않다. 기존 rhwp 서비스의 자체 파싱과 상태 확인에는 `rhwp-python`이 계속 필요하다.
- `rag.parser_platform.kordoc_office_pilot`은 DOCX와 텍스트 PDF의 구조 블록을 기존 Word/PDF 청커에 연결한다. 원본에 이미지가 있으면 OCR 누락을 막기 위해 이 경로를 거부한다.
- XLSX는 파싱 결과 비교까지만 진행한다. kordoc 4.15.7 IR은 빈 행·열과 병합 셀의 원본 좌표를 보존하지 않아 기존 Excel 행 청커에 안전하게 전달할 수 없다.
- 실행 예산 2 vCPU/2 GiB는 초기 검증 값이다. 실문서의 처리 시간과 최고 RAM은 아직 측정하지 않았다.

로컬 합성 시험: 이 디렉터리에서 `pnpm install --frozen-lockfile --ignore-scripts` 후 `node --test test/*.test.mjs`. Python 청킹 테스트는 `app/test/unit_test/rag/parser_platform/test_kordoc_pilot.py`와 `test_kordoc_office_pilot.py`를 실행한다.

다음 단계는 실제 문서의 내용·표·경고와 기존 검색 청크 비교다. DOCX 제목 스타일, PDF 표, 이미지 문서, XLSX 원본 셀 좌표를 별도로 확인해야 한다. 시험 결과가 충족되기 전 운영 엔진 설정을 바꾸지 않는다.
