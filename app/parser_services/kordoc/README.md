# Kordoc 검색 파서

DocMind의 HWP, HWPX, DOC, DOCX, PDF, XLS, XLSX, PPTX 검색 파싱은 이 Node 서비스의 `/v1/parse`를 사용한다. 요청은 원본 bytes의 base64, SHA-256, `source_format`을 전달하며 원본 경로는 전달하지 않는다. DOC와 PPTX는 컨테이너의 LibreOffice로 각각 DOCX와 PDF로 변환한 뒤 Kordoc에 전달한다. rhwp는 미리보기 전용이다.

응답 `schema_version`은 `docmind-kordoc-v2`이며 원본 형식·해시, 구조 `blocks`, `metadata`, `warnings`, 진단용 `markdown`, `patch_revision`을 포함한다. 검색 텍스트는 `blocks`만 사용한다. PDF 응답에만 `pdf_pages`가 있으며 원본의 빈 페이지까지 순서대로 `{page,width,height,has_images,ocr_applied}`를 기록한다. 크기는 블록 bbox와 같은 PDF point 좌표계다. `metadata.pageCount`는 원본 페이지 수다. DOCX 이미지 OCR은 원본 이미지 해시와 OCR 글을 반환하고 이미지 bytes는 응답에서 제외한다.

PDF의 자동 머리글·바닥글 제거는 끄고 오프라인 OCR을 켠다. Kordoc의 기존 페이지 순회에서 이미지·좌표·OCR 정보를 모으고 페이지 처리 전에 상한을 적용하는 patch를 사용한다. 요청 `max_pdf_pages`와 서버 `KORDOC_MAX_PDF_PAGES` 중 작은 값이 상한이며, 초과 시 HTTP 400 `{code:"PARSER_PAGE_LIMIT_EXCEEDED",page_count:N}`을 반환한다. 부분 파싱과 불완전한 페이지 metadata는 실패 처리한다.

HTTP 서버는 별도 Node 작업 프로세스 하나를 유지해 요청을 순서대로 처리한다. `KORDOC_TIMEOUT_MS`는 작업마다 적용한다. timeout, 연결 끊김, 잘못된 출력, 파싱 실패 또는 프로세스 종료 시 그 작업 프로세스를 폐기하고 다음 요청에서 새 프로세스를 시작한다. 입력 한도는 기본 64 MiB이며 동시 파싱은 한 건이다. `KORDOC_MODEL_CACHE` 아래 `ppocr/{det.onnx,rec_korean.onnx,rec_korean.yml}`을 읽기 전용으로 탑재한다. `KORDOC_REQUIRE_OCR=1`이면 `/health`가 모델 존재를 확인한다.

Python의 `rag.parser_platform.kordoc_office_pilot`이 결과를 정규화하고 `OfficeChunker`가 형식별 검색 청크를 만든다. PDF 이미지 페이지는 검색 가능한 OCR 텍스트와 페이지 좌표가 없으면 거부한다. XLS/XLSX는 시트별 행을 묶어 청킹하며 빈 시트는 건너뛰고 기록한다.

Kordoc은 `4.15.7`로 고정한다. `pnpm-workspace.yaml`의 `patchedDependencies`가 `patches/kordoc@4.15.7.patch`를 설치 시 적용하고, 컨테이너 빌드는 patch SHA-256을 검사한다. v2 응답의 `patch_revision`은 같은 digest다. patch를 변경할 때는 빌드 검사 값, 응답 값, Python 설정 fingerprint를 함께 갱신한다. 컨테이너의 LibreOffice core/writer/impress는 Debian 패키지 `4:7.4.7-1+deb12u14`로 고정한다. 저장소에서 이 버전이 사라지면 빌드가 실패한다. 패키지를 올릴 때는 변환기 revision/fingerprint도 갱신하고 DOC/PPTX 변환 시험을 다시 실행한다.

로컬 검증은 이 디렉터리에서 `pnpm install --frozen-lockfile --ignore-scripts` 후 `pnpm test`를 실행한다. Python 연동·청킹 검증은 `app/test/unit_test/rag/parser_platform/test_kordoc_client.py`, `test_kordoc_office_pilot.py`, `test_office_chunker.py`에 있다. 합성 문서의 Node 테스트와 OCR 모델을 탑재한 격리 컨테이너의 DOC/DOCX/PDF/PPTX smoke 검증은 완료했다. 실제 사용자 문서의 품질, 처리 시간, 최고 메모리 및 운영 배포는 검증되지 않았다. Compose의 초기 예산은 2 vCPU/3 GiB다.
