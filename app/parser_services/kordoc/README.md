# kordoc HWP/HWPX 시험 경로

현재는 합성 문서로 확인하는 독립 시험 경로다. DocMind 수집 작업의 엔진 선택이나 검색 활성화에는 연결되지 않았다.

- kordoc 4.15.7을 고정한다. 컨테이너 빌드에서 의존성을 설치하고 실행 시 `KORDOC_OFFLINE=1`로 구동한다.
- `/v1/parse`는 HWP/HWPX 원본 bytes의 base64, SHA256, 형식을 받는다. 원본 파일 경로는 받지 않는다. 결과는 구조 블록만 반환한다.
- 이미지 bytes와 OCR은 이 시험에서 제외한다. 입력 한도는 64 MiB, 동시 파싱은 1건이다.
- `rag.parser_platform.kordoc_pilot`은 Node 파싱 결과를 기존 `parser_services.common.hwp_chunker.HwpHybridChunker`에 전달한다. 새 경로에 `rhwp-python`은 필요하지 않다. 기존 rhwp 서비스의 자체 파싱과 상태 확인에는 `rhwp-python`이 계속 필요하다.
- 실행 예산 2 vCPU/2 GiB는 초기 검증 값이다. 실문서의 처리 시간과 최고 RAM은 아직 측정하지 않았다.

로컬 합성 시험: 이 디렉터리에서 `pnpm install --frozen-lockfile --ignore-scripts` 후 `node --test test/*.test.mjs`. Python 청킹 테스트는 `app/test/unit_test/rag/parser_platform/test_kordoc_pilot.py`를 실행한다.

다음 단계는 실제 HWP/HWPX의 내용·표·경고 확인, 기존 검색 청크와의 비교, 출처 정보와 수집 canary 연결이다. 시험 결과가 충족되기 전 운영 엔진 설정을 바꾸지 않는다.
